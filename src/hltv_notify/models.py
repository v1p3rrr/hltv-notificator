"""Shared structures. Sources record observations; only the state machine
gives birth to events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class ScheduleEntry:
    """One match as seen on the team page."""

    match_id: int
    start_utc: datetime
    opponent_id: Optional[int]
    opponent_name: str
    event_name: str
    url: str
    finished: bool
    score_team: Optional[int] = None
    score_opponent: Optional[int] = None

    @property
    def opponent_is_placeholder(self) -> bool:
        """"Winner of match X" and the like: the opponent is not decided yet."""
        return self.opponent_id is None


@dataclass(frozen=True)
class Event:
    """An event ready to send. The key is derived from the content."""

    type: str
    idempotency_key: str
    match_id: Optional[int]
    payload: Dict[str, Any] = field(default_factory=dict)


class MatchState:
    SCHEDULED = "SCHEDULED"
    IMMINENT = "IMMINENT"
    LIVE = "LIVE"
    MAP_LIVE = "MAP_LIVE"
    MAP_BREAK = "MAP_BREAK"
    FINISHED = "FINISHED"
    POSTPONED = "POSTPONED"
    CANCELLED = "CANCELLED"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"
