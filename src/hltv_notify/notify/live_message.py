"""The live score message: one per map, updated as the game goes on.

This is NOT an event. It has no idempotency key and does not need re-delivery
after a restart — it needs redrawing with the current state. That is why it
goes around the outbox: the queue exists so that milestones are never lost,
whereas a stale score frame is exactly what may and should be dropped.

The message id is kept in the database, otherwise after a restart the service
would start a second live message for the same map.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional, Tuple

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

    @property
    def _interval(self) -> float:
        return max(float(self.config.live_edit_seconds), HARD_MIN_EDIT_SECONDS)

    async def update(self, match_id: int, snapshot: dict, *, force: bool = False,
                     finalize: bool = False) -> None:
        """The live message is per subscriber.

        It is edited in place and the message id differs per chat, so there
        cannot be one shared message for everyone.
        """
        if not self.config.live_message or not snapshot:
            return
        for chat_id, for_team_id in self._recipients(match_id):
            await self._update_one(chat_id, for_team_id, match_id, snapshot,
                                   force=force, finalize=finalize)

    def _recipients(self, match_id: int):
        """The same computation as the event queue's — and the same pause check.

        Having its own computation here is exactly what was missing: the live
        message used to reach someone who had asked for quiet with `/pause`.
        """
        return [(chat, teams[0] if teams else None)
                for chat, teams in audience.match_audience(
                    self.storage, self.config, match_id)]

    async def _update_one(self, chat_id: str, for_team_id, match_id: int, snapshot: dict,
                          *, force: bool = False, finalize: bool = False) -> None:
        map_number = int(snapshot.get("map_number") or 0)
        if map_number <= 0:
            return

        row = self.storage.live_message(chat_id, match_id, map_number)
        if row is not None and row["finalized"]:
            return

        key = (chat_id, match_id, map_number)
        if not force:
            elapsed = time.monotonic() - self._last_edit.get(key, 0.0)
            if elapsed < self._interval:
                return

        text = fmt.render_live(fmt.orient(snapshot, for_team_id),
                               team_name=self.config.team_name)
        if row is not None and row["last_text"] == text and not finalize:
            # The score has not changed — an edit with the same text only
            # spends the rate limit.
            self._last_edit[key] = time.monotonic()
            return

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
                return

        self._last_edit[key] = time.monotonic()
        self.storage.save_live_message(
            chat_id, match_id, map_number, telegram_message_id=message_id,
            text=text, finalized=finalize)

    async def finalize(self, match_id: int, snapshot: dict) -> None:
        """The last edit once the map is over: freeze the final score."""
        await self.update(match_id, snapshot, force=True, finalize=True)
