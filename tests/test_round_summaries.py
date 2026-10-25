"""Map/match round histories, the next pick, and highlight score transitions."""

from dataclasses import replace

import pytest

from conftest import FIXTURES
from hltv_notify.models import Event
from hltv_notify.notify.format import orient, render, round_history_lines
from hltv_notify.replay import _prepare, replay
from hltv_notify.sources.scorebot import PlayerLine, RoundOutcome, parse_scoreboard
from hltv_notify.state import round_history
from hltv_notify.state.db import Storage
from hltv_notify.state.highlights import RoundTracker
from hltv_notify.state.live_machine import LiveMachine
from hltv_notify.state.match_machine import MatchMachine
from test_live_machine import MATCH_ID, TEAM_ID, FOE_ID, add_match, frame
from test_match_machine import observe, maps


def message(event, for_team_id=None):
    return render(event, team_name="FORZE Reload", tz_name="UTC", for_team_id=for_team_id)


def test_real_recording_recovers_regulation_history(storage, config):
    match_id = 2397053
    _prepare(storage, match_id)
    storage.set_map_lineup(match_id, ["Mirage", "Dust2", "Ancient"])
    events = replay(FIXTURES / "scorebot-2397053-forze.jsonl.gz", storage, config, match_id)
    result = next(event for event in events if event.type == "E6")
    history = result.payload["round_history"]
    assert history["rounds"].count("W") == 10
    assert history["rounds"].count("L") == 13
    assert "?" not in history["rounds"]
    assert history["sides"]["0"] == "CT"
    assert history["sides"]["1"] == "T"
    assert "(CT)" in message(result) and " | (T)" in message(result)


def test_overtimes_come_from_score_increments_and_actual_sides(storage, config):
    add_match(storage)
    storage.set_state(MATCH_ID, "LIVE", source="test")
    storage.set_best_of(MATCH_ID, 1)
    machine = LiveMachine(storage, config)
    ours = theirs = 0
    events = []
    # Shorter format to exercise regulation and two overtimes in eight rounds.
    # The first overtime deliberately starts on a different side from regulation.
    wins = "WLWLWLWW"
    sides = [False, False, True, True, True, False, False, True]
    for number, (outcome, ct) in enumerate(zip(wins, sides), 1):
        shot = replace(frame(ours=ours, theirs=theirs, rnd=number,
                             we_are_ct=ct, regulation=2, overtime=1),
                       starting_t=TEAM_ID, starting_ct=FOE_ID)
        events += machine.apply(MATCH_ID, shot)
        ours += outcome == "W"
        theirs += outcome == "L"
        events += machine.apply(MATCH_ID, replace(
            shot, ct_score=ours if ct else theirs, t_score=theirs if ct else ours,
            round_state="ended"))
    map_end = next(event for event in events if event.type == "E6")
    match_end = next(event for event in events if event.type == "E7")
    for event in (map_end, match_end):
        text = message(event)
        assert "(T) WL | (CT) WL" in text
        assert "OT1 (CT) W | (T) L" in text
        assert "OT2 (T) W | (CT) W" in text
    mirrored = message(match_end, FOE_ID)
    assert "(CT) LW | (T) LW" in mirrored
    assert "OT2 (CT) L | (T) L" in mirrored
    assert map_end.payload["next_map"] == {}


def test_history_survives_process_restart_and_leaves_overtime_gaps(tmp_path):
    path = tmp_path / "rounds.db"
    storage = Storage(path)
    add_match(storage)
    initial = replace(frame(ours=12, theirs=12, rnd=25),
                      ct_history=tuple(RoundOutcome(n, n % 2 == 1) for n in range(1, 25)))
    round_history.observe(storage, MATCH_ID, 1, initial, TEAM_ID)
    round_history.observe(storage, MATCH_ID, 1, frame(ours=13, theirs=12, rnd=25, state="ended"), TEAM_ID)
    storage.close()
    storage = Storage(path)
    try:
        # Two rounds missed across the restart: their order is not in the score.
        round_history.observe(storage, MATCH_ID, 1, frame(ours=14, theirs=13, rnd=28), TEAM_ID)
        round_history.observe(storage, MATCH_ID, 1, frame(ours=14, theirs=14, rnd=28, state="ended"), TEAM_ID)
        history = round_history.summary(storage, MATCH_ID, 1, 14, 14)
        assert history["rounds"][24:] == "W??L"
        assert "?" in "\n".join(round_history_lines(history))
    finally:
        storage.close()


