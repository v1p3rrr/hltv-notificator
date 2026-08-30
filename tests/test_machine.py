CHAT = "555"

"""The state machine: E1-E3, thresholds, debounce and deduplication."""

from datetime import timedelta

from conftest import entry, later
from hltv_notify.models import MatchState
from hltv_notify.state.db import utcnow
from hltv_notify.state.machine import ScheduleMachine


TEAM_ID = 12857


def machine(storage, config) -> ScheduleMachine:
    storage.add_team(CHAT, TEAM_ID, "forze-reload", "FORZE Reload")
    return ScheduleMachine(storage, config)


def bootstrap(m, entries=()):
    """The first pass is always silent — it only fills the database."""
    return m.apply(list(entries), TEAM_ID)


def test_first_run_is_silent(storage, config):
    m = machine(storage, config)
    events = bootstrap(m, [entry(1, start=later(600)), entry(2, start=later(900))])
    assert events == []
    assert len(storage.all_matches()) == 2


def test_e1_for_match_appearing_after_bootstrap(storage, config):
    m = machine(storage, config)
    bootstrap(m, [entry(1, start=later(600))])
    events = m.apply([entry(1, start=later(600)), entry(2, start=later(900))], TEAM_ID)
    assert [e.type for e in events] == ["E1"]
    assert events[0].match_id == 2
    assert events[0].idempotency_key == "E1:2:new"


def test_finished_match_never_yields_e1(storage, config):
    """"New match" about something already played is noise."""
    m = machine(storage, config)
    bootstrap(m, [entry(1, start=later(600))])
    events = m.apply([entry(1, start=later(600)),
                      entry(9, start=later(-600), finished=True, score=(2, 0))], TEAM_ID)
    assert events == []


def test_same_schedule_twice_produces_no_new_events(storage, config):
    m = machine(storage, config)
    bootstrap(m, [entry(1, start=later(600))])
    schedule = [entry(1, start=later(600)), entry(2, start=later(900))]
    first = m.apply(schedule, TEAM_ID)
    second = m.apply(schedule, TEAM_ID)
    assert len(first) == 1
    assert second == []


def test_small_shift_is_swallowed(storage, config):
    """A shift below the threshold is accepted silently: pestering the user
    over three minutes is just irritating."""
    m = machine(storage, config)
    start = later(600)
    bootstrap(m, [entry(1, start=start)])
    events = m.apply([entry(1, start=start + timedelta(minutes=3))], TEAM_ID)
    assert events == []
    stored = storage.get_match(1)
    assert stored["start_utc"].startswith((start + timedelta(minutes=3)).isoformat()[:16])


def test_e2_waits_for_debounce_window(storage, config):
    m = machine(storage, config)
    start = later(600)
    bootstrap(m, [entry(1, start=start)])
    moved = start + timedelta(hours=2)

    now = utcnow()
    assert m.apply([entry(1, start=moved)], TEAM_ID, now=now) == []           # window opened
    assert m.apply([entry(1, start=moved)], TEAM_ID, now=now + timedelta(minutes=5)) == []

    events = m.apply([entry(1, start=moved)], TEAM_ID,
                     now=now + timedelta(minutes=config.e2_debounce_minutes + 1))
    assert [e.type for e in events] == ["E2"]
    assert events[0].idempotency_key.startswith("E2:1:moved:")


def test_move_there_and_back_is_not_an_event(storage, config):
    """Moving it there and back inside the window must produce nothing."""
    m = machine(storage, config)
    start = later(600)
    bootstrap(m, [entry(1, start=start)])
    now = utcnow()
    assert m.apply([entry(1, start=start + timedelta(hours=2))], TEAM_ID, now=now) == []
    assert m.apply([entry(1, start=start)], TEAM_ID, now=now + timedelta(minutes=2)) == []
    events = m.apply([entry(1, start=start)], TEAM_ID,
                     now=now + timedelta(minutes=config.e2_debounce_minutes + 5))
    assert events == []


def test_e2_key_depends_only_on_new_time(storage, config):
    m = machine(storage, config)
    start = later(600)
    bootstrap(m, [entry(1, start=start)])
    moved = start + timedelta(hours=2)
    now = utcnow()
    m.apply([entry(1, start=moved)], TEAM_ID, now=now)
    events = m.apply([entry(1, start=moved)], TEAM_ID,
                     now=now + timedelta(minutes=config.e2_debounce_minutes + 1))
    key = events[0].idempotency_key
    assert key == f"E2:1:moved:{moved.replace(microsecond=0).isoformat()}"


def test_e2_does_not_wait_when_the_match_is_about_to_start(storage, config):
    """The window never runs past the start.

    This is match 2397343: 18:00 moved to 18:20, noticed at 17:57. Waiting out
    the ten-minute window would have put the message nine minutes into the
    match — and in fact it never came at all, because the schedule had already
    dropped to idle by then.
    """
    m = machine(storage, config)
    start = later(3)
    bootstrap(m, [entry(1, start=start)])
    moved = start + timedelta(minutes=20)

    events = m.apply([entry(1, start=moved)], TEAM_ID)
    assert [e.type for e in events] == ["E2"]
    assert storage.get_match(1)["start_utc"].startswith(moved.isoformat()[:16])


