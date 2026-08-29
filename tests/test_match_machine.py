"""Переходы по странице матча: E4, E7, детект зависания и дедупликация."""

from datetime import datetime, timedelta, timezone

from conftest import FIXTURES, TEAM_ID
from hltv_notify.models import MatchState
from hltv_notify.sources import match_page
from hltv_notify.sources.match_page import MapLine, MatchObservation
from hltv_notify.state.db import utcnow
from hltv_notify.state.match_machine import MatchMachine

MATCH_ID = 555


def add_match(storage, match_id=MATCH_ID):
    storage.upsert_match(
        match_id=match_id, opponent_id=13973, opponent_name="Color",
        event_name="Test Event", start_utc=utcnow(),
        url=f"https://www.hltv.org/matches/{match_id}/x",
        snapshot={}, snapshot_hash="x",
    )


def observe(status, maps, *, match_id=MATCH_ID):
    """Наша команда — team2 (справа), как в реальном матче 2397053."""
    return MatchObservation(
        match_id=match_id, status=status,
        start_utc=datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc),
        event_name="Test Event", event_id=1, best_of=3,
        team1_id=13973, team1_name="Color",
        team2_id=TEAM_ID, team2_name="FORZE Reload",
        maps=maps, scorebot_id=match_id if status == "live" else None,
    )


def maps(*scores):
    """scores — пары (левый, правый) или None для несыгранной карты."""
    lines = []
    for number, score in enumerate(scores, start=1):
        left, right, halves = (None, None, None) if score is None else score
        lines.append(MapLine(number=number, name=f"Map{number}",
                             score_left=left, score_right=right, halves=halves))
    return lines


def test_e4_on_transition_to_live(storage, config):
    add_match(storage)
    m = MatchMachine(storage, config)
    events = m.apply(observe("live", maps(None, None, None)))
    assert [e.type for e in events] == ["E4"]
    assert events[0].idempotency_key == "E4:555:started"
    assert storage.get_state(MATCH_ID)["state"] == MatchState.LIVE


def test_e4_not_repeated_on_every_poll(storage, config):
    """Страница опрашивается раз в минуту и всё это время говорит LIVE."""
    add_match(storage)
    m = MatchMachine(storage, config)
    m.apply(observe("live", maps(None, None, None)))
    again = m.apply(observe("live", maps((13, 10, "( 8 : 4 ; 5 : 6 )"), None, None)))
    assert [e.type for e in again] == []


def test_e7_with_series_score(storage, config):
    add_match(storage)
    m = MatchMachine(storage, config)
    m.apply(observe("live", maps(None, None, None)))
    events = m.apply(observe("over", maps(
        (10, 13, "( 5 : 7 ; 8 : 3 )"), (8, 13, "( 4 : 8 ; 4 : 5 )"), None)))
    assert [e.type for e in events] == ["E7"]
    e7 = events[0]
    # мы справа: 13 и 13 наши
    assert (e7.payload["series_team"], e7.payload["series_opponent"]) == (2, 0)
    assert e7.payload["won"] is True
    assert e7.idempotency_key == "E7:555:finished:2-0"
    assert len(e7.payload["maps"]) == 2
    assert storage.get_state(MATCH_ID)["state"] == MatchState.FINISHED


def test_e7_not_repeated(storage, config):
    add_match(storage)
    m = MatchMachine(storage, config)
    final = observe("over", maps((10, 13, None), (8, 13, None), None))
    m.apply(final)
    assert m.apply(final) == []


def test_no_e4_if_match_discovered_already_over(storage, config):
    """Слать «матч начался» про доигранный матч бессмысленно — сразу итог."""
    add_match(storage)
    m = MatchMachine(storage, config)
    events = m.apply(observe("over", maps((10, 13, None), (8, 13, None), None)))
    assert [e.type for e in events] == ["E7"]


def test_decider_not_played_is_not_counted(storage, config):
    """BO3 закончился 2:0 — третья карта осталась с прочерком."""
    add_match(storage)
    m = MatchMachine(storage, config)
    events = m.apply(observe("over", maps((10, 13, None), (8, 13, None), None)))
    assert len(storage.map_results(MATCH_ID)) == 2


def test_overtime_detected_by_halves_not_by_score(storage, config):
    """Регламент овертаймов различается между турнирами, поэтому считаем
    половины, а не раунды."""
    add_match(storage)
    m = MatchMachine(storage, config)
    m.apply(observe("over", maps((16, 19, "( 5 : 7 ; 7 : 5 ; 4 : 7 )"), None, None)))
    row = storage.map_results(MATCH_ID)[0]
    assert row["overtime"] == 1
    assert (row["score_team"], row["score_opponent"]) == (19, 16)


def test_stall_reported_only_after_threshold(storage, config):
    add_match(storage)
    m = MatchMachine(storage, config)
    frozen = observe("live", maps((5, 7, None), None, None))
    now = utcnow()

    assert [e.type for e in m.apply(frozen, now=now)] == ["E4"]
    assert m.apply(frozen, now=now + timedelta(minutes=5)) == []

    late = now + timedelta(minutes=config.stale_minutes + 1)
    events = m.apply(frozen, now=late)
    assert [e.type for e in events] == ["E8"]
    assert "stale" in events[0].idempotency_key


def test_progress_resets_the_stall_timer(storage, config):
    """Технические паузы бывают долгими, но пока счёт идёт — это не зависание."""
    add_match(storage)
    m = MatchMachine(storage, config)
    now = utcnow()
    m.apply(observe("live", maps((5, 7, None), None, None)), now=now)
    moved = now + timedelta(minutes=config.stale_minutes - 1)
    m.apply(observe("live", maps((6, 7, None), None, None)), now=moved)
    events = m.apply(observe("live", maps((6, 7, None), None, None)),
                     now=moved + timedelta(minutes=config.stale_minutes - 1))
    assert events == []


def test_observation_without_our_team_is_dropped(storage, config):
    """Не та страница или сменившаяся разметка не должны записать чужой счёт."""
    add_match(storage)
    m = MatchMachine(storage, config)
    alien = MatchObservation(
        match_id=MATCH_ID, status="live", start_utc=None, event_name="", event_id=None,
        best_of=3, team1_id=1, team1_name="A", team2_id=2, team2_name="B",
        maps=maps((13, 10, None)), scorebot_id=None)
    assert m.apply(alien) == []
    assert storage.get_state(MATCH_ID) is None


def test_real_fixtures_drive_a_full_match(storage, config):
    """Прогон по настоящим страницам: live → over даёт ровно E4 и E7."""
    add_match(storage, 2397047)
    m = MatchMachine(storage, config)
    live = match_page.parse((FIXTURES / "match-2397053-live.html").read_text(encoding="utf-8"),
                            2397047)
    over = match_page.parse((FIXTURES / "match-2397047-finished.html").read_text(encoding="utf-8"),
                            2397047)
    produced = [e.type for e in m.apply(live)] + [e.type for e in m.apply(over)]
    assert produced == ["E4", "E7"]
