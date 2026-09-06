"""Clutch detection against the two recorded matches.

A clutch cannot be reproduced on demand, so these are the only two real ones
this repository has — and they are the whole reason the algorithm is what it
is. Both dumps are replayed through `RoundTracker` exactly as the live worker
drives it, with the clutch bar at 1 and the multikill bar off, so that every
clutch in the recording shows up and nothing else does.

The four below were measured off the raw frames before any of this was written.
If a change to the tracker moves one of these numbers, the change is wrong
until proven otherwise: HLTV's own `oneOnXWins` says a round was won alone, and
the alive counts say against how many.
"""

from pathlib import Path

import pytest

from conftest import FIXTURES
from hltv_notify.models import Event
from hltv_notify.notify import format as fmt
from hltv_notify.replay import frames
from hltv_notify.state.highlights import RoundTracker
from hltv_notify.state.live_machine import normalize_map_name

FORZE = FIXTURES / "scorebot-2397053-forze.jsonl.gz"
BOUNDARY = FIXTURES / "scorebot-2396936-map-boundary.jsonl.gz"

# (round, nick, opponents beaten, kills in that round)
EXPECTED = {
    FORZE.name: [
        (9, "Lack1", 1, 1),
        (13, "reyoz", 1, 1),
    ],
    BOUNDARY.name: [
        (10, "zeRRoFIX", 2, 3),
        (14, "cptkurtka023", 1, 2),
    ],
}


def clutches(path: Path) -> list:
    """Every clutch in a recording, both sides of it.

    One tracker per side, driven the way the live machine drives it — our
    players and theirs, taken from the frame by team id so the swap at the
    break cannot turn them around.
    """
    found = []
    trackers = {}
    for frame in frames(path):
        for team_id in (frame.ct_team_id, frame.t_team_id):
            if team_id is None:
                continue
            tracker = trackers.setdefault(
                team_id, RoundTracker(multikill=0, clutch=1))
            for highlight in tracker.observe(
                    normalize_map_name(frame.map_name), frame.current_round,
                    frame.round_state, frame.our_players(team_id),
                    frame.their_players(team_id)):
                found.append((frame.current_round, highlight.player.nick,
                              highlight.clutch_against, highlight.kills))
    return found


@pytest.mark.parametrize("path", [FORZE, BOUNDARY], ids=lambda p: p.name)
def test_the_recorded_clutches_are_found_and_sized(path):
    assert clutches(path) == EXPECTED[path.name]


@pytest.mark.parametrize("path", [FORZE, BOUNDARY], ids=lambda p: p.name)
def test_no_clutch_is_invented(path):
    """Every one reported must name a real number of opponents. A 1v0 is what
    a missed standoff would look like, and it must never be printed."""
    assert all(against >= 1 for _, _, against, _ in clutches(path))


def test_the_half_time_swap_forges_nothing():
    """During `ended` at half time the CT and TERRORIST arrays swap under us and
    the alive counts pass through (1, 1). The forze recording crosses a half,
    so it would show up here as a third clutch in round 12."""
    assert [round_number for round_number, *_ in clutches(FORZE)] == [9, 13]


def test_a_frame_without_advanced_stats_does_not_break_the_parse():
    """`advancedStats` is absent from 1176 of the 21126 player entries in the
    map-boundary recording, all of them in the warmup of a fresh map."""
    seen = [p for frame in frames(BOUNDARY)
            for p in frame.ct_players + frame.t_players]
    assert seen, "the recording should contain players"
    assert all(isinstance(p.clutches, int) for p in seen)
    assert any(p.clutches == 0 for p in seen)


def test_the_recordings_really_do_contain_a_combined_round():
    """zeRRoFIX took his 1v2 with three kills — the case the feature was asked
    for, and the reason a clutch and a multikill cannot be separate messages."""
    combined = [one for one in clutches(BOUNDARY) if one[3] >= 3]
    assert combined == [(10, "zeRRoFIX", 2, 3)]


# --------------------------------------------------------------------------
# What the message says.

def message(kills, against, *, nick="donk"):
    event = Event(type="E15" if against else "E9",
                  idempotency_key="k", match_id=1,
                  payload={"nick": nick, "kills": kills, "clutch_against": against,
                           "map_name": "Mirage", "round": 12,
                           "score_team": 7, "score_opponent": 5,
                           "team_name": "Natus Vincere", "team_id": 1,
                           "opponent": "FaZe", "opponent_id": 2,
                           "event_name": "IEM Katowice",
                           "url": "https://www.hltv.org/matches/1/x", "streams": []})
    return fmt.render(event, team_name="NAVI", tz_name="UTC")


@pytest.mark.parametrize("kills, against, headline", [
    (4, 0, "donk — 4k round"),          # a plain multikill reads as it always did
    (5, 0, "donk — ACE"),
    (1, 3, "donk — clutch 1v3"),        # one kill, three beaten — still a clutch
    (0, 2, "donk — clutch 1v2"),        # taken on the bomb or the clock
    (3, 2, "donk — 3k, clutch 1v2"),
    (4, 3, "donk — 4k, clutch 1v3"),    # the case the feature was asked for
    (5, 4, "donk — ACE, clutch 1v4"),
])
def test_the_headline_names_everything_that_happened(kills, against, headline):
    assert f"<b>{headline}</b>" in message(kills, against)


def test_a_clutch_message_still_carries_the_round_and_the_score():
    text = message(3, 2)
    assert "Mirage, round 12 · score 7:5" in text
    assert "Natus Vincere — FaZe" in text
    assert "Watch the match" in text


def test_a_nick_off_a_web_page_is_escaped():
    """Nicks come from the feed and go straight into HTML. An unescaped one is
    a 400 from Telegram and a message that never arrives."""
    assert "<script>" not in message(4, 0, nick="<script>")
    assert "&lt;script&gt;" in message(4, 0, nick="<script>")


def test_no_message_shape_carries_a_tag_telegram_would_refuse():
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from test_bot import unsupported_tags

    for kills in range(0, 6):
        for against in range(0, 6):
            assert unsupported_tags(message(kills, against)) == []
