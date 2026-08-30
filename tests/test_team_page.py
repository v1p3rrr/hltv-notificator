"""The team-page parser, on real HTML saved during recon."""

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
    # our own team cannot appear among the opponents
    assert all(e.opponent_id != TEAM_ID for e in entries)


def test_start_time_is_utc_from_data_unix(team_page_html):
    entries = team_page.parse(team_page_html, TEAM_ID)
    match = next(e for e in entries if e.match_id == 2397053)
    assert match.start_utc.tzinfo is timezone.utc
    assert match.start_utc.isoformat() == "2026-08-29T09:05:00+00:00"


def test_score_is_from_our_perspective(team_page_html):
    entries = team_page.parse(team_page_html, TEAM_ID)
    lost = next(e for e in entries if e.match_id == 2397026)  # a loss to Nemiga
    assert (lost.score_team, lost.score_opponent) == (0, 2)
    won = next(e for e in entries if e.match_id == 2397337)  # a win over DONSTU
    assert (won.score_team, won.score_opponent) == (2, 0)
    assert won.finished and lost.finished


def test_redesign_looks_like_failure_not_empty_schedule():
    """Zero matches on a valid HTTP response is a source failure, not "no
    matches". Otherwise a site redesign would look like an empty schedule and
    quietly say nothing."""
    with pytest.raises(ParseError):
        team_page.parse("<html><body><p>redesigned</p></body></html>", TEAM_ID)


def test_hash_ignores_nothing_volatile(team_page_html):
    entries = team_page.parse(team_page_html, TEAM_ID)
    first = team_page.hash_of(team_page.snapshot_of(entries[0]))
    again = team_page.hash_of(team_page.snapshot_of(entries[0]))
    assert first == again


# ---------------------------------------------------------------- match address


def build(href: str) -> str:
    """A single schedule row with the given link substituted in."""
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
    """Concatenating the base with the href was a hole: HLTV_BASE does not end
    in a slash, so an href starting with an at-sign steered the request to a
    foreign host while `www.hltv.org` became mere userinfo. Verified against a
    live libcurl — it went exactly there."""
    assert build("/matches/2397091/color-vs-forze-reload") == \
        "https://www.hltv.org/matches/2397091/color-vs-forze-reload"


@pytest.mark.parametrize("href", [
    "@10.0.0.1:8080/matches/2397091/x",           # the host through userinfo
    "@192.168.1.1/matches/2397091/x",             # a neighbouring device on the LAN
    ".evil.example/matches/2397091/x",            # a domain continuation, no at-sign
    "//evil.example/matches/2397091/x",           # a protocol-relative address
    "https://evil.example/matches/2397091/x",     # an absolute foreign address
])
def test_hostile_href_cannot_move_the_host(href):
    from hltv_notify.config import url_allowed

    url = build(href)
    assert url_allowed(url), f"the address led to a foreign host: {url}"
    assert url.startswith("https://www.hltv.org/matches/2397091/")


def test_slug_is_limited_to_harmless_characters():
    """The tail is taken from the href only for readability, and only if it is
    harmless."""
    assert build("/matches/2397091/x?y=1#z") == "https://www.hltv.org/matches/2397091/x"
    assert build("/matches/2397091/") == "https://www.hltv.org/matches/2397091/-"
