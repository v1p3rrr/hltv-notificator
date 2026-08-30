"""Transitions from the match page: E4, E7, the stall detector and dedup."""

from datetime import datetime, timedelta, timezone

from conftest import FIXTURES, TEAM_ID
from hltv_notify.models import Event, MatchState
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
    """Our team is team2 (on the right), as in the real match 2397053."""
    return MatchObservation(
        match_id=match_id, status=status,
        start_utc=datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc),
        event_name="Test Event", event_id=1, best_of=3,
        team1_id=13973, team1_name="Color",
        team2_id=TEAM_ID, team2_name="FORZE Reload",
        maps=maps, scorebot_id=match_id if status == "live" else None,
    )


def maps(*scores):
    """`scores` are triples (left, right, halves) or None for an unplayed map.

    A score here means a PLAYED map, hence has_stats=True: on HLTV the
    statistics record appears at the moment it finishes. For a map being played
    right now there is a separate helper, live_map_line().
    """
    lines = []
    for number, score in enumerate(scores, start=1):
        left, right, halves = (None, None, None) if score is None else score
        lines.append(MapLine(number=number, name=f"Map{number}",
                             score_left=left, score_right=right, halves=halves,
                             has_stats=left is not None))
    return lines


def live_map_line(number, left, right):
    """A map being played: there is a score, there is no statistics record yet."""
    return MapLine(number=number, name=f"Map{number}", score_left=left,
                   score_right=right, halves=None, has_stats=False)


def test_e4_on_transition_to_live(storage, config):
    add_match(storage)
    m = MatchMachine(storage, config)
    events = m.apply(observe("live", maps(None, None, None)))
    assert [e.type for e in events] == ["E4"]
    assert events[0].idempotency_key == "E4:555:started"
    assert storage.get_state(MATCH_ID)["state"] == MatchState.LIVE


def test_e4_waits_while_the_feed_reports_a_warmup(storage, config):
    """The page raises LIVE when the teams connect to the server. The warmup
    before the first map can run twenty minutes, and "the match has started"
    during it is untrue."""
    add_match(storage)
    m = MatchMachine(storage, config)
    now = utcnow()
    storage.set_state(MATCH_ID, MatchState.SCHEDULED, source="team_page")
    storage.set_live_phase(MATCH_ID, "warmup", now)

    events = m.apply(observe("live", maps(None, None, None)), now,
                     feed_connected=True)
    assert events == []
    # The state still flips: the feed has to be brought up, and the schedule
    # must stop treating the match as one that has not begun.
    assert storage.get_state(MATCH_ID)["state"] == MatchState.LIVE
    assert storage.pending_start_event(MATCH_ID)["best_of"] == 3


def test_the_page_sends_the_start_once_the_warmup_is_over(storage, config):
    add_match(storage)
    m = MatchMachine(storage, config)
    now = utcnow()
    storage.set_state(MATCH_ID, MatchState.SCHEDULED, source="team_page")
    storage.set_live_phase(MATCH_ID, "warmup", now)
    assert m.apply(observe("live", maps(None, None, None)), now,
                   feed_connected=True) == []

    storage.set_live_phase(MATCH_ID, "started", now)
    events = m.apply(observe("live", maps(None, None, None)), now,
                     feed_connected=True)
    assert [e.type for e in events] == ["E4"]
    assert storage.pending_start_event(MATCH_ID) is None


def test_e4_is_not_held_without_a_live_feed(storage, config):
    """Losing E4 entirely would be far worse than sending it during a warmup."""
    add_match(storage)
    m = MatchMachine(storage, config)
    now = utcnow()
    storage.set_state(MATCH_ID, MatchState.SCHEDULED, source="team_page")
    storage.set_live_phase(MATCH_ID, "warmup", now)
    events = m.apply(observe("live", maps(None, None, None)), now,
                     feed_connected=False)
    assert [e.type for e in events] == ["E4"]


def test_a_stale_warmup_does_not_hold_e4_forever(storage, config):
    """The feed may have dropped mid-warmup — then there is nothing to wait
    for, and the page goes back to deciding on its own."""
    add_match(storage)
    m = MatchMachine(storage, config)
    now = utcnow()
    storage.set_state(MATCH_ID, MatchState.SCHEDULED, source="team_page")
    storage.set_live_phase(MATCH_ID, "warmup", now - timedelta(minutes=5))
    events = m.apply(observe("live", maps(None, None, None)), now,
                     feed_connected=True)
    assert [e.type for e in events] == ["E4"]


