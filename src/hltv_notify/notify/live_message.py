"""The live score message: one per map, updated as the game goes on.

This is NOT an event. It has no idempotency key and does not need re-delivery
after a restart — it needs redrawing with the current state. That is why it
goes around the outbox: the queue exists so that milestones are never lost,
whereas a stale score frame is exactly what may and should be dropped.

The message id is kept in the database, otherwise after a restart the service
would start a second live message for the same map.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional, Tuple

from .. import settings
from ..config import Config
from ..state.db import Storage
from . import audience
from . import format as fmt
from .telegram import Telegram, TelegramError

log = logging.getLogger(__name__)

# Telegram dislikes frequent edits. Even if the config asks for more, we refuse.
HARD_MIN_EDIT_SECONDS = 5.0

# How long a redraw in flight is given to finish on shutdown before it is
# dropped. Short: Docker's stop_grace_period is behind us, and the queue
# needs the rest of it.
CLOSE_GRACE_SECONDS = 2.0


class LiveMessenger:
    def __init__(self, storage: Storage, config: Config, telegram: Optional[Telegram]):
        self.storage = storage
        self.config = config
        self.telegram = telegram
        # The time of the last edit is kept in memory: the point is to limit
        # how often we call Telegram, not to survive a restart.
        self._last_edit: Dict[Tuple[int, int], float] = {}
        # The newest snapshot waiting to be drawn, per match, and the task
        # drawing it. See `submit`.
        self._pending: Dict[int, dict] = {}
        self._drawing: Dict[int, asyncio.Task] = {}
        # One move of one card at a time. `_drawing` serialises the feed's
        # redraws, but the QUEUE moves cards from its own task — so without
        # this the two can both find the card buried, both delete it and both
        # send a new one, leaving two cards for one map.
        self._moving: Dict[Tuple[str, int, int], asyncio.Lock] = {}

    def _interval(self, recipients: int) -> float:
        """How often ONE person's card may be redrawn.

        The card is per subscriber, so the total number of edits is
        `recipients / interval` while Telegram's budget stays the same. Holding
        the per-person interval fixed therefore means the total climbs with the
        audience until it hits the ceiling, and past that point the cards do
        not slow down gracefully — they start failing and going stale, which
        looks the same as a broken service.

        So what is held fixed is the total: with the default budget of ten
        edits a second, a hundred people still get the configured ten seconds,
        three hundred get thirty. Slower is honest; stuck is not.
        """
        wanted = max(float(self.config.live_edit_seconds), HARD_MIN_EDIT_SECONDS)
        budget = self.config.live_edit_budget
        if budget <= 0 or recipients <= 0:
            return wanted
        return max(wanted, recipients / float(budget))

    async def update(self, match_id: int, snapshot: dict, *, force: bool = False,
                     finalize: bool = False,
                     map_started: bool = False) -> List[str]:
        """The live message is per subscriber.

        It is edited in place and the message id differs per chat, so there
        cannot be one shared message for everyone.

        `map_started` says this frame is the one that started the map, so
        the message must also carry what E5 would have said. Returns the chats
        where that did NOT get through — the caller sends them a plain E5
        instead, because a milestone must not be lost on the best-effort path.
        """
        if not snapshot:
            # `config.live_message` is no longer checked here: it is the
            # default handed to a subscriber who never touched the
            # setting, and `_recipients` applies it per person. Checking
            # it again here would make the environment an override and
            # someone who turned the card ON could never get it.
            return []
        if finalize or map_started:
            # The two moments that must not run beside a background redraw.
            # `_draw` never passes either flag, so this cannot wait on itself.
            await self._settle(match_id)
        recipients = self._recipients(match_id)
        interval = self._interval(len(recipients))
        missed: List[str] = []
        for chat_id, for_team_id in recipients:
            # A subscriber who muted E5 gets the plain score message: muting
            # asked for exactly that, and the queue will not send them E5 either.
            carries_start = not self._muted(chat_id, for_team_id, "E5")
            ok = await self._update_one(chat_id, for_team_id, match_id, snapshot,
                                        force=force, finalize=finalize,
                                        announces_start=carries_start,
                                        interval=interval)
            if map_started and carries_start and not ok:
                missed.append(chat_id)
        return missed

    def submit(self, match_id: int, snapshot: dict) -> None:
        """Redraw the card without the caller waiting for it.

        The feed loop used to `await` the whole round of edits: one Telegram
        call per subscriber, in sequence. With a hundred of them that is some
        ten seconds during which no frame is read at all, so the score the
        cards are being drawn with is already stale by the time they are drawn.

        Only the NEWEST snapshot per match is kept. A frame that was overtaken
        while the previous round was in flight is worthless — it is a score
        nobody will ever need again — so it is dropped rather than queued. That
        is the same reasoning that keeps the card out of the outbox entirely.

        The two moments that cannot be fire-and-forget stay `await`ed by the
        caller: creating the card (it carries the map start, and the caller
        needs to know for whom that failed) and the final edit.
        """
        self._pending[match_id] = snapshot
        task = self._drawing.get(match_id)
        if task is None or task.done():
            self._drawing[match_id] = asyncio.create_task(self._draw(match_id))

    async def _settle(self, match_id: int) -> None:
        """Wait for a background redraw of this match to finish, if any.

        Without this, the final edit races the redraw it overtook. The draw
        reads the row before `finalized` is written, renders the score as it
        was, and finishes afterwards — and `save_live_message` writes
        `finalized = excluded.finalized`, so the freeze is cleared and the
        stale score becomes the card's last text. The map then goes on being
        redrawn after it has ended.

        Waited for rather than cancelled: a cancel in the middle of creating
        the card would leave the message posted and its id unsaved, and the
        next start would open a second card for the same map. The pending
        snapshot is dropped first, so the draw stops after the round it is
        already in.
        """
        self._pending.pop(match_id, None)
        task = self._drawing.pop(match_id, None)
        if task is not None and not task.done():
            try:
                await task
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - already logged inside _draw
                pass

    async def _draw(self, match_id: int) -> None:
        while True:
            snapshot = self._pending.pop(match_id, None)
            if snapshot is None:
                return
            try:
                await self.update(match_id, snapshot)
            except Exception:  # noqa: BLE001 - the card must not kill the feed
                log.exception("live message for match %s could not be redrawn",
                              match_id)

    async def close(self) -> None:
        """Let what is being drawn finish, briefly, and then drop it.

        Not an immediate cancel: a cancel landing on the await inside
        `send_message` leaves the card posted in the chat while its message id
        is never saved, and the next start finds no row and opens a SECOND
        card for the same map. The same reasoning as the queue's, which is not
        cancelled on shutdown either.

        The pending snapshot is dropped first so the draw stops after the
        round it is in, and the wait is bounded because Docker has its own
        timer behind us. A stale score frame that does not make it is no loss.
        """
        self._pending.clear()
        tasks = [task for task in self._drawing.values() if not task.done()]
        self._drawing.clear()
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=CLOSE_GRACE_SECONDS)
        if pending:
            log.warning("%d live message update(s) did not finish in %.0fs",
                        len(pending), CLOSE_GRACE_SECONDS)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    # ------------------------------------------------------------------
    # Keeping the card at the bottom

    @staticmethod
    def _buried(row) -> bool:
        """Has something been sent below this card since it was last posted.

        A finalized card is never buried, and the check has to be HERE rather
        than only in the query that finds them: the map can end between
        `buried_live_messages` and the re-read under the lock, and a move that
        went ahead anyway would delete the frozen final card, send it again and
        clear the freeze — the score would then keep being overwritten after
        the map was over.
        """
        return (row is not None
                and not row["finalized"]
                and row["telegram_message_id"] is not None
                and (row["bury_seq"] or 0) > (row["posted_seq"] or 0))

    def _move_lock(self, key) -> asyncio.Lock:
        lock = self._moving.get(key)
        if lock is None:
            lock = self._moving[key] = asyncio.Lock()
        return lock

    async def repost_buried(self, chat_id: str) -> None:
        """Move this chat's buried cards back to the bottom.

        Called by the QUEUE, right after it has finished delivering a chat's
        messages — not by the feed. That is the whole point: half time is
        exactly when the feed falls silent (see `FeedIdle`), so a card waiting
        for the next frame could sit above the half-time message for a minute.
        Being called from inside `_drain_chat` also gives the ordering for
        free: that loop serves one chat strictly in order, so the card lands
        below the message that buried it.

        The text is the one already in the database, which is what lets this
        run with no feed at all. The next ordinary redraw edits the new
        message with a fresh score as usual.
        """
        if self.config.dry_run or self.telegram is None:
            return
        for row in self.storage.buried_live_messages(chat_id):
            match_id = row["match_id"]
            if chat_id not in {chat for chat, _ in self._recipients(match_id)}:
                # Moving a card means SENDING one, so this path answers "who
                # gets this" the same way every other path does — through
                # `_recipients`, which is where the pause and `/settings card`
                # live. A row left over from before someone switched the card
                # off must not be re-sent to them.
                continue
            # A background redraw may be editing this very message. Waited for,
            # never cancelled: a cancel inside send_message leaves a card in
            # the chat whose id was never saved, and the next start would open
            # a second one. Safe here because this runs in the queue's task,
            # not inside `_draw` — `_update_one` must never call this.
            await self._settle(match_id)
            key = (chat_id, match_id, row["map_number"])
            async with self._move_lock(key):
                # Re-read under the lock: a redraw may have moved it already
                # while we waited, and moving it twice means two cards.
                row = self.storage.live_message(*key)
                if not self._buried(row):
                    continue
                seen = await self._drop(chat_id, row)
                if seen is None:
                    continue
                await self._resend(chat_id, row, row["last_text"], seen)

    async def _drop(self, chat_id: str, row):
        """Delete the card from the chat. Returns the burial it acted on.

        None means the card did not move and the caller must go on editing it.
        Telegram refuses deletes it considers impossible — too old, the bot
        lost the right — and in that case the burial is written off rather
        than retried: otherwise every frame would attempt the same delete for
        the rest of the map.
        """
        match_id, map_number = row["match_id"], row["map_number"]
        # The value being acted on, written back afterwards INSTEAD of the
        # current one. A burial landing while the delete and the send are in
        # flight then stays ahead, and the card moves again.
        seen = row["bury_seq"] or 0
        try:
            await self.telegram.delete_message(chat_id, row["telegram_message_id"])
        except TelegramError as exc:
            log.warning("the live card of match %s could not be moved down for "
                        "%s, it stays where it is: %s", match_id, chat_id, exc)
            self.storage.save_live_message(
                chat_id, match_id, map_number, telegram_message_id=None,
                text=row["last_text"], finalized=bool(row["finalized"]),
                posted_seq=seen)
            return None
        # Recorded before anything is sent: from here on the old message does
        # not exist, so a send that fails must leave the next redraw creating a
        # new card rather than editing a ghost.
        self.storage.forget_live_message_id(chat_id, match_id, map_number)
        return seen

    async def _resend(self, chat_id: str, row, text: str, seen: int) -> bool:
        match_id, map_number = row["match_id"], row["map_number"]
        try:
            new_id = await self.telegram.send_message(chat_id, text)
        except TelegramError as exc:
            log.warning("the live card of match %s was deleted for %s but not "
                        "sent again: %s", match_id, chat_id, exc)
            return False
        self._last_edit[(chat_id, match_id, map_number)] = time.monotonic()
        # `finalized` is carried over deliberately: it defaults to False and
        # the statement writes `finalized = excluded.finalized`, so omitting it
        # would unfreeze a card that was frozen while this move was in flight.
        self.storage.save_live_message(
            chat_id, match_id, map_number, telegram_message_id=new_id,
            text=text, finalized=bool(row["finalized"]), posted_seq=seen)
        log.info("live card of match %s map %d moved down for %s (new id %s)",
                 match_id, map_number, chat_id, new_id)
        return True

    def _muted(self, chat_id: str, for_team_id, event_type: str) -> bool:
        if for_team_id is None:
            return False
        return event_type in self.storage.team_mutes(chat_id, for_team_id)

    def _recipients(self, match_id: int):
        """The same computation as the event queue's — and the same pause check.

        Having its own computation here is exactly what was missing: the live
        message used to reach someone who had asked for quiet with `/pause`.

        The card is also the one thing a person can switch off on its own
        (`/settings card off`) — there is no event type to mute, because the
        card is not an event. LIVE_MESSAGE in the environment stays the
        default for anyone who has not said otherwise.
        """
        return [(chat, teams[0] if teams else None)
                for chat, teams in audience.match_audience(
                    self.storage, self.config, match_id)
                if self.storage.setting(
                    chat, "card", settings.default_for(self.config, "card"))]

    async def _update_one(self, chat_id: str, for_team_id, match_id: int, snapshot: dict,
                          *, force: bool = False, finalize: bool = False,
                          announces_start: bool = False,
                          interval: Optional[float] = None) -> bool:
        """True means the message is in the chat and up to date."""
        map_number = int(snapshot.get("map_number") or 0)
        if map_number <= 0:
            return False

        row = self.storage.live_message(chat_id, match_id, map_number)
        if row is not None and row["finalized"]:
            return True

        if row is None and snapshot.get("warmup"):
            # The card is not opened during the warmup. It IS the map's card —
            # it says the map has started — and during the warmup that is
            # simply untrue: the score sits at 0:0 and the warmup can run for
            # twenty minutes. Seen in the chat: the card appeared with
            # "round 1 · warmup" before anything had been played.
            #
            # Only creation is held back. A warmup in the middle of a map (a
            # server restart, a technical pause) finds the card already there,
            # and it keeps being updated.
            return False

        key = (chat_id, match_id, map_number)
        # Something arrived below the card. The throttle and the "same text"
        # shortcut both have to stand aside, or the move would be swallowed by
        # exactly the checks that exist to avoid pointless edits.
        buried = self._buried(row)
        if not force and not buried and row is not None:
            # The throttle applies to edits, never to creating the message:
            # holding the first one back would delay the map's card by the
            # whole interval.
            # "Never drawn" is None, not zero: time.monotonic() counts from
            # the machine's boot, so on a freshly started host zero looks like
            # a very recent edit and the redraw would be skipped.
            last = self._last_edit.get(key)
            if last is not None and time.monotonic() - last < (
                    self._interval(0) if interval is None else interval):
                return True

        text = fmt.render_live(fmt.orient(snapshot, for_team_id),
                               team_name=self.config.team_name,
                               announces_start=announces_start)
        if row is not None and row["last_text"] == text and not finalize and not buried:
            # The score has not changed — an edit with the same text only
            # spends the rate limit.
            self._last_edit[key] = time.monotonic()
            return True

        # Everything that decides which message this card IS, and then writes
        # to it, happens under one lock per card. Not just the move: the queue
        # can be moving this very card right now, and between its delete and
        # its send the row carries NO id at all. A redraw that read the row in
        # that window would conclude there is no card, send its own, and the
        # map would end with two.
        async with self._move_lock(key):
            row = self.storage.live_message(*key)
            if row is not None and row["finalized"]:
                # Frozen while we were rendering: the map ended. Writing now
                # would clear the freeze and hand the card a stale score.
                return True
            message_id = row["telegram_message_id"] if row is not None else None
            posted_seq = None

            if self._buried(row) and not (self.config.dry_run or self.telegram is None):
                # The feed-driven half of the move, and the reason it is not
                # simply `_resend`: a fresh score is already in hand, so the
                # card is deleted and RE-CREATED with it. Going through the
                # stored text would cost a third call to edit it afterwards.
                #
                # `_settle` is deliberately NOT called here: this runs inside
                # `_draw`, and waiting there would be waiting on ourselves.
                posted_seq = await self._drop(chat_id, row)
                if posted_seq is not None:
                    message_id = None

            if message_id is None and posted_seq is None:
                # A card created from scratch lands at the bottom by
                # construction, so it owes nothing to any burial recorded
                # before it existed. Without this it would be born already
                # "buried" — after a re-send that failed, for instance — and
                # the next redraw would delete and re-post the message that
                # had only just appeared.
                posted_seq = (row["bury_seq"] or 0) if row is not None else 0

            if self.config.dry_run or self.telegram is None:
                reason = "DRY_RUN" if self.config.dry_run else "Telegram not configured"
                log.debug("[%s] live message for match %s map %d:\n%s",
                          reason, match_id, map_number, text)
            else:
                try:
                    if message_id is None:
                        message_id = await self.telegram.send_message(chat_id, text)
                        log.info("live message for match %s map %d created for %s (id %s)",
                                 match_id, map_number, chat_id, message_id)
                    else:
                        await self.telegram.edit_message_text(chat_id, message_id, text)
                except TelegramError as exc:
                    # The live message is auxiliary. Bringing the worker down
                    # over it and losing milestones is not acceptable.
                    log.warning("live message for match %s map %d was not updated: %s",
                                match_id, map_number, exc)
                    self._last_edit[key] = time.monotonic()
                    return False

            self._last_edit[key] = time.monotonic()
            self.storage.save_live_message(
                chat_id, match_id, map_number, telegram_message_id=message_id,
                text=text, finalized=finalize, posted_seq=posted_seq)
            return True

    async def finalize(self, match_id: int, snapshot: dict) -> None:
        """The last edit once the map is over: freeze the final score."""
        await self.update(match_id, snapshot, force=True, finalize=True)
