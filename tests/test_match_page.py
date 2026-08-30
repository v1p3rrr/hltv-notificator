"""The match-page parser, on three real fixtures: before, live, and after."""

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
    """Without the .timeAndEvent scope, the first thing in the DOM is the
    .fbw-vp-header-time widget carrying OTHER matches' times — two of them on
    this fixture. The mistake would produce false "time changed" E2 events."""
    assert finished.start_utc.isoformat() == "2026-08-27T18:45:00+00:00"


def test_our_side_is_found_by_id_either_way(live, finished):
    """In one match our team is on the right, in another on the left — we go
    by id."""
    assert live.our_side(TEAM_ID) == "right"
    assert finished.our_side(TEAM_ID) == "left"
    assert live.opponent(TEAM_ID) == (13973, "Color")
    assert finished.opponent(TEAM_ID) == (13924, "Black Phoenix")


def test_map_scores_are_oriented_to_our_team(live):
    first = live.maps[0]
    assert first.name == "Mirage"
    assert live.map_score(first, TEAM_ID) == (10, 13)   # we are on the right, we lost
    assert first.halves == "( 5 : 7 ; 8 : 3 )"


def test_series_score_is_counted_from_decided_maps(live, finished):
    """The page shows a ready-made series score only for a finished match, so
    during play it is computed from the decided maps."""
    assert live.series_score(TEAM_ID) == (0, 1)
    assert finished.series_score(TEAM_ID) == (2, 0)


def test_undecided_maps_are_not_counted(finished):
    """The decider was not played and must stay undecided, not 0:0."""
    decider = finished.maps[2]
    assert decider.name == "Nuke"
    assert decider.has_score is False
    assert len(finished.final_maps()) == 2


def test_maps_are_tba_before_veto(upcoming):
    assert [m.name for m in upcoming.maps] == ["TBA", "TBA", "TBA"]
    assert upcoming.final_maps() == []


def test_best_of_and_event(live):
    assert live.best_of == 3
    assert live.event_id == 9349
    assert "GLuck" in live.event_name


def test_scorebot_id_only_on_live_page(upcoming, live, finished):
    """#scoreboardElement only exists while the match is running: you cannot
    connect to the live feed after the fact."""
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


# ----------------------------------------------------------------------
# Regression: a RUNNING map has a numeric score too
# ----------------------------------------------------------------------


def test_live_map_is_not_counted_as_played():
    """The fixture was captured from match 2397091 while the second map ran.

    The naive rule "there is a numeric score, so the map is played" would have
    called it finished at 5:7 (that was the current in-play score) and sent a
    wrong E6. What marks a finished map is the presence of a statistics record.
    """
    o = load("match-2397091-live-midmap.html", 2397091)
    assert o.status == match_page.STATUS_LIVE

    played, live, untouched = o.maps
    assert (played.name, played.score_left, played.score_right) == ("Nuke", 13, 9)
    assert played.has_stats is True
    assert o.is_final(played) is True

    assert (live.name, live.score_left, live.score_right) == ("Mirage", 5, 7)
    assert live.has_score is True          # there is a score...
    assert live.has_stats is False         # ...but the map is still running
    assert o.is_final(live) is False

    assert untouched.has_score is False
    assert o.live_map() is live
    assert [m.number for m in o.final_maps()] == [1]


def test_series_score_ignores_the_running_map():
    """The series score must not count a map that is still being played."""
    o = load("match-2397091-live-midmap.html", 2397091)
    assert o.series_score(12363) == (1, 0)      # Arcade only won Nuke
    assert o.series_score(11668) == (0, 1)