def test_the_page_stays_quiet_if_the_feed_sent_the_start(storage, config):
    add_match(storage)
    m = MatchMachine(storage, config)
    now = utcnow()
    storage.set_state(MATCH_ID, MatchState.SCHEDULED, source="team_page")
    storage.set_live_phase(MATCH_ID, "warmup", now)
    m.apply(observe("live", maps(None, None, None)), now, feed_connected=True)

    # The live machine got there first and its message is in the journal.
    storage.record_event(idempotency_key=f"E4:{MATCH_ID}:started", event_type="E4",
                         match_id=MATCH_ID, body="x", chat_id="1")
    storage.set_live_phase(MATCH_ID, "started", now)
    assert m.apply(observe("live", maps(None, None, None)), now,
                   feed_connected=True) == []
    assert storage.pending_start_event(MATCH_ID) is None


def test_e4_not_repeated_on_every_poll(storage, config):
    """The page is polled every minute and says LIVE the whole time. A map
    managed to finish meanwhile — an E6 about it, but no repeat E4."""
    add_match(storage)
    m = MatchMachine(storage, config)
    m.apply(observe("live", maps(None, None, None)))
    again = [e.type for e in m.apply(observe("live", maps((13, 10, "( 8 : 4 ; 5 : 6 )"), None, None)))]
    assert "E4" not in again
    assert again == ["E6"]


def test_e7_with_series_score(storage, config):
    add_match(storage)
    m = MatchMachine(storage, config)
    m.apply(observe("live", maps(None, None, None)))
    events = m.apply(observe("over", maps(
        (10, 13, "( 5 : 7 ; 8 : 3 )"), (8, 13, "( 4 : 8 ; 4 : 5 )"), None)))
    # Both maps finished between polls: first each of them, then the result.
    assert [e.type for e in events] == ["E6", "E6", "E7"]
    e7 = events[-1]
    # we are on the right: both 13s are ours
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
    """Sending "the match started" about a finished match is pointless — go
    straight to the result."""
    add_match(storage)
    m = MatchMachine(storage, config)
    events = m.apply(observe("over", maps((10, 13, None), (8, 13, None), None)))
    assert [e.type for e in events] == ["E7"]


def test_decider_not_played_is_not_counted(storage, config):
    """The BO3 ended 2:0 — the third map was left with a dash."""
    add_match(storage)
    m = MatchMachine(storage, config)
    events = m.apply(observe("over", maps((10, 13, None), (8, 13, None), None)))
    assert len(storage.map_results(MATCH_ID)) == 2


def test_overtime_detected_by_halves_not_by_score(storage, config):
    """Overtime rules differ between tournaments, so we count halves rather
    than rounds."""
    add_match(storage)
    m = MatchMachine(storage, config)
    m.apply(observe("over", maps((16, 19, "( 5 : 7 ; 7 : 5 ; 4 : 7 )"), None, None)))
    row = storage.map_results(MATCH_ID)[0]
    assert row["overtime"] == 1
    assert (row["score_team"], row["score_opponent"]) == (19, 16)


def test_stall_reported_only_after_threshold(storage, config):
    """A stall IN THE MIDDLE of a map: the map is running but the score is
    not moving."""
    add_match(storage)
    m = MatchMachine(storage, config)
    frozen = observe("live", [live_map_line(1, 5, 7)] + maps(None, None)[1:])
    now = utcnow()

    assert [e.type for e in m.apply(frozen, now=now)] == ["E4"]
    assert m.apply(frozen, now=now + timedelta(minutes=5)) == []

    late = now + timedelta(minutes=config.stale_minutes + 1)
    events = m.apply(frozen, now=late)
    assert [e.type for e in events] == ["E8"]
    assert "stale" in events[0].idempotency_key


def test_progress_resets_the_stall_timer(storage, config):
    """Technical pauses can be long, but while the score moves it is not a stall."""
    add_match(storage)
    m = MatchMachine(storage, config)
    now = utcnow()
    m.apply(observe("live", [live_map_line(1, 5, 7)] + maps(None, None)[1:]), now=now)
    moved = now + timedelta(minutes=config.stale_minutes - 1)
    m.apply(observe("live", [live_map_line(1, 6, 7)] + maps(None, None)[1:]), now=moved)
    events = m.apply(observe("live", [live_map_line(1, 6, 7)] + maps(None, None)[1:]),
                     now=moved + timedelta(minutes=config.stale_minutes - 1))
    assert events == []


def test_break_between_maps_is_not_a_stall(storage, config):
    """A real case from a BLAST match: a map ended, the next had not started,
    and after 20 minutes a false "the match has stalled" arrived. Between maps
    the threshold is stretched."""
    add_match(storage)
    m = MatchMachine(storage, config)
    between = observe("live", maps((13, 4, "( 8 : 4 ; 5 : 0 )"), None, None))
    now = utcnow()
    m.apply(between, now=now)

    normal = now + timedelta(minutes=config.stale_minutes + 1)
    assert m.apply(between, now=normal) == []

    very_long = now + timedelta(minutes=config.stale_minutes * 3 + 1)
    assert [e.type for e in m.apply(between, now=very_long)] == ["E8"]