def test_score_reset_drops_discarded_rounds(storage):
    add_match(storage)
    round_history.observe(storage, MATCH_ID, 1, frame(), TEAM_ID)
    round_history.observe(storage, MATCH_ID, 1, frame(ours=1, rnd=1, state="ended"), TEAM_ID)
    round_history.observe(storage, MATCH_ID, 1, frame(), TEAM_ID)
    round_history.observe(storage, MATCH_ID, 1, frame(theirs=1, rnd=1, state="ended"), TEAM_ID)
    assert round_history.summary(storage, MATCH_ID, 1, 0, 1)["rounds"] == "L"


def test_incoherent_and_warmup_frames_do_not_record_rounds(storage, config):
    add_match(storage)
    machine = LiveMachine(storage, config)
    machine.apply(MATCH_ID, frame(ours=9, theirs=4, rnd=1, state="ended"))
    machine.apply(MATCH_ID, frame(ours=3, rnd=4, state="warmup"))
    assert round_history.summary(storage, MATCH_ID, 1, 9, 4) == {}


def test_history_parser_rejects_unknown_results():
    parsed = parse_scoreboard({"mapName": "de_nuke", "ctMatchHistory": {
        "firstHalf": [{"roundOrdinal": 1, "type": "lost"},
                      {"roundOrdinal": 2, "type": "new_unknown_type"},
                      {"roundOrdinal": 3, "type": "CTs_Win"}],
        "secondHalf": []}})
    assert parsed.ct_history == (RoundOutcome(1, False), RoundOutcome(3, True))


def test_page_and_feed_share_next_pick_and_history(storage, config):
    add_match(storage)
    page = MatchMachine(storage, config)
    observation = observe("live", maps(None, None, None), match_id=MATCH_ID)
    observation = replace(observation, maps=[
        replace(observation.maps[0], name="Mirage", picked_by="right"),
        replace(observation.maps[1], name="Dust2", picked_by="left"),
        replace(observation.maps[2], name="Ancient")])
    page.apply(observation)
    live = LiveMachine(storage, config)
    live.apply(MATCH_ID, replace(frame(ours=12, theirs=5, rnd=18),
                                t_team_name="Color", ct_team_name="FORZE Reload"))
    feed_result = next(event for event in live.apply(
        MATCH_ID, replace(frame(ours=13, theirs=5, rnd=18, state="ended"),
                          t_team_name="Color", ct_team_name="FORZE Reload")) if event.type == "E6")
    assert feed_result.payload["next_map"] == {"number": 2, "name": "Dust2", "pick": "opponent"}
    assert "Next: <b>Dust2</b> (Color pick)" in message(feed_result)
    assert "Next: <b>Dust2</b> (Color pick)" in message(feed_result, FOE_ID)
    # The page fallback attaches the exact same persisted history and pick.
    finished_line = replace(observation.maps[0], score_left=5, score_right=13, has_stats=True)
    fallback = page._event_e6(observation, storage.get_match(MATCH_ID), TEAM_ID, finished_line)
    assert fallback.payload["round_history"] == feed_result.payload["round_history"]
    assert fallback.payload["next_map"] == feed_result.payload["next_map"]
    # A forfeit can end a BO3 before either team reaches two maps.
    finished = page._event_e6(replace(observation, status="over"),
                              storage.get_match(MATCH_ID), TEAM_ID, finished_line)
    assert finished.payload["next_map"] == {}


