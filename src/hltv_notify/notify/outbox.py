"""The outgoing queue: a Telegram failure must not lose notifications.

An event arrives here already deduplicated (see Storage.record_event), so the
worker's job is simple: deliver it and stay within Telegram's limits.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, Optional

from ..config import Config
from ..models import Event
from ..state.db import Storage
from . import audience
from . import format as fmt
from .telegram import Telegram, TelegramError

log = logging.getLogger(__name__)

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

    def __init__(self, storage: Storage, config: Config, telegram: Optional[Telegram]):
        self.storage = storage
        self.config = config
        self.telegram = telegram
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
            body = fmt.render(
                event, team_name=self.config.team_name,
                # Everyone has their own timezone: subscribers may live in
                # different ones.
                tz_name=self.storage.subscriber_timezone(chat_id, self.config.timezone),
                for_team_id=for_team_id)
            if self.storage.record_event(
                    idempotency_key=event.idempotency_key,
                    event_type=event.type,
                    match_id=event.match_id,
                    body=body,
                    chat_id=chat_id):
                created += 1

        if created:
            log.info("event %s queued for %d recipient(s): %s",
                     event.type, created, event.idempotency_key)
            self._arrived.set()
        else:
            log.debug("event went to nobody (duplicate or muted): %s",
                      event.idempotency_key)
        return bool(created)

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
            if event.type == "E9" and player_team:
                # A multikill is addressed to those following THIS player's team.
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
            if not their_teams:
                recipients.append((chat, None))
                continue
            wanted = [team_id for team_id in their_teams
                      if event.type not in self.storage.team_mutes(chat, team_id)]
            if not wanted:
                log.debug("event %s is muted for %s", event.type, chat)
                continue
            recipients.append((chat, wanted[0]))
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
