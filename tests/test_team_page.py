"""Парсер страницы команды — на реальном HTML, сохранённом при разведке."""

from datetime import timezone

import pytest

from conftest import TEAM_ID
from hltv_notify.sources import team_page
from hltv_notify.sources.team_page import ParseError


def test_parses_upcoming_and_results(team_page_html):
    entries = team_page.parse(team_page_html, TEAM_ID)
    assert len(entries) == 18
    upcoming = team_page.upcoming(entries)
    assert [e.match_id for e in upcoming] == [2397053, 2397340]


def test_opponent_taken_by_id_not_by_position(team_page_html):
    entries = team_page.parse(team_page_html, TEAM_ID)
    color = next(e for e in entries if e.match_id == 2397053)
    assert color.opponent_id == 13973
    assert color.opponent_name == "Color"
    # своей команды среди соперников быть не может
    assert all(e.opponent_id != TEAM_ID for e in entries)


def test_start_time_is_utc_from_data_unix(team_page_html):
    entries = team_page.parse(team_page_html, TEAM_ID)
    match = next(e for e in entries if e.match_id == 2397053)
    assert match.start_utc.tzinfo is timezone.utc
    assert match.start_utc.isoformat() == "2026-08-29T09:05:00+00:00"


def test_score_is_from_our_perspective(team_page_html):
    entries = team_page.parse(team_page_html, TEAM_ID)
    lost = next(e for e in entries if e.match_id == 2397026)  # поражение от Nemiga
    assert (lost.score_team, lost.score_opponent) == (0, 2)
    won = next(e for e in entries if e.match_id == 2397337)  # победа над DONSTU
    assert (won.score_team, won.score_opponent) == (2, 0)
    assert won.finished and lost.finished


def test_redesign_looks_like_failure_not_empty_schedule():
    """Ноль матчей при валидном HTTP — это отказ источника, а не «матчей нет».
    Иначе редизайн сайта выглядел бы как пустое расписание и тихо молчал."""
    with pytest.raises(ParseError):
        team_page.parse("<html><body><p>redesigned</p></body></html>", TEAM_ID)


def test_hash_ignores_nothing_volatile(team_page_html):
    entries = team_page.parse(team_page_html, TEAM_ID)
    first = team_page.hash_of(team_page.snapshot_of(entries[0]))
    again = team_page.hash_of(team_page.snapshot_of(entries[0]))
    assert first == again
