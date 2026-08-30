"""The watchdog: telling you that notifications have stopped working.

There is exactly one point to it — if the service has gone blind anywhere, the
user must find out and go look at the match by hand. So the alarm is not
raised instantly (a short failure fixes itself through retries), but neither is
it raised "eventually".

The urgency depends on what is at stake right now:

* less than a minute to the match start, or someone is three rounds from
  winning the map, or an overtime is being played — no waiting, alarm after a
  minute;
* everything else — after `DEGRADED_ALERT_SECONDS` (5 minutes by default,
  configurable up to 10).

One alarm per failure: the idempotency key contains the moment the failure
started, so repeated checks of the same failure send nothing while a new
failure is reported afresh. Recovery is reported separately — so nobody has to
guess whether it passed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from .config import Config
from .models import Event, MatchState
from .scoring import rounds_to_win
from .state.db import Storage, iso, parse_iso, utcnow

log = logging.getLogger(__name__)

# The lower bound: even in the most urgent situation we allow a minute for
# retries, otherwise the alarm would fire on any single timeout.
URGENT_SECONDS = 60.0
# The upper bound of the setting: staying quiet for more than ten minutes is
# pointless, by then the match has already gone past.
MAX_ALERT_SECONDS = 600.0

# How many rounds to a win counts as "it is all about to be decided".
DECISIVE_ROUNDS = 3
# How long after the scheduled start a match still counts as "about to begin".
# Beyond that window a silent match stops being urgent: it may not have
# happened at all.
START_GRACE_MINUTES = 30

SUBSYSTEMS = {
    "schedule": "The schedule cannot be read",
    "match_page": "The match page cannot be read",
    "live_feed": "The live feed will not come up",
    "outbox": "Notifications are not reaching Telegram",
}


def _since_key(subsystem: str) -> str:
    return f"degraded_since:{subsystem}"


def _detail_key(subsystem: str) -> str:
    return f"degraded_detail:{subsystem}"


def _alerted_key(subsystem: str) -> str:
    """Whether an alarm was actually SENT about this failure.

    Not the same as "a failure was recorded". Both are needed because the two
    happen at different times: the countdown starts on the first failed
    attempt, while the alarm only goes out once the failure has held past the
    threshold — and it may never go out at all, because the next attempt only
    happens on the poller's next cycle. Seen in production twice: the live feed
    connected 0.7 s after the countdown began, the schedule recovered on its
    next attempt 35 minutes later, and both produced a "Recovered" for an
    outage nobody had ever been told about.
    """
    return f"degraded_alerted:{subsystem}"


class Watchdog:
    def __init__(self, storage: Storage, config: Config):
        self.storage = storage
        self.config = config

    # ------------------------------------------------------------------

    @property
    def normal_delay(self) -> float:
        return min(max(float(self.config.degraded_alert_seconds), URGENT_SECONDS),
                   MAX_ALERT_SECONDS)

    def urgency(self, now: Optional[datetime] = None) -> Tuple[float, str]:
        """How long before raising the alarm, and why exactly that long."""
        now = now or utcnow()

        for row in self.storage.active_matches(now):
            if row["state"] != MatchState.LIVE:
                continue
            reason = self._match_urgency(row["match_id"])
            if reason:
                return URGENT_SECONDS, reason

        # A window around the start, not just "before the start". The nastiest
        # case is a match that has ALREADY begun while we are blind and have
        # not yet realised it is running: that is when staying quiet for five
        # minutes costs the most.
        for row in self.storage.active_matches(now):
            # A running match does not belong here: that one we do see, and its
            # urgency has already been judged from the score above.
            if row["state"] in (MatchState.LIVE, MatchState.FINISHED):
                continue
            start = parse_iso(row["start_utc"])
            if start - now > timedelta(seconds=URGENT_SECONDS):
                continue
            if now - start > timedelta(minutes=START_GRACE_MINUTES):
                continue
            if start > now:
                return URGENT_SECONDS, "less than a minute to the match start"
            return URGENT_SECONDS, "the match should have started and we cannot see it"

        return self.normal_delay, "the match is not at a decisive stage"

    def _match_urgency(self, match_id: int) -> Optional[str]:
        """Urgency judged from the running map's score."""
        state = self.storage.get_state(match_id)
        if state is None or not state["current_map_score"]:
            return None
        try:
            ours, theirs = (int(part) for part in state["current_map_score"].split("-"))
        except (ValueError, AttributeError):
            return None

        regulation = state["regulation_rounds"] or 12
        overtime = state["overtime_rounds"] or 3

        # Overtime: both sides made it to the end of regulation.
        if ours >= regulation and theirs >= regulation:
            return f"an overtime is being played at {ours}:{theirs}"

        left = rounds_to_win(ours, theirs, regulation=regulation, overtime=overtime)
        if left <= DECISIVE_ROUNDS:
            return f"{left} round(s) to the end of the map at {ours}:{theirs}"
        return None

    # ------------------------------------------------------------------

    def report_failure(self, subsystem: str, detail: str,
                       now: Optional[datetime] = None,
                       since: Optional[datetime] = None) -> List[Event]:
        """The subsystem is not working. An alarm only once the failure has held.

        `since` is passed by whoever knows the REAL start of the failure. For
        the queue that is the creation time of the oldest stuck message: making
        it count from scratch would mean staying quiet for one more threshold
        on top of everything it has already waited.
        """
        now = now or utcnow()
        since_raw = self.storage.get_meta(_since_key(subsystem))
        if not since_raw:
            self.storage.set_meta(_since_key(subsystem), iso(since or now))
            self.storage.set_meta(_detail_key(subsystem), detail)
            log.warning("subsystem %s is not responding, the clock starts: %s",
                        subsystem, detail)
            since_raw = iso(since or now)
            if since is None:
                return []

        self.storage.set_meta(_detail_key(subsystem), detail)
        since = parse_iso(since_raw)
        delay, reason = self.urgency(now)
        broken_for = now - since
        if broken_for < timedelta(seconds=delay):
            return []

        minutes = max(1, int(broken_for.total_seconds() // 60))
        # From here on a "Recovered" is warranted: the alarm is going out.
        self.storage.set_meta(_alerted_key(subsystem), iso(since))
        return [Event(
            type="E8",
            # The key includes the moment the failure STARTED: one alarm per
            # failure, but a new failure is reported afresh.
            idempotency_key=f"E8:{subsystem}:down:{iso(since)}",
            match_id=None,
            payload={
                "reason": SUBSYSTEMS.get(subsystem, subsystem),
                "detail": (f"Broken for {minutes} min and did not fix itself. "
                           f"The threshold is {int(delay)} s because {reason}. {detail}"),
                "url": self._match_url(),
            },
        )]

    def report_success(self, subsystem: str,
                       now: Optional[datetime] = None) -> List[Event]:
        """The subsystem came back. If the failure was reported, report the end."""
        since_raw = self.storage.get_meta(_since_key(subsystem))
        if not since_raw:
            return []
        now = now or utcnow()
        alerted = self.storage.get_meta(_alerted_key(subsystem))
        self.storage.set_meta(_since_key(subsystem), "")
        self.storage.set_meta(_detail_key(subsystem), "")
        self.storage.set_meta(_alerted_key(subsystem), "")

        since = parse_iso(since_raw)
        broken_for = now - since
        # "Recovered" only makes sense after an alarm the owner actually saw.
        # Elapsed time is not the test: a subsystem is re-checked on the
        # poller's own cycle, so an outage of a second can look like a minute
        # and one of 35 minutes can pass without a single alarm.
        if not alerted:
            log.info("subsystem %s came back after %.0f s, no alarm had been "
                     "raised — staying quiet", subsystem, broken_for.total_seconds())
            return []

        minutes = max(1, int(broken_for.total_seconds() // 60))
        return [Event(
            type="E8R",
            idempotency_key=f"E8R:{subsystem}:up:{iso(since)}",
            match_id=None,
            payload={
                "reason": SUBSYSTEMS.get(subsystem, subsystem),
                "detail": f"Working again. The outage lasted {minutes} min.",
            },
        )]

    # ------------------------------------------------------------------

    def check_outbox(self, now: Optional[datetime] = None) -> List[Event]:
        """The queue is not draining — which means Telegram is not accepting.

        The alarm about it goes into that same stuck queue, and that is fine:
        it will get through when the connection returns, and until then it is
        visible in /status and in the logs. Staying quiet is not an option —
        otherwise nobody learns about the mute delivery.
        """
        now = now or utcnow()
        oldest = self.storage.oldest_pending_utc()
        if oldest is None:
            return self.report_success("outbox", now)
        stuck_for = now - parse_iso(oldest)
        delay, _ = self.urgency(now)
        if stuck_for < timedelta(seconds=delay):
            return []
        return self.report_failure(
            "outbox",
            f"the oldest message has been waiting {int(stuck_for.total_seconds() // 60)} min",
            now, since=parse_iso(oldest))

    def check_live_feed(self, connected: dict, now: Optional[datetime] = None) -> List[Event]:
        """A match is running and there is no feed. Page polling still works, so
        this is not a total loss — but E5, multikills and the speed of E6 are
        gone."""
        now = now or utcnow()
        live = [row for row in self.storage.active_matches(now)
                if row["state"] == MatchState.LIVE]
        if not live:
            return self.report_success("live_feed", now)
        if any(connected.get(row["match_id"]) for row in live):
            return self.report_success("live_feed", now)
        return self.report_failure(
            "live_feed", f"{len(live)} match(es) running, the feed is connected to none",
            now)

    def _match_url(self) -> str:
        """A link to a running match if there is one: so that from the alarm you
        can go and look at the score with your own eyes."""
        for row in self.storage.active_matches():
            if row["state"] == MatchState.LIVE:
                return row["url"]
        upcoming = self.storage.upcoming_matches()
        return upcoming[0]["url"] if upcoming else ""

    async def run(self, stop, notifier, interval: float = 60.0) -> None:
        """The periodic check of the thing nobody else checks.

        Schedule polling and match polling report their own failures. The
        sending queue does not: it can quietly pile up while Telegram refuses.
        """
        import asyncio

        while not stop.is_set():
            try:
                for event in self.check_outbox():
                    notifier.enqueue(event)
            except Exception:  # noqa: BLE001 - the watchdog is not allowed to die
                log.exception("watchdog failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    def degraded_subsystems(self) -> List[str]:
        return [name for name in SUBSYSTEMS
                if self.storage.get_meta(_since_key(name))]
