"""Парсер страницы матча — на трёх реальных фикстурах: до матча, live, после."""

from pathlib import Path

import pytest

from conftest import FIXTURES, TEAM_ID
from hltv_notify.sources import match_page
from hltv_notify.sources.match_page import ParseError


def load(name: str, match_id: int):
    return match_page.parse((FIXTURES / name).read_text(encoding="utf-8"), match_id)


@pytest.fixture()
def upcoming():
    return load("match-2397340-upcoming.html", 2397340)


@pytest.fixture()
def live():
    return load("match-2397053-live.html", 2397053)


@pytest.fixture()
def finished():
    return load("match-2397047-finished.html", 2397047)


def test_status_of_three_states(upcoming, live, finished):
    assert upcoming.status == match_page.STATUS_UPCOMING
    assert live.status == match_page.STATUS_LIVE
    assert finished.status == match_page.STATUS_OVER


def test_start_time_is_scoped_to_the_match(finished):
    """Без скоупа .timeAndEvent первым в DOM идёт виджет .fbw-vp-header-time
    с временем ЧУЖИХ матчей — на этой фикстуре их там два. Ошибка дала бы
    ложные E2 «время изменилось»."""
    assert finished.start_utc.isoformat() == "2026-08-27T18:45:00+00:00"


def test_our_side_is_found_by_id_either_way(live, finished):
    """В одном матче наша команда справа, в другом слева — ориентируемся по id."""
    assert live.our_side(TEAM_ID) == "right"
    assert finished.our_side(TEAM_ID) == "left"
    assert live.opponent(TEAM_ID) == (13973, "Color")
    assert finished.opponent(TEAM_ID) == (13924, "Black Phoenix")


def test_map_scores_are_oriented_to_our_team(live):
    first = live.maps[0]
    assert first.name == "Mirage"
    assert live.map_score(first, TEAM_ID) == (10, 13)   # мы справа, проиграли
    assert first.halves == "( 5 : 7 ; 8 : 3 )"


def test_series_score_is_counted_from_decided_maps(live, finished):
    """Готовый счёт серии страница показывает только у завершённого матча,
    поэтому во время игры он считается по решённым картам."""
    assert live.series_score(TEAM_ID) == (0, 1)
    assert finished.series_score(TEAM_ID) == (2, 0)


def test_undecided_maps_are_not_counted(finished):
    """Решающая карта не игралась и должна остаться нерешённой, а не 0:0."""
    decider = finished.maps[2]
    assert decider.name == "Nuke"
    assert decider.decided is False
    assert len(finished.decided_maps()) == 2


def test_maps_are_tba_before_veto(upcoming):
    assert [m.name for m in upcoming.maps] == ["TBA", "TBA", "TBA"]
    assert upcoming.decided_maps() == []


def test_best_of_and_event(live):
    assert live.best_of == 3
    assert live.event_id == 9349
    assert "GLuck" in live.event_name


def test_scorebot_id_only_on_live_page(upcoming, live, finished):
    """#scoreboardElement есть только пока матч идёт: подключиться к живому
    фиду задним числом нельзя."""
    assert live.scorebot_id == 2397053
    assert upcoming.scorebot_id is None
    assert finished.scorebot_id is None


def test_progress_signature_changes_with_score(live):
    signature = live.progress_signature(TEAM_ID)
    assert "Mirage:10-13" in signature
    assert signature == live.progress_signature(TEAM_ID)


def test_redesign_is_an_error_not_empty_data():
    with pytest.raises(ParseError):
        match_page.parse("<html><body>redesigned</body></html>", 1)
