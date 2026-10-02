"""The outgoing queue: a Telegram failure must not lose notifications.

An event arrives here already deduplicated (see Storage.record_event), so the
worker's job is simple: deliver it and stay within Telegram's limits.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, Optional

from .. import settings
from .. import streams as stream_lib
from ..config import Config
from ..models import Event
from ..state.db import Storage
from . import audience
from . import format as fmt
from .telegram import Telegram, TelegramError

log = logging.getLogger(__name__)

# The types the live card absorbs — a map point, the half, an overtime — are
# `fmt.CARD_EVENTS`, kept beside the function that draws their banner so the
# two cannot drift by one type. Only milestones of the map the card is about:
# they are rare, and the card is what the reader is watching when they arrive.
#
# E9 (multikill) and E15 (clutch) are deliberately absent — there are several a
# map, and a card that deletes and re-posts itself after each would spend the
# rate budget jumping around. Everything else is either about a different match,
# where rebuilding this card would be noise, or already lives in the card itself
# (E5) or ends it (E6).

# Events about ONE team's own doings rather than about the match: a highlight
# by one of its players, a bingo moment, a bingo card. They go to the people
# following THAT team and nobody else.
#
# This is not tidiness. A match can have two tracked teams in it, and none of
# these can be turned round for the other side the way a score can: a count of
# kills through smoke belongs to whoever made them, and shown to the opponent's
# follower it is simply wrong. `format.orient` cannot help — it sees the
# payload's team_id, finds it is not the reader's, and has nothing to flip.
TEAM_EVENTS = frozenset({"E9", "E15", "E16", "E17", "E18"})

# Types that can turn out to have been about rounds the server threw away, and
# are therefore sent carrying the map they belong to so they can be found
# again. Only the bingo moment: it is the only message this service sends about
# a single round of a map whose score can still be reset under it. A map result
# or a highlight is announced from a score the reset watch has already
# believed.
RETRACTABLE = frozenset({"E16"})

# Telegram's two limits are different in kind and are answered in two
# different places. Roughly one message per second into ONE chat is this
# constant, and it is also where the ordering guarantee lives: a chat's
# messages are sent one after another in queue order. The other limit — some
# thirty calls a second across everything — is not here at all, but in the
# Telegram client, which is the single door every sender goes through.
#
# The pause used to be applied between every two messages whoever they were
# for, which for one subscriber is the same thing and for a fan-out to fifty
# people meant a minute to deliver one map result.
SEND_INTERVAL_SECONDS = 1.2
# How many chats are served at once. Not a rate limit — the pacer above is —
# but a bound on the tasks in flight, so a thousand recipients do not become a
# thousand coroutines all waiting on the same gate.
MAX_CONCURRENT_CHATS = 16
# Rows taken from the queue in one go, and the ceiling on one pass so that a
# huge backlog cannot hold the worker forever and starve the retry timers.
BATCH_SIZE = 50
MAX_ROWS_PER_PASS = 1000
MAX_ATTEMPTS = 8
# How long the final pass gets on shutdown. Beyond that it is the caller's
# call: it has its own timer, and behind that is SIGKILL from Docker.
FINAL_DRAIN_SECONDS = 6.0


class Notifier:
    """Accepting events and delivering them. The only writer to Telegram."""

    def __init__(self, storage: Storage, config: Config, telegram: Optional[Telegram],
                 live_messenger=None):
        self.storage = storage
        self.config = config
        self.telegram = telegram
        # The live card, so the queue can hand it a milestone of the same map
        # instead of sending the milestone beside it. Optional: the replay
        # tool and most tests have no card at all.
        self.live_messenger = live_messenger
        # Which flags stand for which language. Parsed once: the config is
        # frozen for the life of the process, and this is read for every
        # multikill for every recipient.
        self._flag_languages = config.flag_languages()
        # Set by enqueue so the worker does not sleep out its five seconds
        # with a message already waiting. It matters where two messages have
        # to arrive in order — the live card waits for the "match started" it
        # continues — and it costs nothing anywhere else.
        self._arrived = asyncio.Event()

    def enqueue(self, event: Event) -> bool:
        """Queue the event for EVERYONE it concerns.

        False means it went to nobody: either it had already been sent to all
        of them, or it is muted for every matching subscriber.
        """
        created = 0
        for chat_id, for_team_id in self._recipients(event):
            # Their own taste in broadcasts, asked for only when the payload
            # actually carries streams, which costs two queries per
            # recipient. Keyed off the payload rather than off a list of
            # event types: a list would be a fourth thing to keep in step,
            # and this one cannot drift.
            stream_prefs = (self._stream_prefs(chat_id)
                            if event.payload.get("streams") else None)
            body = fmt.render(
                event, team_name=self.config.team_name,
                # Everyone has their own timezone: subscribers may live in
                # different ones.
                tz_name=self.storage.subscriber_timezone(chat_id, self.config.timezone),
                for_team_id=for_team_id,
                # And their own bar for what counts as a comeback. The map was
                # watched once, at the lowest bar in use; whether the line is
                # worth printing is decided here, per reader.
                comeback_threshold=self._threshold(chat_id, "comeback"),
                # And their own streams. Same reasoning again: one multikill,
                # many readers, and which are worth a tap is not a property
                # of the event.
                stream_prefs=stream_prefs)
            # A milestone the card absorbs is rendered twice, because which
            # form is sent is only known at delivery: the banner when this
            # reader has a card up, the message above when they do not.
            banner = map_number = None
            if event.type in fmt.CARD_EVENTS:
                banner = fmt.render_banner(
                    event, team_name=self.config.team_name,
                    for_team_id=for_team_id, stream_prefs=stream_prefs)
                map_number = event.payload.get("map_number")
            elif event.type in RETRACTABLE:
                # No banner — this one is never absorbed by the card — but the
                # map has to be on the row all the same: it is what a reset
                # score looks the message up by when it has to be struck
                # through. A NULL banner is still what says "deliver it as its
                # own message", so the two do not collide.
                map_number = event.payload.get("map_number")
            if self.storage.record_event(
                    idempotency_key=event.idempotency_key,
                    event_type=event.type,
                    match_id=event.match_id,
                    body=body,
                    chat_id=chat_id,
                    banner=banner,
                    map_number=map_number):
                created += 1

        if created:
            log.info("event %s queued for %d recipient(s): %s",
                     event.type, created, event.idempotency_key)
            self._arrived.set()
        else:
            log.debug("event went to nobody (duplicate or muted): %s",
                      event.idempotency_key)
        return bool(created)

    def _threshold(self, chat_id: str, name: str) -> int:
        return self.storage.setting(
            chat_id, name, settings.default_for(self.config, name))

    def _stream_prefs(self, chat_id: str):
        """This reader's broadcast block, or None when they switched it off.

        None rather than a zero limit: zero is a real value there and means
        "list every one of them", so the two cannot share a number. That is
        also why `streams` exists as its own switch.
        """
        if self._threshold(chat_id, "streams") <= 0:
            return None
        wanted = self.storage.text_setting(
            chat_id, "streams_langs",
            str(settings.default_for(self.config, "streams_langs")))
        return stream_lib.StreamPreference(
            limit=self._threshold(chat_id, "streams_count"),
            languages=tuple(stream_lib.parse_languages(wanted)),
            aliases=self._flag_languages)

    def _wants(self, chat_id: str, event: Event) -> bool:
        """The recipient's own threshold, as opposed to muting.

        Muting says "never this type for this team"; a threshold says "not
        this one" — the same event reaches the person who asked for 3k and not
        the one who asked for 5k. It is checked here rather than in the machine
        because the machine writes ONE event for everybody: it is built at the
        lowest bar in use and narrowed down at the moment it is addressed.
        """
        if event.type == "E9":
            wanted = self._threshold(chat_id, "multikill")
            return wanted > 0 and int(event.payload.get("kills") or 0) >= wanted
        if event.type == "E15":
            # Either bar, because a clutched round takes the message over: the
            # kills that happened in it are reported inside E15 and nowhere
            # else. Checking the clutch bar alone would lose a 4k outright for
            # a reader who has clutches off and multikills on — the round would
            # simply have been typed out of their reach.
            against = int(event.payload.get("clutch_against") or 0)
            clutch = self._threshold(chat_id, "clutch")
            if clutch > 0 and against >= clutch:
                return True
            multikill = self._threshold(chat_id, "multikill")
            return multikill > 0 and int(event.payload.get("kills") or 0) >= multikill
        if event.type == "E12":
            return self._threshold(chat_id, "half") > 0
        if event.type == "E13":
            return self._threshold(chat_id, "overtime") > 0
        if event.type in ("E17", "E18"):
            return self._threshold(chat_id, "bingo") > 0
        if event.type == "E16":
            # BOTH, and in this order. `bingo_live` is the per-moment stream
            # and `bingo` is the card itself; somebody who turned the card off
            # has not asked to keep receiving its moments, and a reader who
            # only ever set `bingo_live` would otherwise get them with no
            # summary to put them in.
            return (self._threshold(chat_id, "bingo") > 0
                    and self._threshold(chat_id, "bingo_live") > 0)
        return True

    def _recipients(self, event: Event):
        """Who this event is addressed to and which team to show it from.

        Who is listening at all is decided by `audience` — the pause check
        lives there too. What stays here is what only the queue knows: targeted
        events and muting by type.

        The rule for a match between two tracked teams: the event reaches a
        subscriber if AT LEAST ONE of their teams in that match has not muted
        the type. Otherwise one team would silently mute notifications about
        the other.
        """
        if event.match_id is None:
            rows = audience.service_audience(self.storage, self.config)
        else:
            teams = self.storage.match_team_ids(event.match_id)
            player_team = event.payload.get("team_id")
            if event.type in TEAM_EVENTS and player_team:
                # Addressed to those following THIS team — see TEAM_EVENTS.
                teams = [player_team]
            rows = audience.match_audience(self.storage, self.config,
                                           event.match_id, teams=teams)

        only_chat = event.payload.get("only_chat")
        if only_chat is not None:
            # A targeted event (a reminder): intervals differ between
            # subscribers, so it must not go to everyone in the match.
            if only_chat not in audience.active_subscribers(self.storage):
                return []
            mine = [(chat, their) for chat, their in rows if chat == only_chat]
            # The match may not be linked to any team yet — the reminder is
            # targeted regardless, so show it from the match's point of view.
            rows = mine or [(only_chat, [])]

        recipients = []
        for chat, their_teams in rows:
            if not self._wants(chat, event):
                log.debug("event %s is below %s's threshold", event.type, chat)
                continue
            if not their_teams:
                recipients.append((chat, None))
                continue
            wanted = [team_id for team_id in their_teams
                      if event.type not in self.storage.team_mutes(chat, team_id)]
            if not wanted:
                log.debug("event %s is muted for %s", event.type, chat)
                continue
            # A milestone the card absorbs is shown from the SAME side as the
            # card — its first team — even when that team has the type muted
            # and the second one is what let it through. The banner sits on
            # top of the card's body; oriented on the other team it would say
            # "map point against, 6:12" over a body reading 12:6.
            side = their_teams[0] if event.type in fmt.CARD_EVENTS else wanted[0]
            recipients.append((chat, side))
        return recipients

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self._arrived.clear()
            try:
                await self._drain()
            except Exception:  # noqa: BLE001 - the worker is not allowed to die
                log.exception("queue worker failed")
            # Whichever comes first: a new message, the stop signal, or the
            # five seconds. The timeout is still needed — a retry becomes due
            # on its own, with nobody enqueueing anything.
            waiters = [asyncio.create_task(stop.wait()),
                       asyncio.create_task(self._arrived.wait())]
            done, pending = await asyncio.wait(
                waiters, timeout=5, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()

        # The final pass, already on shutdown. An event may have been born a
        # second ago — the end of a map in a match that finished right during
        # the restart. Without this pass it would sit in the queue until the
        # next start, by which time the notification is useless.
        try:
            await self._drain(deadline=time.monotonic() + FINAL_DRAIN_SECONDS)
        except Exception:  # noqa: BLE001 - shutdown must not crash
            log.exception("failed to flush the queue on shutdown")

    async def retract(self, match_id: int, map_number: int,
                      sent_before: str) -> int:
        """Strike through what a map announced before its score was reset.

        Edited rather than deleted, and that is the whole point: the reader
        saw "Lack1 killed with a knife" go by and a message that quietly
        vanishes leaves them remembering something that never happened. A
        struck-through line with a ❌ on it says what the service now believes.

        The journal is not touched. The row stays 'sent' with its key in
        `sent_events`, because it WAS sent — anything that made it look unsent
        would have the queue deliver it again, which is the one failure this
        queue exists to prevent.

        Failures are logged and swallowed: a message the reader deleted, or one
        older than Telegram's 48-hour edit window, answers 400 forever, and a
        retraction that could not be made must not stall the feed behind it.

        The types come from `RETRACTABLE` rather than being named here: the
        same list decides which rows are queued carrying their map, and a type
        added to one and not the other would be stored so it could be found
        again and then never looked for. The same reasoning as `menu.MUTABLE`.

        In DRY_RUN there is nothing in the chat to edit, so the rows carry no
        message id — `delivered` is what lets them be found all the same, and
        the struck text goes to the log. That is not cosmetic: a run against
        live HLTV in DRY_RUN is how this project checks itself, and a
        retraction invisible there is a retraction nobody can verify.
        """
        sending = self._sending()
        rows = []
        for event_type in sorted(RETRACTABLE):
            rows.extend(self.storage.sent_before(
                event_type=event_type, match_id=match_id, map_number=map_number,
                created_before=sent_before, delivered=sending))
        if not rows:
            return 0
        done = 0
        for row in rows:
            if not sending:
                log.info("[retracted] %s", fmt.strike(row["body"]))
                self.storage.mark_retracted(row["id"])
                done += 1
                continue
            chat_id = row["chat_id"] or self.config.main_chat_id
            try:
                await self.telegram.edit_message_text(
                    chat_id, int(row["telegram_message_id"]),
                    fmt.strike(row["body"]))
            except TelegramError as exc:
                log.warning("could not strike through message %s: %s", row["id"], exc)
                continue
            self.storage.mark_retracted(row["id"])
            done += 1
        if done:
            log.info("struck through %d message(s) about map %s of match %s",
                     done, map_number, match_id)
        return done

    async def _drain(self, deadline: Optional[float] = None) -> None:
        """Send everything that is due.

        `deadline` (a monotonic timestamp) bounds the pass on shutdown: the
        service has little time left, and sending as much as fits beats being
        killed mid-send.
        """
        handled = 0
        while handled < MAX_ROWS_PER_PASS:
            if deadline is not None and time.monotonic() >= deadline:
                left = self.storage.pending_count()
                if left:
                    log.warning("queue: out of time, %d left", left)
                return
            rows = self.storage.due_outbox(limit=BATCH_SIZE)
            if not rows:
                return
            await self._send_batch(rows, deadline)
            handled += len(rows)
            if len(rows) < BATCH_SIZE:
                return

    async def _send_batch(self, rows, deadline: Optional[float]) -> None:
        """One batch: chats in parallel, each chat strictly in order.

        Two messages for the same person may depend on each other — the live
        card continues the "match started" it follows — so within a chat
        nothing overtakes anything. Two different people share nothing but
        Telegram's global rate, which the client itself holds.
        """
        by_chat: Dict[str, list] = {}
        for row in rows:
            by_chat.setdefault(row["chat_id"] or self.config.main_chat_id, []).append(row)
        if len(by_chat) == 1:
            await self._drain_chat(next(iter(by_chat.values())), None, deadline)
            return
        limit = asyncio.Semaphore(MAX_CONCURRENT_CHATS)
        # return_exceptions, and not for tidiness: without it the first
        # failure is raised while the other chats' coroutines keep running
        # detached. The caller would then log, wait, and read the queue
        # again — and those rows are still pending, because the task that
        # owns them has not reached mark_sent yet. That is one message sent
        # to the person twice, which is the one thing this queue exists to
        # prevent.
        results = await asyncio.gather(
            *(self._drain_chat(chat_rows, limit, deadline)
              for chat_rows in by_chat.values()),
            return_exceptions=True)
        for outcome in results:
            if isinstance(outcome, BaseException):
                log.error("a chat could not be drained: %r", outcome)

    async def _drain_chat(self, rows, limit: Optional[asyncio.Semaphore],
                          deadline: Optional[float]) -> None:
        if limit is not None:
            await limit.acquire()
        try:
            for index, row in enumerate(rows):
                if deadline is not None and time.monotonic() >= deadline:
                    return
                await self._deliver(row)
                if index + 1 < len(rows) and self._sending():
                    # The pause goes between messages, not after the last one:
                    # on shutdown a spare second is a second that may be
                    # missing.
                    await asyncio.sleep(SEND_INTERVAL_SECONDS)
        finally:
            if limit is not None:
                limit.release()

    def _sending(self) -> bool:
        """Whether messages really leave for Telegram. In DRY_RUN they go to
        the log instead, and neither of the two rates applies to a log."""
        return not (self.config.dry_run or self.telegram is None)

    async def _deliver(self, row) -> None:
        if self.config.dry_run or self.telegram is None:
            reason = "DRY_RUN" if self.config.dry_run else "Telegram not configured"
            log.info("[%s] message not sent, contents:\n%s", reason, row["body"])
            self.storage.mark_sent(row["id"], None)
            return

        attempts = row["attempts"] + 1
        chat_id = row["chat_id"] or self.config.main_chat_id

        # A milestone of a map goes INTO the card when this reader has one:
        # the card deletes itself and comes back with the milestone on top
        # and the score as of now, one message instead of two. Decided at
        # delivery, not at enqueue — that is when the message actually
        # reaches the chat, and whether a card is up can change in between.
        # Rows queued before the columns existed carry NULL and go plain.
        if (row["banner"] is not None and row["map_number"] is not None
                and row["match_id"] is not None and self.live_messenger is not None):
            message_id = await self.live_messenger.absorb(
                chat_id, row["match_id"], int(row["map_number"]), row["banner"])
            if message_id is not None:
                self.storage.mark_sent(row["id"], message_id)
                log.info("message %s delivered inside the live card (telegram id %s)",
                         row["id"], message_id)
                return

        try:
            message_id = await self.telegram.send_message(chat_id, row["body"])
        except TelegramError as exc:
            if exc.fatal:
                log.error("message %s dropped, retrying will not help: %s", row["id"], exc)
                self.storage.mark_sent(row["id"], None)
                return
            if attempts >= MAX_ATTEMPTS:
                log.error("message %s not delivered in %d attempts: %s",
                          row["id"], attempts, exc)
                self.storage.mark_retry(row["id"], attempts, 3600)
                return
            delay = exc.retry_after if exc.retry_after else min(2 ** attempts, 300)
            log.warning("Telegram refused it (%s), retrying in %.0fs", exc, delay)
            self.storage.mark_retry(row["id"], attempts, delay)
            return

        self.storage.mark_sent(row["id"], message_id)
        log.info("sent message %s (telegram id %s)", row["id"], message_id)
