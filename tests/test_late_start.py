"""The minutes around a start: a match late for its slot, and a move made then.

HLTV moves a match in exactly those minutes — before the slot and after it —
and three things went wrong there at once: the match page was read on the
three-minute pre-match cadence, the team page (which alone decides E2) had to
come round on its own cycle to notice the move, and a match past its time and
not LIVE was in neither `/next` nor `/live`. The bot half is in test_bot.py.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from conftest import FIXTURES, later
from hltv_notify.match_poller import DUE_LEAD_MINUTES, MatchPoller
from hltv_notify.models import MatchState
from hltv_notify.scheduler import SchedulePoller
from hltv_notify.state.db import iso

TEAM_ID = 12857
UPCOMING_ID = 2397340     # FORZE Reload vs ex-RUSTEC, the page says 15:00 UTC
PAGE_START = datetime(2026, 8, 29, 15, 0, tzinfo=timezone.utc)
LIVE_ID = 2397053


class FakeHttp:
    def __init__(self, html: str):
        self.html = html
        self.requests = 0

    async def get_text(self, url, **_):
        self.requests += 1
        return self.html


class FakeNotifier:
    def __init__(self):
        self.events = []

    def enqueue(self, event):
        self.events.append(event)


class FakeSupervisor:
    def __init__(self, connected=()):
        self._connected = {one: True for one in connected}
        self.any_connected = bool(self._connected)

    def connected_matches(self):
        return dict(self._connected)


def add_match(storage, match_id, start, state=MatchState.SCHEDULED, team_id=TEAM_ID):
    storage.upsert_match(
        match_id=match_id, team_id=team_id, opponent_id=13901, opponent_name="ex-RUSTEC",
        event_name="Test Event", start_utc=start,
        url=f"https://www.hltv.org/matches/{match_id}/x", snapshot={}, snapshot_hash="h")
    storage.link_match_team(match_id, team_id)
    storage.set_state(match_id, state, source="team_page")


def page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------- the window


def test_the_window_reaches_ahead_only_when_asked(storage):
    """The schedule keeps its old question — "past the slot" — and the match
    page asks the wider one, "about to start or past it"."""
    add_match(storage, 1, later(3))
    assert storage.matches_awaiting_start() == []
    assert [r["match_id"] for r in storage.matches_awaiting_start(
        ahead_minutes=DUE_LEAD_MINUTES)] == [1]


def test_the_window_reads_the_pending_time(storage):
    """During a debounce the confirmed time is stale by definition."""
    add_match(storage, 1, later(20))
    storage.set_pending_start(1, later(-2), later(-4))
    assert [r["match_id"] for r in storage.matches_awaiting_start()] == [1]


# ---------------------------------------------------------------- the cadence


def poller(storage, config, supervisor=None, recheck=None, html=""):
    return MatchPoller(storage, config, FakeHttp(html), FakeNotifier(),
                       supervisor, recheck_schedule=recheck)


@pytest.mark.parametrize("minutes, mode", [
    (20, "prematch"),     # inside the pre-match window, not yet close
    (3, "due"),           # a few minutes before the slot
    (-10, "due"),         # late for it
    (-90, "prematch"),    # past the grace: it may never happen at all
])
def test_the_page_is_read_like_a_live_one_around_the_start(storage, config, minutes, mode):
    add_match(storage, 1, later(minutes))
    one = poller(storage, config)
    assert one._mode_for(one.active()) == mode


def test_the_due_cadence_is_the_live_one(config):
    assert config.interval_for("due") == config.interval_for("live")


def test_a_match_that_started_is_no_longer_due(storage, config):
    add_match(storage, 1, later(-10), state=MatchState.LIVE)
    one = poller(storage, config, FakeSupervisor(connected=[1]))
    one.live_feed_active = True
    assert one._mode_for(one.active()) == "live_with_feed"


def test_a_second_match_about_to_start_beats_the_first_ones_feed(storage, config):
    """One cycle polls every row, so the slowest-mode-wins would read the
    second team's page every five minutes through its start."""
    add_match(storage, 1, later(-40), state=MatchState.LIVE)
    add_match(storage, 2, later(2), team_id=4494)
    one = poller(storage, config, FakeSupervisor(connected=[1]))
    one.live_feed_active = True
    assert one._mode_for(one.active()) == "due"


def test_a_live_match_without_a_feed_stays_live(storage, config):
    add_match(storage, 1, later(-10), state=MatchState.LIVE)
    one = poller(storage, config)
    assert one._mode_for(one.active()) == "live"


# ---------------------------------------------------------------- the recheck


class Recheck:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1


def poll(one, match_id=UPCOMING_ID):
    return asyncio.run(one._poll_match(one.storage.get_match(match_id)))


