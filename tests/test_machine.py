"""Машина состояний: E1-E3, пороги, дебаунс и дедупликация."""

from datetime import timedelta

from conftest import entry, later
from hltv_notify.models import MatchState
from hltv_notify.state.db import utcnow
from hltv_notify.state.machine import ScheduleMachine


TEAM_ID = 12857


def machine(storage, config) -> ScheduleMachine:
    storage.add_team(TEAM_ID, "forze-reload", "FORZE Reload")
    return ScheduleMachine(storage, config)


def bootstrap(m, entries=()):
    """Первый прогон всегда молчаливый — он только наполняет базу."""
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
    """«Новый матч» про уже доигранное — мусор."""
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
    """Сдвиг меньше порога принимается молча: дёргать пользователя из-за
    трёх минут — раздражать его."""
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
    assert m.apply([entry(1, start=moved)], TEAM_ID, now=now) == []           # окно открылось
    assert m.apply([entry(1, start=moved)], TEAM_ID, now=now + timedelta(minutes=5)) == []

    events = m.apply([entry(1, start=moved)], TEAM_ID,
                     now=now + timedelta(minutes=config.e2_debounce_minutes + 1))
    assert [e.type for e in events] == ["E2"]
    assert events[0].idempotency_key.startswith("E2:1:moved:")


def test_move_there_and_back_is_not_an_event(storage, config):
    """Перенос туда-обратно внутри окна не должен ничего порождать."""
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


def test_e3_when_future_match_disappears(storage, config):
    m = machine(storage, config)
    bootstrap(m, [entry(1, start=later(600)), entry(2, start=later(900))])
    events = m.apply([entry(1, start=later(600))], TEAM_ID)
    assert [e.type for e in events] == ["E3"]
    assert events[0].idempotency_key == "E3:2:cancelled"
    assert storage.get_state(2)["state"] == MatchState.CANCELLED


def test_no_e3_when_start_already_passed(storage, config):
    """Матч, чей старт уже прошёл, мог просто начаться. Решать это должен
    опрос страницы матча, а не догадка по расписанию."""
    m = machine(storage, config)
    bootstrap(m, [entry(1, start=later(600)), entry(2, start=later(-30))])
    events = m.apply([entry(1, start=later(600))], TEAM_ID)
    assert events == []
    assert storage.get_state(2)["state"] == MatchState.UNKNOWN


def test_placeholder_opponent_resolves_without_new_match(storage, config):
    """Появление реального соперника вместо «Winner of match X» — это не новый матч."""
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