def test_no_stall_alert_while_the_live_feed_is_connected(storage, config):
    """The point of the event is "I have gone blind". While the feed is
    connected we do see the match, and its silence during a pause is not
    blindness."""
    add_match(storage)
    m = MatchMachine(storage, config)
    frozen = observe("live", [live_map_line(1, 5, 7)] + maps(None, None)[1:])
    now = utcnow()
    m.apply(frozen, now=now, feed_connected=True)
    late = now + timedelta(minutes=config.stale_minutes * 5)
    assert m.apply(frozen, now=late, feed_connected=True) == []


def test_stall_timer_does_not_accumulate_under_a_working_feed(storage, config):
    """After the feed disconnects the alarm must not fire instantly for all
    the time it was working."""
    add_match(storage)
    m = MatchMachine(storage, config)
    frozen = observe("live", [live_map_line(1, 5, 7)] + maps(None, None)[1:])
    now = utcnow()
    m.apply(frozen, now=now, feed_connected=True)
    later = now + timedelta(hours=2)
    assert m.apply(frozen, now=later, feed_connected=True) == []
    # the feed dropped — the countdown restarts rather than backdating
    assert m.apply(frozen, now=later, feed_connected=False) == []


def test_observation_without_our_team_is_dropped(storage, config):
    """The wrong page or changed markup must not record somebody else's score."""
    add_match(storage)
    m = MatchMachine(storage, config)
    alien = MatchObservation(
        match_id=MATCH_ID, status="live", start_utc=None, event_name="", event_id=None,
        best_of=3, team1_id=1, team1_name="A", team2_id=2, team2_name="B",
        maps=maps((13, 10, None)), scorebot_id=None)
    assert m.apply(alien) == []
    assert storage.get_state(MATCH_ID) is None


def test_real_fixtures_drive_a_full_match(storage, config):
    """A pass over the real pages.

    On the live fixture the first map has already been played by the time we
    meet the match, so no E6 is sent for it — that is not a transition. The
    second finishes under observation and is reported.
    """
    add_match(storage, 2397047)
    m = MatchMachine(storage, config)
    live = match_page.parse((FIXTURES / "match-2397053-live.html").read_text(encoding="utf-8"),
                            2397047)
    over = match_page.parse((FIXTURES / "match-2397047-finished.html").read_text(encoding="utf-8"),
                            2397047)
    produced = [e.type for e in m.apply(live)] + [e.type for e in m.apply(over)]
    assert produced == ["E4", "E6", "E7"]


# ----------------------------------------------------------------------
# E6 is the key requirement of the spec: the end of a map with the score
# ----------------------------------------------------------------------


def test_e6_when_map_becomes_decided(storage, config):
    add_match(storage)
    m = MatchMachine(storage, config)
    m.apply(observe("live", maps(None, None, None)))
    events = m.apply(observe("live", maps((10, 13, "( 5 : 7 ; 8 : 3 )"), None, None)))
    assert [e.type for e in events] == ["E6"]
    e6 = events[0]
    assert e6.idempotency_key == "E6:555:map:1:result:13-10"
    assert (e6.payload["score_team"], e6.payload["score_opponent"]) == (13, 10)
    assert (e6.payload["series_team"], e6.payload["series_opponent"]) == (1, 0)
    assert e6.payload["map_name"] == "Map1"


def test_e6_not_repeated_while_match_continues(storage, config):
    """The page keeps showing a played map's score for a long time — one event."""
    add_match(storage)
    m = MatchMachine(storage, config)
    m.apply(observe("live", maps(None, None, None)))
    decided = observe("live", maps((10, 13, None), None, None))
    assert [e.type for e in m.apply(decided)] == ["E6"]
    assert m.apply(decided) == []
    assert m.apply(decided) == []


def test_two_maps_decided_between_polls_get_their_own_series_score(storage, config):
    """If polling missed the end of the first map, the series score in the
    message about it must be as of that map, not the final one."""
    add_match(storage)
    m = MatchMachine(storage, config)
    m.apply(observe("live", maps(None, None, None)))
    events = m.apply(observe("live", maps((10, 13, None), (13, 8, None), None)))
    assert [e.type for e in events] == ["E6", "E6"]
    first, second = events
    assert (first.payload["map_number"], first.payload["series_team"],
            first.payload["series_opponent"]) == (1, 1, 0)
    assert (second.payload["map_number"], second.payload["series_team"],
            second.payload["series_opponent"]) == (2, 1, 1)


