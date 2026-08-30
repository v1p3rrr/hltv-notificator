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


# ---------------------------------------------------------------- адрес матча


def build(href: str) -> str:
    """Одна строка расписания с подставленной ссылкой."""
    html = f'''<table class="match-table"><tr class="event-header-cell">Ev</tr>
    <tr class="team-row">
      <td class="date-cell"><span data-unix="1790000000000"></span></td>
      <a class="team-name team-1" href="/team/12857/forze-reload">FORZE Reload</a>
      <a class="team-name team-2" href="/team/1/other">Other</a>
      <div class="score-cell"><span class="score">-</span><span class="score">-</span></div>
      <a class="matchpage-button" href="{href}">M</a>
    </tr></table>'''
    return team_page.parse(html, 12857)[0].url


def test_match_url_is_built_from_the_id_not_from_the_href():
    """Склейка базы с href была дырой: HLTV_BASE не кончается слэшем, поэтому
    href с собаки уводил запрос на чужой хост, а `www.hltv.org` становился
    всего лишь userinfo. Проверено на живом libcurl — он шёл именно туда."""
    assert build("/matches/2397091/color-vs-forze-reload") == \
        "https://www.hltv.org/matches/2397091/color-vs-forze-reload"


@pytest.mark.parametrize("href", [
    "@10.0.0.1:8080/matches/2397091/x",           # хост через userinfo
    "@192.168.1.1/matches/2397091/x",             # соседнее устройство в LAN
    ".evil.example/matches/2397091/x",            # продолжение домена, без собаки
    "//evil.example/matches/2397091/x",           # протокол-относительный адрес
    "https://evil.example/matches/2397091/x",     # абсолютный чужой адрес
])
def test_hostile_href_cannot_move_the_host(href):
    from hltv_notify.config import url_allowed

    url = build(href)
    assert url_allowed(url), f"адрес увёл на чужой хост: {url}"
    assert url.startswith("https://www.hltv.org/matches/2397091/")


def test_slug_is_limited_to_harmless_characters():
    """Хвост берётся из href только ради читаемости и только безобидный."""
    assert build("/matches/2397091/x?y=1#z") == "https://www.hltv.org/matches/2397091/x"
    assert build("/matches/2397091/") == "https://www.hltv.org/matches/2397091/-"
