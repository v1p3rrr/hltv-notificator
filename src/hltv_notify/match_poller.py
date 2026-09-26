"""Polling match pages: the actual start, the course of the series, the finish.

Lives separately from schedule polling and at a different frequency. Active
only around matches: a team can go weeks without playing, and round-the-clock
active polling would be disrespectful to the source for no benefit at all.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable, Dict, List, Optional

from .config import Config
from .http import HltvHttp, SourceRejected, SourceUnavailable, jittered
from .models import Event, MatchState
from .notify.outbox import Notifier
from .sources import match_page
from .sources.match_page import MatchObservation, ParseError
from .state.db import Storage, iso, parse_iso, utcnow
from .state.match_machine import MatchMachine
from .watchdog import Watchdog

log = logging.getLogger(__name__)

IDLE_RECHECK_SECONDS = 60.0
LAST_MATCH_POLL_KEY = "last_match_poll_utc"

# How long before its start a match's page is read at the live cadence. HLTV
# moves matches in the minutes around the slot — and after it, for as long as
# the match is late — and this page is where both the move and the real start
# show first. Seen in use: 18:45 moved to 18:55, visible on HLTV at 18:46,
# reported around 18:49 by the three-minute pre-match cadence. Five minutes
# ahead costs a handful of reads per match.
DUE_LEAD_MINUTES = 5


class MatchPoller:
    def __init__(self, storage: Storage, config: Config, http: HltvHttp, notifier: Notifier,
                 supervisor=None, recheck_schedule: Optional[Callable[[], None]] = None):
        self.storage = storage
        self.config = config
        self.http = http
        self.notifier = notifier
        self.supervisor = supervisor
        # The schedule poller's out-of-turn request (`SchedulePoller.request_poll`).
        self.recheck_schedule = recheck_schedule
        self.machine = MatchMachine(storage, config)
        self.watchdog = Watchdog(storage, config)
        self._last_poll_failed = False
        self.mode = "idle"
        self.live_feed_active = False
        # The start this page last made us ask the schedule about, per match —
        # so a team page that lags behind does not turn the request into a
        # schedule sweep every minute. Private to this poller.
        self._start_rechecked: Dict[int, datetime] = {}

    # ------------------------------------------------------------------

    def active(self, now: Optional[datetime] = None):
        return self.storage.active_matches(
            now, lookahead_minutes=self.config.prematch_window_minutes)

    def _mode_for(self, rows, now: Optional[datetime] = None) -> str:
        live = any(row["state"] == MatchState.LIVE for row in rows)
        if live and not self.live_feed_active:
            return "live"
        if self._any_due(rows, now):
            # Ahead of "live_with_feed" on purpose: one cycle polls every row,
            # and a second team's match about to start must not be read every
            # five minutes because the first one has a feed.
            return "due"
        if live:
            return "live_with_feed"
        return "prematch" if rows else "idle"

    def _any_due(self, rows, now: Optional[datetime] = None) -> bool:
        """Is one of the matches being polled about to start, or late for it."""
        if not rows:
            return False
        polled = {row["match_id"] for row in rows}
        due = self.storage.matches_awaiting_start(
            now, grace_minutes=self.config.late_start_grace_minutes,
            ahead_minutes=DUE_LEAD_MINUTES)
        return any(row["match_id"] in polled for row in due)

    # ------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            rows = self.active()
            self._reconcile_live_feed(rows)
            self.mode = self._mode_for(rows)

            if rows:
                try:
                    await self.poll_once(rows)
                except Exception:  # noqa: BLE001 - polling must not bring the process down
                    log.exception("unexpected failure while polling matches")
                # Once more, now on fresh state: a match may have just gone
                # LIVE, and waiting a whole cycle to bring the feed up would
                # lose a minute where the entire point is speed.
                self._reconcile_live_feed(self.active())
                self.mode = self._mode_for(self.active())
                delay = jittered(self.config.interval_for(self.mode))
            else:
                # No matches nearby — not a single request, just a cheap look
                # at the database.
                delay = IDLE_RECHECK_SECONDS

            log.debug("match polling: mode %s, %d active, next cycle in %.0fs",
                      self.mode, len(rows), delay)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                continue

    # ------------------------------------------------------------------

    def _reconcile_live_feed(self, rows) -> None:
        """The live feed is only brought up for running matches.

        It also decides the page polling mode: while the feed is connected the
        page is only needed for cross-checking and is polled far less often.
        """
        if self.supervisor is None:
            return
        live = {row["match_id"]: row["url"]
                for row in rows if row["state"] == MatchState.LIVE}
        self.supervisor.reconcile(live)
        self.live_feed_active = self.supervisor.any_connected
        for event in self.watchdog.check_live_feed(self.supervisor.connected_matches()):
            self.notifier.enqueue(event)

    async def poll_once(self, rows=None) -> List[Event]:
        rows = self.active() if rows is None else rows
        produced: List[Event] = []
        produced_failure = False
        for row in rows:
            events = await self._poll_match(row)
            produced_failure = produced_failure or self._last_poll_failed
            produced.extend(events)
        if rows and not produced_failure:
            self.storage.set_meta(LAST_MATCH_POLL_KEY, iso(utcnow()))
            for event in self.watchdog.report_success("match_page"):
                self.notifier.enqueue(event)
                produced.append(event)
        return produced

    async def _poll_match(self, row) -> List[Event]:
        match_id = row["match_id"]
        url = row["url"]
        self._last_poll_failed = False
        try:
            html = await self.http.get_text(url)
        except (SourceRejected, SourceUnavailable) as exc:
            self._last_poll_failed = True
            log.error("match page %s cannot be read: %s", match_id, exc)
            return self._degraded(f"Match page {match_id} cannot be read: {exc}")

        try:
            observation = match_page.parse(html, match_id)
        except ParseError as exc:
            self._last_poll_failed = True
            self.storage.log_raw("match_page", url, "200/parse-error", html[:20000],
                                 self.config.raw_log_days)
            log.error("could not parse match page %s: %s", match_id, exc)
            return self._degraded(f"Match page {match_id} could not be parsed: {exc}")

        self._notice_moved_start(observation)
        feed_connected = bool(
            self.supervisor and self.supervisor.connected_matches().get(match_id))
        events = self.machine.apply(observation, feed_connected=feed_connected)
        for event in events:
            self.notifier.enqueue(event)
        if events:
            log.info("match %s: events %s", match_id, [e.type for e in events])
        else:
            log.debug("match %s: %s, series %s", match_id, observation.status,
                      observation.series_score(
                          self.storage.canonical_team(match_id) or self.config.team_id))
        return events

    def _notice_moved_start(self, observation: MatchObservation) -> None:
        """Ask the schedule to look again when this page shows another start.

        Around the slot this page is read every minute and the team page every
        three, and that is exactly when HLTV moves a match — so this page is
        usually the first to know. It decides nothing, though: E2 belongs to
        the schedule machine, and a second writer of the start time would
        flip it back and forth whenever the two pages disagree for a poll. It
        only makes the one writer look NOW rather than on its next cycle, and
        if the team page does not agree, nothing is announced — a wrong read
        here costs one request, never a false E2.

        Once per distinct time: a team page that lags behind this one must not
        turn the request into a schedule sweep every minute.
        """
        if self.recheck_schedule is None or observation.start_utc is None:
            return
        match_id = observation.match_id
        if observation.status in (match_page.STATUS_LIVE, match_page.STATUS_OVER):
            # A move after the start is not news, and the memo is done with.
            self._start_rechecked.pop(match_id, None)
            return
        team_id = self.storage.canonical_team(match_id) or self.config.team_id
        if observation.our_side(team_id) is None:
            # The machine discards this page as someone else's; so is its time.
            return
        known = self.storage.effective_start(match_id)
        if known is None:
            return
        if parse_iso(known) == observation.start_utc:
            self._start_rechecked.pop(match_id, None)
            return
        if self._start_rechecked.get(match_id) == observation.start_utc:
            return
        self._start_rechecked[match_id] = observation.start_utc
        log.info("match %s: the page says it starts at %s, the schedule has %s "
                 "— reading the schedule again now",
                 match_id, iso(observation.start_utc), known)
        self.recheck_schedule()

    # ------------------------------------------------------------------

    def _degraded(self, detail: str) -> List[Event]:
        events = self.watchdog.report_failure("match_page", detail)
        for event in events:
            self.notifier.enqueue(event)
        return events