def test_a_new_time_on_the_match_page_asks_the_schedule_to_look_now(storage, config):
    """18:45 moved to 18:55 was on HLTV at 18:46 and reported at 18:49: the
    team page only came round on its three-minute cycle. The match page is read
    every minute then, so it is the one that sees the move first."""
    add_match(storage, UPCOMING_ID, datetime(2026, 8, 29, 14, 45, tzinfo=timezone.utc))
    recheck = Recheck()
    one = poller(storage, config, recheck=recheck, html=page("match-2397340-upcoming.html"))
    poll(one)
    assert recheck.calls == 1


def test_the_page_never_moves_the_start_itself(storage, config):
    """E2 and the stored start are the schedule machine's alone: a second
    writer would flip the time back and forth whenever the two pages disagree
    for a poll. A wrong read here costs one request, never a false E2."""
    old = datetime(2026, 8, 29, 14, 45, tzinfo=timezone.utc)
    add_match(storage, UPCOMING_ID, old)
    one = poller(storage, config, recheck=Recheck(),
                 html=page("match-2397340-upcoming.html"))
    events = poll(one)
    assert storage.get_match(UPCOMING_ID)["start_utc"] == iso(old)
    assert storage.get_state(UPCOMING_ID)["pending_start_utc"] is None
    assert "E2" not in [event.type for event in events]


def test_a_lagging_team_page_is_asked_once_per_time(storage, config):
    """If the team page has not caught up, asking again every minute would
    turn the schedule into a per-minute sweep for as long as it lags."""
    add_match(storage, UPCOMING_ID, datetime(2026, 8, 29, 14, 45, tzinfo=timezone.utc))
    recheck = Recheck()
    one = poller(storage, config, recheck=recheck, html=page("match-2397340-upcoming.html"))
    poll(one)
    poll(one)
    poll(one)
    assert recheck.calls == 1


def test_agreement_resets_the_memo(storage, config):
    """Once the schedule agrees, a LATER move is a new question."""
    add_match(storage, UPCOMING_ID, datetime(2026, 8, 29, 14, 45, tzinfo=timezone.utc))
    recheck = Recheck()
    one = poller(storage, config, recheck=recheck, html=page("match-2397340-upcoming.html"))
    poll(one)
    add_match(storage, UPCOMING_ID, PAGE_START)          # the schedule caught up
    poll(one)
    assert recheck.calls == 1
    add_match(storage, UPCOMING_ID, datetime(2026, 8, 29, 14, 30, tzinfo=timezone.utc))
    poll(one)
    assert recheck.calls == 2


def test_nothing_to_ask_when_the_times_agree(storage, config):
    add_match(storage, UPCOMING_ID, PAGE_START)
    recheck = Recheck()
    poll(poller(storage, config, recheck=recheck, html=page("match-2397340-upcoming.html")))
    assert recheck.calls == 0


def test_a_debounced_move_already_counts_as_known(storage, config):
    """The pending time is the newest the schedule knows; the page agreeing
    with it is not news."""
    add_match(storage, UPCOMING_ID, datetime(2026, 8, 29, 13, 0, tzinfo=timezone.utc))
    storage.set_pending_start(UPCOMING_ID, PAGE_START, later(-1))
    recheck = Recheck()
    poll(poller(storage, config, recheck=recheck, html=page("match-2397340-upcoming.html")))
    assert recheck.calls == 0


def test_a_running_match_is_not_rechecked(storage, config):
    """A move after the start is not news."""
    add_match(storage, LIVE_ID, datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc))
    recheck = Recheck()
    poll(poller(storage, config, recheck=recheck, html=page("match-2397053-live.html")),
         LIVE_ID)
    assert recheck.calls == 0


def test_somebody_elses_page_is_not_believed(storage, config):
    """The machine discards a page our team is not on; its time goes with it."""
    add_match(storage, UPCOMING_ID, datetime(2026, 8, 29, 14, 45, tzinfo=timezone.utc),
              team_id=4494)
    recheck = Recheck()
    poll(poller(storage, config, recheck=recheck, html=page("match-2397340-upcoming.html")))
    assert recheck.calls == 0


def test_the_recheck_is_the_schedules_out_of_turn_poll(storage, config):
    """The seam as `__main__` wires it: the request lands on the flag the
    schedule loop waits on."""
    add_match(storage, UPCOMING_ID, datetime(2026, 8, 29, 14, 45, tzinfo=timezone.utc))
    schedule = SchedulePoller(storage, config, http=None, notifier=None)
    one = poller(storage, config, recheck=schedule.request_poll,
                 html=page("match-2397340-upcoming.html"))
    assert not schedule._force.is_set()
    poll(one)
    assert schedule._force.is_set()
