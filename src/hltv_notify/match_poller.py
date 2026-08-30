"""Polling match pages: the actual start, the course of the series, the finish.

Lives separately from schedule polling and at a different frequency. Active
only around matches: a team can go weeks without playing, and round-the-clock
active polling would be disrespectful to the source for no benefit at all.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import List, Optional

from .config import Config
from .http import HltvHttp, SourceRejected, SourceUnavailable, jittered
from .models import Event, MatchState
from .notify.outbox import Notifier
from .sources import match_page
from .sources.match_page import ParseError
from .state.db import Storage, iso, utcnow
from .state.match_machine import MatchMachine
from .watchdog import Watchdog

log = logging.getLogger(__name__)

IDLE_RECHECK_SECONDS = 60.0
LAST_MATCH_POLL_KEY = "last_match_poll_utc"


class MatchPoller:
    def __init__(self, storage: Storage, config: Config, http: HltvHttp, notifier: Notifier,
                 supervisor=None):
        self.storage = storage
        self.config = config
        self.http = http
        self.notifier = notifier
        self.supervisor = supervisor
        self.machine = MatchMachine(storage, config)
        self.watchdog = Watchdog(storage, config)
        self._last_poll_failed = False
        self.mode = "idle"
        self.live_feed_active = False

    # ------------------------------------------------------------------

    def active(self, now: Optional[datetime] = None):
        return self.storage.active_matches(
            now, lookahead_minutes=self.config.prematch_window_minutes)

    def _mode_for(self, rows) -> str:
        if any(row["state"] == MatchState.LIVE for row in rows):
            return "live_with_feed" if self.live_feed_active else "live"
        return "prematch" if rows else "idle"

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

    # ------------------------------------------------------------------

    def _degraded(self, detail: str) -> List[Event]:
        events = self.watchdog.report_failure("match_page", detail)
        for event in events:
            self.notifier.enqueue(event)
        return events