def test_e2_is_not_sent_once_the_new_time_has_passed(storage, config):
    """A reschedule reported after the fact is not news, it is history."""
    m = machine(storage, config)
    start = later(-60)
    bootstrap(m, [entry(1, start=start)])
    moved = start + timedelta(minutes=20)          # still in the past

    assert m.apply([entry(1, start=moved)], TEAM_ID) == []
    # The time itself is accepted all the same — nothing is lost.
    assert storage.get_match(1)["start_utc"].startswith(moved.isoformat()[:16])


def test_a_pending_move_keeps_the_match_upcoming(storage, config):
    """Everything hangs off upcoming_matches: the polling cadence, the
    reminders, /next. Judged by the confirmed time, a match moved forward drops
    out of the list at its old start — and that is how the reschedule was
    lost."""
    m = machine(storage, config)
    start = later(20)
    bootstrap(m, [entry(1, start=start)])
    moved = start + timedelta(minutes=20)
    m.apply([entry(1, start=moved)], TEAM_ID)      # the window opens, no event yet

    after_the_old_start = start + timedelta(minutes=1)
    rows = storage.upcoming_matches(after_the_old_start)
    assert [row["match_id"] for row in rows] == [1]
    assert rows[0]["start_utc"].startswith(moved.isoformat()[:16])
    assert rows[0]["confirmed_start_utc"].startswith(start.isoformat()[:16])


def test_e3_when_future_match_disappears(storage, config):
    m = machine(storage, config)
    bootstrap(m, [entry(1, start=later(600)), entry(2, start=later(900))])
    events = m.apply([entry(1, start=later(600))], TEAM_ID)
    assert [e.type for e in events] == ["E3"]
    assert events[0].idempotency_key == "E3:2:cancelled"
    assert storage.get_state(2)["state"] == MatchState.CANCELLED


def test_no_e3_when_start_already_passed(storage, config):
    """A match whose start has passed may simply have begun. That is for the
    match-page polling to decide, not for a guess from the schedule."""
    m = machine(storage, config)
    bootstrap(m, [entry(1, start=later(600)), entry(2, start=later(-30))])
    events = m.apply([entry(1, start=later(600))], TEAM_ID)
    assert events == []
    assert storage.get_state(2)["state"] == MatchState.UNKNOWN


def test_placeholder_opponent_resolves_without_new_match(storage, config):
    """A real opponent replacing "Winner of match X" is not a new match."""
    m = machine(storage, config)
    bootstrap(m, [entry(1, start=later(600))])
    start = later(900)
    created = m.apply([entry(1, start=later(600)),
                       entry(7, start=start, opponent_id=None, opponent_name="Winner of match X")], TEAM_ID)
    assert [e.type for e in created] == ["E1"]
    assert created[0].payload["placeholder"] is True

    resolved = m.apply([entry(1, start=later(600)),
                        entry(7, start=start, opponent_id=13901, opponent_name="ex-RUSTEC")], TEAM_ID)
    assert resolved == []
    assert storage.get_match(7)["opponent_name"] == "ex-RUSTEC"


# ---------------------------------------------------------------- polling mode

def poller(storage, config):
    """current_mode only reads the database — the network is not involved."""
    from hltv_notify.scheduler import SchedulePoller
    return SchedulePoller(storage, config, http=None, notifier=None)


def test_a_match_past_its_start_keeps_the_frequent_polling(storage, config):
    """HLTV moves a match after its own slot has arrived as a matter of
    routine. Judged only by "is the start ahead", such a match is nobody's
    business and the schedule falls back to a poll every half hour."""
    m = machine(storage, config)
    bootstrap(m, [entry(1, start=later(-5))])
    assert poller(storage, config).current_mode() == "prematch"


def test_a_match_that_started_lets_the_schedule_rest(storage, config):
    m = machine(storage, config)
    bootstrap(m, [entry(1, start=later(-5))])
    storage.set_state(1, MatchState.LIVE, source="match_page")
    assert poller(storage, config).current_mode() == "idle"


def test_a_match_long_past_stops_holding_the_schedule(storage, config):
    """It may never have happened at all."""
    m = machine(storage, config)
    bootstrap(m, [entry(1, start=later(-180))])
    assert poller(storage, config).current_mode() == "idle"


def test_a_move_made_after_the_slot_passed_is_reported_at_once(storage, config):
    """The whole story: 18:00 comes, nothing starts, at 18:03 the page says
    18:15. The frequent polling is what sees it, and there is no time left to
    debounce, so the message goes out on the spot."""
    m = machine(storage, config)
    bootstrap(m, [entry(1, start=later(-3))])
    assert poller(storage, config).current_mode() == "prematch"

    events = m.apply([entry(1, start=later(15))], TEAM_ID)
    assert [e.type for e in events] == ["E2"]
    assert poller(storage, config).current_mode() == "prematch"
