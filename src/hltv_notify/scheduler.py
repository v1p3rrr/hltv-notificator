"""Schedule polling and the choice of frequency mode.

A match must not be polled in active mode around the clock: a team can go weeks
without playing, and that is normal, not an error.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from .config import HARD_MIN_REQUEST_INTERVAL_SECONDS, HLTV_BASE, Config
from .http import HltvHttp, SourceRejected, SourceUnavailable, jittered
from .models import Event
from .notify.outbox import Notifier
from .sources import team_page
from .sources.team_page import ParseError
from .state.db import Storage, iso, parse_iso, utcnow
from .state.machine import ScheduleMachine
from .watchdog import Watchdog

log = logging.getLogger(__name__)

LAST_POLL_KEY = "last_schedule_poll_utc"
LAST_ERROR_KEY = "last_schedule_error"


class SchedulePoller:
    def __init__(self, storage: Storage, config: Config, http: HltvHttp, notifier: Notifier):
        self.storage = storage
        self.config = config
        self.http = http
        self.notifier = notifier
        self.machine = ScheduleMachine(storage, config)
        self.watchdog = Watchdog(storage, config)
        self.mode = "idle"
        self._force = asyncio.Event()
        # So the "too many teams" warning is written when the number
        # changes rather than on every cycle.
        self._warned_team_count = 0

    # ------------------------------------------------------------------

    def request_poll(self) -> None:
        """An out-of-turn poll requested by the /check command."""
        self._force.set()

    def current_mode(self, now: Optional[datetime] = None) -> str:
        now = now or utcnow()
        overdue = self.storage.matches_awaiting_start(
            now, grace_minutes=self.config.late_start_grace_minutes)
        if overdue:
            # The slot has arrived and the match has not. This is exactly when
            # HLTV moves it — by five minutes, then ten, then fifteen — and
            # exactly when the old rule stopped looking: with the start behind
            # us the match was no longer "upcoming", the mode fell to idle and
            # the next look at the page came half an hour later.
            log.debug("%d match(es) past their start and not running yet — "
                      "staying on the frequent schedule", len(overdue))
            return "prematch"

        upcoming = self.storage.upcoming_matches(now)
        if not upcoming:
            return "idle"
        nearest = parse_iso(upcoming[0]["start_utc"])
        window = timedelta(minutes=self.config.prematch_window_minutes)
        return "prematch" if nearest - now <= window else "idle"

    # ------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            # The flag is cleared BEFORE the poll, not after. Otherwise a
            # /check issued while a poll was already running was erased by that
            # clear: the bot answered "checking the schedule" and no
            # out-of-turn check happened — the next one came only after the
            # regular interval, up to half an hour later.
            self._force.clear()
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001 - polling must not bring the process down
                log.exception("unexpected failure while polling the schedule")

            self.mode = self.current_mode()
            delay = jittered(self.config.interval_for(self.mode))
            log.info("mode %s, next poll in %.0fs", self.mode, delay)

            if self._force.is_set():
                # The request arrived during the poll — serve it right away.
                log.info("out-of-turn poll requested by /check")
                continue

            waiters = [asyncio.create_task(stop.wait()), asyncio.create_task(self._force.wait())]
            done, pending = await asyncio.wait(
                waiters, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if stop.is_set():
                return

    # ------------------------------------------------------------------

    def _warn_if_too_many(self, teams: int) -> None:
        """Say so when the team count has outgrown the request ceiling.

        A sweep reads one page per DISTINCT team and no page may be asked for
        more often than once every 30 seconds, so the sweep itself takes
        `teams x 30 s`. Once that exceeds the pre-match interval, the mode
        still says "every three minutes" while the pages are in fact seen far
        less often — and nothing else in the service would ever mention it.
        Logged only when the number changes, not on every cycle.
        """
        if teams == self._warned_team_count:
            return
        self._warned_team_count = teams
        sweep = teams * HARD_MIN_REQUEST_INTERVAL_SECONDS
        wanted = self.config.interval_for("prematch")
        if sweep > wanted:
            log.warning(
                "%d teams tracked: one sweep of the schedule takes at least "
                "%.0f s, more than the %d s of pre-match mode. The pages are "
                "being read less often than the mode claims — drop teams or "
                "accept the lag; the rate ceiling does not move.",
                teams, sweep, wanted)

    async def poll_once(self) -> List[Event]:
        """Poll the schedule of EVERY tracked team.

        Different teams' pages are independent sources: a failure on one must
        not get in the way of the others, so errors are collected and the loop
        goes on.
        """
        teams = self.storage.tracked_teams()
        if not teams:
            log.warning("no tracked teams configured")
            return []
        self._warn_if_too_many(len(teams))

        produced: List[Event] = []
        failures: List[str] = []
        for team in teams:
            try:
                produced.extend(await self._poll_team(team))
            except (SourceRejected, SourceUnavailable) as exc:
                failures.append(f"{team['name']}: {exc}")
            except ParseError as exc:
                failures.append(f"{team['name']}: could not parse the page: {exc}")

        if failures and len(failures) == len(teams):
            # Not a single team could be read — that is a source failure, not
            # an individual mishap.
            produced.extend(self._handle_failure("; ".join(failures)))
        elif failures:
            log.error("some teams could not be read: %s", "; ".join(failures))
        else:
            self.storage.set_meta(LAST_POLL_KEY, iso(utcnow()))
            self.storage.set_meta(LAST_ERROR_KEY, "")
            self.http.consecutive_failures = 0
            for event in self.watchdog.report_success("schedule"):
                self.notifier.enqueue(event)
                produced.append(event)
        return produced

    async def _poll_team(self, team) -> List[Event]:
        url = f"{HLTV_BASE}/team/{team['team_id']}/{team['slug']}"
        html = await self.http.get_text(url)
        try:
            entries = team_page.parse(html, team["team_id"])
        except ParseError:
            # Zero matches on an HTTP 200 is almost certainly a markup change.
            # Treat it as a source failure, not as "there are no matches".
            self.storage.log_raw("team_page", url, "200/parse-error", html[:20000],
                                 self.config.raw_log_days)
            raise

        events = self.machine.apply(entries, team["team_id"])
        for event in events:
            self.notifier.enqueue(event)
        log.info("schedule of %s: %d matches, %d upcoming, %d events",
                 team["name"], len(entries), len(team_page.upcoming(entries)), len(events))
        return events

    # ------------------------------------------------------------------

    def _handle_failure(self, detail: str) -> List[Event]:
        """The watchdog decides about the alarm: it depends not on the number of
        attempts but on how long we have been blind and what is at risk now."""
        self.storage.set_meta(LAST_ERROR_KEY, detail)
        log.error("schedule polling failed: %s", detail)
        events = self.watchdog.report_failure("schedule", detail)
        for event in events:
            self.notifier.enqueue(event)
        return events
