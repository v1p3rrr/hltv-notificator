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

from ..config import Config
from ..state.db import Storage
from . import audience
from . import format as fmt
from .telegram import Telegram, TelegramError

log = logging.getLogger(__name__)

# Telegram dislikes frequent edits. Even if the config asks for more, we refuse.
HARD_MIN_EDIT_SECONDS = 5.0


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
        if not self.config.live_message or not snapshot:
            return []
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
        """Drop whatever was still being drawn. Nothing is lost that matters:
        a card is a score that is about to be redrawn anyway, and the final
        edit goes through `finalize`, which is awaited."""
        self._pending.clear()
        tasks = [task for task in self._drawing.values() if not task.done()]
        self._drawing.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _muted(self, chat_id: str, for_team_id, event_type: str) -> bool:
        if for_team_id is None:
            return False
        return event_type in self.storage.team_mutes(chat_id, for_team_id)

    def _recipients(self, match_id: int):
        """The same computation as the event queue's — and the same pause check.

        Having its own computation here is exactly what was missing: the live
        message used to reach someone who had asked for quiet with `/pause`.
        """
        return [(chat, teams[0] if teams else None)
                for chat, teams in audience.match_audience(
                    self.storage, self.config, match_id)]

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
        if not force and row is not None:
            # The throttle applies to edits, never to creating the message:
            # holding the first one back would delay the map's card by the
            # whole interval.
            elapsed = time.monotonic() - self._last_edit.get(key, 0.0)
            if elapsed < (self._interval(0) if interval is None else interval):
                return True

        text = fmt.render_live(fmt.orient(snapshot, for_team_id),
                               team_name=self.config.team_name,
                               announces_start=announces_start)
        if row is not None and row["last_text"] == text and not finalize:
            # The score has not changed — an edit with the same text only
            # spends the rate limit.
            self._last_edit[key] = time.monotonic()
            return True

        message_id = row["telegram_message_id"] if row is not None else None
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
                # The live message is auxiliary. Bringing the worker down over
                # it and losing milestones is not acceptable.
                log.warning("live message for match %s map %d was not updated: %s",
                            match_id, map_number, exc)
                self._last_edit[key] = time.monotonic()
                return False

        self._last_edit[key] = time.monotonic()
        self.storage.save_live_message(
            chat_id, match_id, map_number, telegram_message_id=message_id,
            text=text, finalized=finalize)
        return True

    async def finalize(self, match_id: int, snapshot: dict) -> None:
        """The last edit once the map is over: freeze the final score."""
        await self.update(match_id, snapshot, force=True, finalize=True)
