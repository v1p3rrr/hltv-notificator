"""The match page and the live feed on ONE match.

Both machines are covered by tests individually, and individually both behaved
correctly. Two defects lived precisely at the seam: the machines shared one
state field and drew conclusions about their own history from it. This file
covers the seam.
"""

from datetime import timedelta

import pytest

from conftest import FIXTURES, TEAM_ID
from hltv_notify.sources import match_page
from hltv_notify.sources.scorebot import LiveFrame
from hltv_notify.state.db import Storage, utcnow
from hltv_notify.state.live_machine import LiveMachine
from hltv_notify.state.match_machine import MatchMachine

MATCH_ID = 2397053
FOE_ID = 13973


@pytest.fixture()
def match(storage):
    storage.upsert_match(
        match_id=MATCH_ID, opponent_id=FOE_ID, opponent_name="Color",
        event_name="GLuck Qualifier", start_utc=utcnow() - timedelta(minutes=30),
        url="https://www.hltv.org/matches/2397053/x", snapshot={}, snapshot_hash="h")
    storage.set_map_lineup(MATCH_ID, ["Mirage", "Dust2", "Ancient"])
    return storage


def page(name: str):
    return match_page.parse((FIXTURES / name).read_text(encoding="utf-8"), MATCH_ID)


def frame(map_name, *, ours=0, theirs=0, rnd=1, state="started", live=True):
    return LiveFrame(
        map_name=map_name, current_round=rnd, round_state=state, live=live,
        ct_team_id=TEAM_ID, ct_team_name="FORZE Reload", ct_score=ours,
        t_team_id=FOE_ID, t_team_name="Color", t_score=theirs,
        regulation=12, overtime=3)


# ----------------------------------------------------------------------
# E5: the page must not swallow the start of a map
# ----------------------------------------------------------------------


def test_page_poll_does_not_swallow_map_start(match, config):
    """Page polling puts the UPCOMING map (the first unplayed one) into the
    state. The live machine must not read that as the map having already been."""
    MatchMachine(match, config).apply(page("match-2397053-live.html"))
    assert match.get_state(MATCH_ID)["current_map_name"] == "Dust2"

    events = LiveMachine(match, config).apply(MATCH_ID, frame("de_dust2", rnd=1))
    assert [e.type for e in events] == ["E5"]
    assert events[0].payload["map_name"] == "Dust2"


def test_map_start_survives_repeated_page_polls(match, config):
    """Page polling runs every minute for as long as the map lasts and must
    not break the marker for the start of the next one."""
    page_machine = MatchMachine(match, config)
    live_machine = LiveMachine(match, config)
    live = page("match-2397053-live.html")

    page_machine.apply(live)
    assert [e.type for e in live_machine.apply(MATCH_ID, frame("de_dust2", rnd=1))] == ["E5"]

    page_machine.apply(live)          # the page talks about Dust2 again
    later = live_machine.apply(MATCH_ID, frame("de_dust2", ours=5, theirs=3, rnd=9))
    assert [e.type for e in later] == []      # there must be no second E5


def test_feed_writes_do_not_reset_what_the_page_already_saw(match, config):
    """The live feed rewrites the state several times a second, and that must
    not return the page to the "I am seeing this match for the first time"
    position.

    The order here is exactly the production one: the live worker is only
    brought up for a match already marked LIVE, and it is the page that marks
    it — the same page that emits E4 at that moment. The feed cannot get ahead
    of it.
    """
    page_machine = MatchMachine(match, config)
    live = page("match-2397053-live.html")

    assert [e.type for e in page_machine.apply(live)] == ["E4"]
    seen_at = match.get_state(MATCH_ID)["page_seen_utc"]
    assert seen_at is not None

    live_machine = LiveMachine(match, config)
    for _ in range(5):
        live_machine.apply(MATCH_ID, frame("de_dust2", ours=3, theirs=2, rnd=6))

    # The marker survived the feed writing and was not reset to a new time.
    assert match.get_state(MATCH_ID)["page_seen_utc"] == seen_at
    # A repeat page poll does not emit E4 a second time.
    assert page_machine.apply(live) == []


# ----------------------------------------------------------------------
# E6: the page is obliged to back the feed up
# ----------------------------------------------------------------------


def test_page_reports_map_end_that_the_feed_missed(match, config):
    """The key property of "the feed decides, the page confirms": if the feed
    missed the end of a map (a reconnect, the pause after a 403), the page is
    obliged to report it. It used to stay silent the whole time the feed was
    connected."""
    page_machine = MatchMachine(match, config)

    # the page has already observed the match, the map is still running
    page_machine.apply(page("match-2397053-live.html"), feed_connected=True)

    # the feed is working and rewrites the state on every frame
    live_machine = LiveMachine(match, config)
    for _ in range(3):
        live_machine.apply(MATCH_ID, frame("de_dust2", ours=4, theirs=6, rnd=11))
    assert match.get_state(MATCH_ID)["last_source"] == "scorebot"

    # ...and missed the end of the map. The page sees it as played.
    events = page_machine.apply(page("match-2397047-finished.html"), feed_connected=True)
    assert "E6" in [e.type for e in events]


def test_first_page_observation_is_still_silent(match, config):
    """The flip side: if the page sees the match FOR THE FIRST TIME and the
    map is already played, that is not a transition but the state at the moment
    of meeting."""
    events = MatchMachine(match, config).apply(page("match-2397047-finished.html"))
    assert [e.type for e in events] == ["E7"]


def test_both_sources_on_the_same_map_end_give_one_event(match, config):
    """The feed and the page bring the same end of map. One notification."""
    from hltv_notify.notify.outbox import Notifier

    notifier = Notifier(match, config, telegram=None)
    page_machine = MatchMachine(match, config)
    live_machine = LiveMachine(match, config)

    page_machine.apply(page("match-2397053-live.html"), feed_connected=True)
    live_machine.apply(MATCH_ID, frame("de_dust2", rnd=1))

    # the feed settles the end of the map from the score
    for event in live_machine.apply(MATCH_ID, frame("de_dust2", ours=13, theirs=10, rnd=23)):
        notifier.enqueue(event)
    # then the page sees the same map as played
    for event in page_machine.apply(page("match-2397047-finished.html"), feed_connected=True):
        notifier.enqueue(event)

    keys = [row["idempotency_key"] for row in match.due_outbox(limit=50)]
    assert len([k for k in keys if k.startswith("E6:2397053:map:2")]) == 1