def test_partial_veto_does_not_erase_a_known_next_pick(storage):
    add_match(storage)
    known = {"number": 2, "name": "Nuke", "pick": "opponent"}
    round_history.set_picks(storage, MATCH_ID, [known])
    round_history.set_picks(storage, MATCH_ID, [
        {"number": 1, "name": "Mirage", "pick": "team"},
        {"number": 2, "name": "TBA", "pick": "decider"}])
    assert round_history.next_map(storage, MATCH_ID, 1, 1, 0) == known


def test_final_map_has_no_next_pick_and_decider_is_named(storage):
    add_match(storage)
    storage.set_state(MATCH_ID, "LIVE", source="test")
    storage.set_best_of(MATCH_ID, 3)
    round_history.set_picks(storage, MATCH_ID, [
        {"number": 2, "name": "Dust2", "pick": "team"},
        {"number": 3, "name": "Ancient", "pick": "decider"}])
    assert round_history.next_map(storage, MATCH_ID, 2, 2, 0) == {}
    assert round_history.next_map(storage, MATCH_ID, 2, 1, 1)["pick"] == "decider"
    event = Event(type="E6", idempotency_key="example", match_id=MATCH_ID, payload={
        "next_map": round_history.next_map(storage, MATCH_ID, 2, 1, 1)})
    assert "Next: <b>Ancient</b> (decider)" in message(event)


def test_page_result_correction_does_not_assert_incompatible_history(storage):
    add_match(storage)
    round_history.observe(storage, MATCH_ID, 1, frame(ours=1, rnd=1, state="ended"), TEAM_ID)
    assert round_history.summary(storage, MATCH_ID, 1, 0, 1)["rounds"] == "?"


def player(kills, alive=True, clutches=0):
    return PlayerLine("steam", "ropz", kills, alive=alive, clutches=clutches)


@pytest.mark.parametrize("clutch", [False, True])
def test_highlight_score_transition_reaches_the_message(storage, config, clutch):
    add_match(storage)
    storage.add_team("555", TEAM_ID, "forze-reload", "FORZE Reload")
    machine = LiveMachine(storage, config)
    start = replace(frame(ours=2, theirs=4, rnd=7),
                    ct_players=(player(10),),
                    t_players=tuple(PlayerLine(str(i), str(i), 0) for i in range(3)))
    machine.apply(MATCH_ID, start)
    # The first frame establishes the baseline; the next observes the 1v3.
    machine.apply(MATCH_ID, start)
    ending = replace(start, ct_score=3, round_state="ended",
                     ct_players=(player(14, clutches=int(clutch)),))
    events = machine.apply(MATCH_ID, ending)
    event = next(event for event in events if event.type in ("E9", "E15"))
    assert event.type == ("E15" if clutch else "E9")
    assert "round 7 · score 2:4 → 3:4" in message(event)
    assert "round 7 · score 4:2 → 4:3" in message(event, FOE_ID)


def test_dead_player_is_reported_with_current_score_only():
    tracker = RoundTracker(multikill=4, clutch=0)
    tracker.observe("Mirage", 7, "started", [player(10)], score=(2, 4))
    found = tracker.observe("Mirage", 7, "started", [player(14, alive=False)], score=(2, 4))[0]
    assert found.score_after_team is None
    event = Event(type="E9", idempotency_key="example", match_id=1, payload={
        "kills": 4, "round": found.round_number, "score_team": found.score_team,
        "score_opponent": found.score_opponent})
    assert "round 7 · score 2:4" in message(event)
    assert "→" not in message(event)


def test_late_highlight_uses_only_a_contiguous_round_result():
    for next_round, end_score, expected in ((8, (3, 4), 3), (10, (4, 5), None)):
        tracker = RoundTracker(multikill=4)
        tracker.observe("Mirage", 7, "started", [player(10)], score=(2, 4))
        tracker.observe("Mirage", 7, "started", [player(14)], score=(2, 4))
        found = tracker.observe("Mirage", next_round, "started", [player(15)], score=end_score)[0]
        assert found.round_number == 7
        assert found.score_before_team == 2
        assert found.score_after_team == expected
