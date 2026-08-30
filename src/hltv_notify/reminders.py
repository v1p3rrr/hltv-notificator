"""Pre-match reminders.

The first notification about a match is "the match has started", and one would
like to sit down beforehand. The set of intervals is per subscriber: fifteen
minutes is enough for some, others need an hour to get to the television.

A reminder is targeted: intervals differ between subscribers, so the event does
not go to everyone the match concerns but to one specific chat (`only_chat` in
the payload).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from .config import Config
from .models import Event
from .state.db import Storage, parse_iso, utcnow

log = logging.getLogger(__name__)

TICK_SECONDS = 30.0


def humanize(minutes: int) -> str:
    """15 -> "15 min", 60 -> "1 h", 90 -> "1 h 30 min"."""
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    if rest == 0:
        return f"{hours} h"
    return f"{hours} h {rest} min"


class ReminderScheduler:
    def __init__(self, storage: Storage, config: Config):
        self.storage = storage
        self.config = config

    def due(self, now: Optional[datetime] = None) -> List[Event]:
        """Reminders that should go out right now."""
        now = now or utcnow()
        events: List[Event] = []

        for chat_id in self.storage.subscriber_ids():
            offsets = self.storage.reminders(chat_id)
            if not offsets:
                continue
            for row in self.storage.upcoming_matches(now):
                if not self._is_theirs(chat_id, row["match_id"]):
                    continue
                start = parse_iso(row["start_utc"])
                for minutes in offsets:
                    # The window opens exactly N minutes before and closes at
                    # the start. It will not fire twice: the key contains the
                    # match and the interval, and the journal of what was sent
                    # will not let it be created a second time.
                    if not (now < start <= now + timedelta(minutes=minutes)):
                        continue
                    events.append(self._event(chat_id, row, minutes, start, now))
        return events

    def _is_theirs(self, chat_id: str, match_id: int) -> bool:
        tracked = self.storage.match_team_ids(match_id)
        if not tracked:
            # The match was created before subscribers existed — remind everyone.
            return True
        return any(chat_id in self.storage.subscribers_tracking(team_id)
                   for team_id in tracked)

    def _event(self, chat_id: str, row, minutes: int,
               start: datetime, now: datetime) -> Event:
        left = max(1, int((start - now).total_seconds() // 60))
        team_id = row["team_id"]
        return Event(
            type="E10",
            idempotency_key=f"E10:{row['match_id']}:remind:{minutes}",
            match_id=row["match_id"],
            payload={
                "only_chat": chat_id,
                "team_name": self.storage.team_name(team_id, self.config.team_name),
                "team_id": team_id,
                "opponent": row["opponent_name"],
                "opponent_id": row["opponent_id"],
                "event_name": row["event_name"],
                "start_utc": row["start_utc"],
                "minutes_before": minutes,
                "minutes_left": left,
                "url": row["url"],
            },
        )

    async def run(self, stop: asyncio.Event, notifier) -> None:
        while not stop.is_set():
            try:
                for event in self.due():
                    notifier.enqueue(event)
            except Exception:  # noqa: BLE001 - reminders do not bring the process down
                log.exception("the reminder scheduler failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
            except asyncio.TimeoutError:
                continue
