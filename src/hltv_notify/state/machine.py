"""The state machine: the only place where events are born.

An event arises on a TRANSITION of state, not on the fact that data arrived.
Sources only record observations. This is fundamental: the schedule is polled
constantly and returns the same thing, and the live feed sends the full state
many times a second and again after every reconnect. "We saw X, so send a
notification" is guaranteed to produce duplicates.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from ..config import Config
from ..models import Event, MatchState, ScheduleEntry
from ..sources import team_page
from .db import Storage, iso, parse_iso, utcnow

log = logging.getLogger(__name__)


def bootstrap_key(team_id: int) -> str:
    """The first-run flag is PER TEAM.

    A team can be added through the bot while the service is running, and at
    that moment it has dozens of already played matches. A shared flag would
    make such an addition noisy: an E1 for every match of the new schedule.
    """
    return f"bootstrapped:{team_id}"


def _key_time(dt: datetime) -> str:
    """Time in an idempotency key is always UTC and truncated to seconds.

    The key must depend only on the event's content. Let the time the response
    arrived get in here and deduplication stops working.
    """
    return dt.astimezone(dt.tzinfo).replace(microsecond=0).isoformat()


class ScheduleMachine:
    """Transitions born from schedule observations: E1, E2, E3."""

    def __init__(self, storage: Storage, config: Config):
        self.storage = storage
        self.config = config

    # ------------------------------------------------------------------

    def apply(self, entries: List[ScheduleEntry], team_id: int,
              now: Optional[datetime] = None) -> List[Event]:
        now = now or utcnow()
        bootstrap = self.storage.get_meta(bootstrap_key(team_id)) is None

        events: List[Event] = []
        seen: Dict[int, ScheduleEntry] = {e.match_id: e for e in entries}

        for entry in entries:
            events.extend(self._apply_entry(entry, team_id, now=now, bootstrap=bootstrap))

        events.extend(self._detect_disappeared(seen, team_id, now=now, bootstrap=bootstrap))

        if bootstrap:
            self.storage.set_meta(bootstrap_key(team_id), iso(now))
            log.info(
                "team %s taken under observation: %d matches recorded silently, "
                "no notifications sent for them", team_id, len(entries),
            )
        return events

    # ------------------------------------------------------------------

    def _apply_entry(self, entry: ScheduleEntry, team_id: int, *,
                     now: datetime, bootstrap: bool) -> List[Event]:
        snapshot = team_page.snapshot_of(entry)
        snapshot_hash = team_page.hash_of(snapshot)
        existing = self.storage.get_match(entry.match_id)

        if existing is None:
            # Played matches are recorded but produce no E1: "new match" about
            # something already finished is noise.
            self.storage.upsert_match(
                match_id=entry.match_id, opponent_id=entry.opponent_id,
                opponent_name=entry.opponent_name, event_name=entry.event_name,
                start_utc=entry.start_utc, url=entry.url,
                snapshot=snapshot, snapshot_hash=snapshot_hash, team_id=team_id,
            )
            self.storage.link_match_team(entry.match_id, team_id)
            self.storage.set_state(
                entry.match_id,
                MatchState.FINISHED if entry.finished else MatchState.SCHEDULED,
                source="team_page",
            )
            if bootstrap or entry.finished:
                return []
            return [self._event_e1(entry)]

        events: List[Event] = []
        confirmed_start = parse_iso(existing["start_utc"])
        start_for_storage = confirmed_start

        if not entry.finished:
            moved, new_start = self._check_time_change(
                entry, confirmed_start=confirmed_start, now=now, bootstrap=bootstrap)
            if moved is not None:
                events.append(moved)
            if new_start is not None:
                start_for_storage = new_start

        # The match may have been created by another tracked team — the link is
        # made regardless, but the perspective (matches.team_id) stays put.
        self.storage.link_match_team(entry.match_id, team_id)
        canonical = self.storage.canonical_team(entry.match_id)
        oriented = canonical is None or canonical == team_id
        self.storage.upsert_match(
            match_id=entry.match_id,
            opponent_id=entry.opponent_id if oriented else existing["opponent_id"],
            opponent_name=entry.opponent_name if oriented else existing["opponent_name"],
            event_name=entry.event_name,
            start_utc=start_for_storage, url=entry.url,
            snapshot=snapshot, snapshot_hash=snapshot_hash, team_id=team_id,
        )
        return events

    # ------------------------------------------------------------------

    def _check_time_change(self, entry: ScheduleEntry, *, confirmed_start: datetime,
                           now: datetime, bootstrap: bool):
        """E2 with a threshold and a debounce.

        Moving a match back and forth is routine. Notifying about every edit is
        irritating; not notifying loses the point. So: shifts below the
        threshold are swallowed silently, and a burst of edits within a short
        window collapses into a single event carrying the last value.
        """
        observed = entry.start_utc
        if observed == confirmed_start:
            self._clear_pending(entry.match_id)
            return None, None

        shift = abs(observed - confirmed_start)
        if shift < timedelta(minutes=self.config.e2_min_shift_minutes):
            # A small shift: quietly accept the new time, do not call it an event.
            self._clear_pending(entry.match_id)
            return None, observed

        state = self.storage.get_state(entry.match_id)
        pending_start = state["pending_start_utc"] if state else None
        pending_since = state["pending_since_utc"] if state else None

        if pending_start != iso(observed):
            # A new (or changed) proposal to move it — the window restarts.
            self.storage.set_pending_start(entry.match_id, observed, now)
            return None, None

        if pending_since is None:
            self.storage.set_pending_start(entry.match_id, observed, now)
            return None, None

        held_for = now - parse_iso(pending_since)
        if held_for < timedelta(minutes=self.config.e2_debounce_minutes):
            log.debug("match %s: holding the reschedule for another %s", entry.match_id,
                      timedelta(minutes=self.config.e2_debounce_minutes) - held_for)
            return None, None

        self._clear_pending(entry.match_id)
        if bootstrap:
            return None, observed
        return self._event_e2(entry, old_start=confirmed_start), observed

    def _clear_pending(self, match_id: int) -> None:
        state = self.storage.get_state(match_id)
        if state is not None and state["pending_start_utc"] is not None:
            self.storage.set_pending_start(match_id, None, None)

    # ------------------------------------------------------------------

    def _detect_disappeared(self, seen: Dict[int, ScheduleEntry], team_id: int, *,
                            now: datetime, bootstrap: bool) -> List[Event]:
        """E3: the match vanished from the team page.

        Careful: a match also leaves "Upcoming" in the normal course of events —
        when it starts and moves to "Recent results". Such a match stays
        visible on the page, that is, it lands in `seen`. A real disappearance
        is when it is nowhere. If the scheduled start is still ahead we read
        that as a cancellation or a reschedule; if the start has already
        passed, we stay quiet and let the match-page polling work it out.
        """
        events: List[Event] = []
        for match_id in self.storage.tracked_match_ids(team_id):
            if match_id in seen:
                continue
            row = self.storage.get_match(match_id)
            if row is None:
                continue
            state = self.storage.get_state(match_id)
            if state is not None and state["state"] in (MatchState.FINISHED, MatchState.CANCELLED):
                self.storage.mark_missing(match_id, now)
                continue

            start = parse_iso(row["start_utc"])
            self.storage.mark_missing(match_id, now)
            if start <= now:
                log.warning(
                    "match %s vanished from the page but its start has already passed — "
                    "no E3, the match-page polling will decide the state", match_id)
                self.storage.set_state(match_id, MatchState.UNKNOWN, source="team_page")
                continue

            self.storage.set_state(match_id, MatchState.CANCELLED, source="team_page")
            if not bootstrap:
                events.append(self._event_e3(row))
        return events

    # ------------------------------------------------------------------

    def _event_e1(self, entry: ScheduleEntry) -> Event:
        return Event(
            type="E1",
            idempotency_key=f"E1:{entry.match_id}:new",
            match_id=entry.match_id,
            payload={
                "opponent": entry.opponent_name,
                "opponent_id": entry.opponent_id,
                "event_name": entry.event_name,
                "start_utc": entry.start_utc.isoformat(),
                "url": entry.url,
                "placeholder": entry.opponent_is_placeholder,
            },
        )

    def _event_e2(self, entry: ScheduleEntry, *, old_start: datetime) -> Event:
        return Event(
            type="E2",
            idempotency_key=f"E2:{entry.match_id}:moved:{_key_time(entry.start_utc)}",
            match_id=entry.match_id,
            payload={
                "opponent": entry.opponent_name,
                "event_name": entry.event_name,
                "old_start_utc": old_start.isoformat(),
                "start_utc": entry.start_utc.isoformat(),
                "url": entry.url,
            },
        )

    def _event_e3(self, row) -> Event:
        return Event(
            type="E3",
            idempotency_key=f"E3:{row['match_id']}:cancelled",
            match_id=row["match_id"],
            payload={
                "opponent": row["opponent_name"],
                "event_name": row["event_name"],
                "start_utc": row["start_utc"],
                "url": row["url"],
            },
        )
