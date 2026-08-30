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


# ----------------------------------------------------------------------
# The end of the match: the feed gives the speed, the page stays the truth
# ----------------------------------------------------------------------


def played_out(match, config):
    """Both maps of the finished fixture won through the feed."""
    live = LiveMachine(match, config)
    events = []
    for name, ours, theirs in (("de_mirage", 13, 10), ("de_dust2", 13, 10)):
        live.apply(MATCH_ID, frame(name, rnd=1))
        events += live.apply(MATCH_ID, frame(name, ours=ours, theirs=theirs, rnd=23))
    return events


def test_the_feed_finishes_the_match_without_waiting_for_the_page(match, config):
    """This is what the four-minute gap was: the feed knew at the winning round,
    the page only once HLTV flipped its status."""
    match.set_state(MATCH_ID, "LIVE", source="page")
    match.set_best_of(MATCH_ID, 3)
    events = played_out(match, config)
    assert [e.type for e in events] == ["E6", "E6", "E7"]
    assert events[-1].idempotency_key == f"E7:{MATCH_ID}:finished:2-0"


def test_the_page_adds_nothing_when_it_agrees(match, config):
    """The page produces the same key, so the unique index swallows its copy
    and the owner gets one message, not two."""
    from hltv_notify.notify.outbox import Notifier

    match.add_subscriber("1")
    match.add_team("1", TEAM_ID, "forze-reload", "FORZE Reload")
    match.link_match_team(MATCH_ID, TEAM_ID)
    match.set_state(MATCH_ID, "LIVE", source="page")
    match.set_best_of(MATCH_ID, 3)
    notifier = Notifier(match, config, telegram=None)

    for event in played_out(match, config):
        notifier.enqueue(event)
    before = [row["idempotency_key"] for row in match.conn.execute(
        "SELECT idempotency_key FROM sent_events WHERE event_type = 'E7'")]
    assert len(before) == 1

    for event in MatchMachine(match, config).apply(page("match-2397047-finished.html")):
        notifier.enqueue(event)
    after = [row["idempotency_key"] for row in match.conn.execute(
        "SELECT idempotency_key FROM sent_events WHERE event_type = 'E7'")]
    assert after == before


def test_a_disagreeing_page_says_it_is_a_correction(match, config):
    """If the page ends up with a different series score the key differs and the
    message goes out — but it must say it is a correction, otherwise it reads
    as a duplicate of what already arrived."""
    match.set_state(MATCH_ID, "LIVE", source="page")
    match.set_best_of(MATCH_ID, 3)

    # The feed saw one map and called the series 1-0 on a BO1-sized guess.
    live = LiveMachine(match, config)
    live.apply(MATCH_ID, frame("de_mirage", rnd=1))
    match.set_best_of(MATCH_ID, 1)
    finished = [e for e in live.apply(MATCH_ID, frame("de_mirage", ours=13, theirs=10, rnd=23))
                if e.type == "E7"]
    assert finished and finished[0].payload["corrected"] is False
    match.record_event(idempotency_key=finished[0].idempotency_key,
                       event_type="E7", match_id=MATCH_ID, body="x", chat_id="1")

    # The page sees both maps played, so its series score is 2-0 — a different
    # key, a real disagreement.
    events = MatchMachine(match, config).apply(page("match-2397047-finished.html"))
    e7 = [e for e in events if e.type == "E7"]
    assert e7, "the page must still report a result it does not agree with"
    assert e7[0].idempotency_key != finished[0].idempotency_key
    assert e7[0].payload["corrected"] is True


# ----------------------------------------------------------------------
# E4: the page raises LIVE during the warmup, the feed says when it is over
# ----------------------------------------------------------------------


def test_the_start_message_waits_for_the_first_round(match, config):
    """The page cannot tell a warmup from a game — it flips to LIVE when the
    teams connect to the server. The feed can, and it is the one that decides
    the moment."""
    page_machine = MatchMachine(match, config)
    live = LiveMachine(match, config)
    now = utcnow()

    # The feed connects first and reports a warmup.
    live.apply(MATCH_ID, frame("de_dust2", rnd=1, state="warmup", live=False))

    events = page_machine.apply(page("match-2397053-live.html"), now,
                                feed_connected=True)
    assert "E4" not in [e.type for e in events]
    assert match.pending_start_event(MATCH_ID) is not None

    # The warmup ends: the start goes out, and the map start with it.
    events = live.apply(MATCH_ID, frame("de_dust2", rnd=1, state="started"))
    assert [e.type for e in events] == ["E4", "E5"]
    assert events[0].payload["picks"], "the picks come from the page, not the feed"

    # And the next page poll adds nothing: the message is in the journal.
    for event in events:
        match.record_event(idempotency_key=event.idempotency_key, event_type=event.type,
                           match_id=MATCH_ID, body="x", chat_id="1")
    again = page_machine.apply(page("match-2397053-live.html"), now, feed_connected=True)
    assert "E4" not in [e.type for e in again]