def test_no_e6_if_match_discovered_already_over(storage, config):
    """The match finished while the service was down: showering E6 over every
    map after the fact is noise, we send only the result."""
    add_match(storage)
    m = MatchMachine(storage, config)
    events = m.apply(observe("over", maps((10, 13, None), (8, 13, None), None)))
    assert [e.type for e in events] == ["E7"]
    assert len(storage.map_results(MATCH_ID)) == 2


def test_e6_survives_overtime(storage, config):
    add_match(storage)
    m = MatchMachine(storage, config)
    m.apply(observe("live", maps(None, None, None)))
    events = m.apply(observe("live", maps((16, 19, "( 5 : 7 ; 7 : 5 ; 4 : 7 )"), None, None)))
    assert events[0].payload["overtime"] is True
    assert (events[0].payload["score_team"], events[0].payload["score_opponent"]) == (19, 16)


def test_full_bo3_replay_gives_exact_event_sequence(storage, config):
    """A recorded sequence of observations always yields the same list of
    events — that doubles as a regression test."""
    add_match(storage)
    m = MatchMachine(storage, config)
    timeline = [
        observe("upcoming", maps(None, None, None)),
        observe("live", maps(None, None, None)),
        observe("live", maps((10, 13, None), None, None)),
        observe("live", maps((10, 13, None), None, None)),
        observe("live", maps((10, 13, None), (13, 9, None), None)),
        observe("live", maps((10, 13, None), (13, 9, None), None)),
        observe("live", maps((10, 13, None), (13, 9, None), (11, 13, None))),
        observe("over", maps((10, 13, None), (13, 9, None), (11, 13, None))),
    ]
    produced = []
    for observation in timeline:
        produced += [e.type for e in m.apply(observation)]
    assert produced == ["E4", "E6", "E6", "E6", "E7"]


def test_replaying_the_same_timeline_twice_sends_nothing_new(storage, config):
    """The same scenario as a live-feed reconnect: the full state arrives
    again and the notification count must not change."""
    from hltv_notify.notify.outbox import Notifier

    add_match(storage)
    m = MatchMachine(storage, config)
    notifier = Notifier(storage, config, telegram=None)
    timeline = [
        observe("live", maps(None, None, None)),
        observe("live", maps((10, 13, None), None, None)),
        observe("live", maps((10, 13, None), (13, 9, None), None)),
        observe("over", maps((10, 13, None), (13, 9, None), None)),
    ]
    for _ in range(2):
        for observation in timeline:
            for event in m.apply(observation):
                notifier.enqueue(event)

    assert storage.sent_event_count() == 4       # E4, E6, E6, E7
    assert storage.pending_count() == 4


def test_running_map_does_not_produce_e6(storage, config):
    """The main trap, found on a live match: a running map has a numeric score
    too, and the naive rule would have sent an E6 with an in-play score."""
    add_match(storage)
    m = MatchMachine(storage, config)
    m.apply(observe("live", maps(None, None, None)))

    running = [live_map_line(1, 5, 7)] + maps(None, None)[1:]
    assert m.apply(observe("live", running)) == []
    assert storage.map_results(MATCH_ID) == []

    # ...and once the map ended, the event arrives once, with the final score
    events = m.apply(observe("live", maps((11, 13, "( 5 : 7 ; 6 : 6 )"), None, None)))
    assert [e.type for e in events] == ["E6"]
    assert (events[0].payload["score_team"], events[0].payload["score_opponent"]) == (13, 11)


def test_series_score_stored_during_running_map_excludes_it(storage, config):
    add_match(storage)
    m = MatchMachine(storage, config)
    m.apply(observe("live", maps(None, None, None)))
    m.apply(observe("live", maps((11, 13, None), None, None) [:1]
                    + [live_map_line(2, 3, 4)] + maps(None, None, None)[2:]))
    assert storage.get_state(MATCH_ID)["series_score"] == "1-0"


def test_drawn_series_is_neither_a_win_nor_a_loss():
    """A BO2 quite happily ends 1:1.

    A boolean here would mean a defeat, and for the recipient following the
    opponent format.orient would flip it into a win — about one and the same
    result.
    """
    from hltv_notify.notify import format as fmt

    payload = {"series_team": 1, "series_opponent": 1,
               "won": None, "team_id": 1, "opponent_id": 2,
               "team_name": "MOUZ", "opponent": "FORZE Reload",
               "event_name": "Major", "url": "u", "maps": []}
    event = Event(type="E7", idempotency_key="E7:1:finished:1-1",
                  match_id=1, payload=payload)

    ours = fmt.render(event, team_name="MOUZ", tz_name="UTC", for_team_id=1)
    theirs = fmt.render(event, team_name="MOUZ", tz_name="UTC", for_team_id=2)
    assert "🤝" in ours and "🤝" in theirs
    assert "🏆" not in theirs and "💀" not in ours
