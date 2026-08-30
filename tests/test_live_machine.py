"""Transitions driven by live-feed frames: E5 and an immediate E6."""

import pytest

from hltv_notify.sources.scorebot import LiveFrame, parse_scoreboard
from hltv_notify.state.db import utcnow
from hltv_notify.state.live_machine import LiveMachine, normalize_map_name

MATCH_ID = 777
TEAM_ID = 12857
FOE_ID = 13973


def add_match(storage, lineup=("Mirage", "Dust2", "Ancient")):
    storage.upsert_match(
        match_id=MATCH_ID, opponent_id=FOE_ID, opponent_name="Color",
        event_name="Test Event", start_utc=utcnow(),
        url=f"https://www.hltv.org/matches/{MATCH_ID}/x",
        snapshot={}, snapshot_hash="x")
    if lineup:
        storage.set_map_lineup(MATCH_ID, list(lineup))


def frame(map_name="de_mirage", *, ours=0, theirs=0, rnd=1, state="started",
          live=True, we_are_ct=True, regulation=12, overtime=3) -> LiveFrame:
    """Our team is on CT by default. Sides swap in the feed after the break,
    so the score is tied to the id, not to the side."""
    if we_are_ct:
        ct_id, ct_score, t_id, t_score = TEAM_ID, ours, FOE_ID, theirs
    else:
        ct_id, ct_score, t_id, t_score = FOE_ID, theirs, TEAM_ID, ours
    return LiveFrame(
        map_name=map_name, current_round=rnd, round_state=state, live=live,
        ct_team_id=ct_id, ct_team_name="CT", ct_score=ct_score,
        t_team_id=t_id, t_team_name="T", t_score=t_score,
        regulation=regulation, overtime=overtime)


# ---------------------------------------------------------------- map names

@pytest.mark.parametrize("raw,expected", [
    ("de_mirage", "Mirage"), ("de_nuke", "Nuke"), ("cs_office", "Office"),
    ("Mirage", "Mirage"), ("", ""),
])
def test_map_name_normalisation(raw, expected):
    """The feed gives de_mirage, the page gives Mirage. They must be compared
    in one shape, otherwise a change of source would look like a change of map."""
    assert normalize_map_name(raw) == expected


# ---------------------------------------------------------------- E5

def test_e5_when_map_changes(storage, config):
    add_match(storage)
    m = LiveMachine(storage, config)
    m.apply(MATCH_ID, frame("de_mirage", rnd=1))
    m.apply(MATCH_ID, frame("de_mirage", ours=13, theirs=5, rnd=18))  # map taken
    # The warmup of the next map says nothing yet: it can run for twenty minutes.
    assert m.apply(MATCH_ID, frame("de_dust2", rnd=1, state="warmup", live=False)) == []
    events = m.apply(MATCH_ID, frame("de_dust2", rnd=1, state="started", live=False))
    assert [e.type for e in events] == ["E5"]
    assert events[0].idempotency_key == "E5:777:map:2:started:Dust2"
    assert events[0].payload["series_team"] == 1


def test_no_e5_when_connecting_in_the_middle_of_a_map(storage, config):
    """We connected mid-map — too late to announce that the map has started."""
    add_match(storage)
    m = LiveMachine(storage, config)
    assert m.apply(MATCH_ID, frame("de_mirage", ours=7, theirs=5, rnd=13)) == []


def test_e5_on_the_very_first_map_if_caught_from_the_start(storage, config):
    add_match(storage)
    m = LiveMachine(storage, config)
    events = m.apply(MATCH_ID, frame("de_mirage", rnd=1, state="started", live=False))
    assert [e.type for e in events] == ["E5"]
    assert events[0].payload["map_number"] == 1


def test_warmup_is_not_the_start_of_the_map(storage, config):
    """The warmup can run for twenty minutes, and the score sits at 0:0 all of
    it. The feed reports the warmup explicitly through currentRoundState — that
    is the only reliable signal.

    The `live` flag is NOT one: measured on a recorded map boundary it only
    turns true once the FIRST ROUND HAS BEEN PLAYED. Gating on it would announce
    the map after its first round was already decided, so the sequence below is
    exactly the recorded one: warmup/live=False, then started/live=False.
    """
    add_match(storage)
    m = LiveMachine(storage, config)

    for _ in range(3):
        assert m.apply(MATCH_ID, frame("de_mirage", rnd=1, state="warmup",
                                       live=False)) == []
    events = m.apply(MATCH_ID, frame("de_mirage", rnd=1, state="started", live=False))
    assert [e.type for e in events] == ["E5"]


def test_warmup_does_not_consume_the_map_change(storage, config):
    """The private memo must not be advanced during the warmup either: it would
    leave nothing for the map start to notice."""
    add_match(storage)
    m = LiveMachine(storage, config)
    m.apply(MATCH_ID, frame("de_mirage", rnd=1))
    m.apply(MATCH_ID, frame("de_mirage", ours=13, theirs=5, rnd=18))

    for _ in range(5):
        m.apply(MATCH_ID, frame("de_dust2", rnd=1, state="warmup", live=False))
    events = m.apply(MATCH_ID, frame("de_dust2", rnd=1, state="started", live=False))
    assert [e.type for e in events] == ["E5"]


def test_e5_not_repeated_on_every_frame(storage, config):
    """A scoreboard frame arrives several times a second."""
    add_match(storage)
    m = LiveMachine(storage, config)
    m.apply(MATCH_ID, frame("de_mirage", rnd=1))
    for _ in range(20):
        assert m.apply(MATCH_ID, frame("de_mirage", ours=3, theirs=2, rnd=6)) == []


# ---------------------------------------------------------------- E6

def test_e6_at_the_winning_round(storage, config):
    """This is what the whole thing is for: the event at the winning round,
    not when HLTV gets around to updating its maps section."""
    add_match(storage)
    m = LiveMachine(storage, config)
    m.apply(MATCH_ID, frame("de_mirage", rnd=1))
    # 12:9 is already a map point — that is E11, and it is tested on its own below.
    assert [e.type for e in
            m.apply(MATCH_ID, frame("de_mirage", ours=12, theirs=9, rnd=22))] == ["E11"]
    events = m.apply(MATCH_ID, frame("de_mirage", ours=13, theirs=9, rnd=22, state="ended"))
    assert [e.type for e in events] == ["E6"]
    assert events[0].idempotency_key == "E6:777:map:1:result:13-9"
    assert events[0].payload["overtime"] is False
    assert (events[0].payload["series_team"], events[0].payload["series_opponent"]) == (1, 0)


# ---------------------------------------------------------------- E11

def test_e11_at_map_point(storage, config):
    add_match(storage)
    m = LiveMachine(storage, config)
    m.apply(MATCH_ID, frame("de_mirage", rnd=1))
    assert m.apply(MATCH_ID, frame("de_mirage", ours=11, theirs=9, rnd=21)) == []
    events = m.apply(MATCH_ID, frame("de_mirage", ours=12, theirs=9, rnd=22))
    assert [e.type for e in events] == ["E11"]
    assert events[0].idempotency_key == "E11:777:map:1:point:us:13"
    assert events[0].payload["overtime"] == 0
    assert (events[0].payload["score_team"], events[0].payload["score_opponent"]) == (12, 9)


def test_e11_says_when_the_map_would_end_the_match(storage, config):
    """"Get ready" and "it is over in a minute" are different messages."""
    add_match(storage)
    m = LiveMachine(storage, config)
    m.apply(MATCH_ID, frame("de_mirage", rnd=1))
    storage.set_best_of(MATCH_ID, 3)   # the match page fills it in
    events = m.apply(MATCH_ID, frame("de_mirage", ours=12, theirs=9, rnd=22))
    # 0-0 in the series: taking Mirage makes it 1-0, the BO3 goes on.
    assert events[0].payload["decides_match"] is False

    m.apply(MATCH_ID, frame("de_mirage", ours=13, theirs=9, rnd=22, state="ended"))
    m.apply(MATCH_ID, frame("de_dust2", rnd=1))
    events = m.apply(MATCH_ID, frame("de_dust2", ours=12, theirs=9, rnd=22))
    assert [e.type for e in events] == ["E11"]
    assert events[0].payload["decides_match"] is True


def test_e11_for_the_opponent_too(storage, config):
    """A map point AGAINST us is the more urgent of the two."""
    add_match(storage)
    m = LiveMachine(storage, config)
    m.apply(MATCH_ID, frame("de_mirage", rnd=1))
    events = m.apply(MATCH_ID, frame("de_mirage", ours=4, theirs=12, rnd=17))
    assert [e.type for e in events] == ["E11"]
    assert events[0].idempotency_key == "E11:777:map:1:point:them:13"


def test_e11_not_repeated_while_the_round_is_played(storage, config):
    """The score stays at map point for a whole round — hundreds of frames."""
    add_match(storage)
    m = LiveMachine(storage, config)
    m.apply(MATCH_ID, frame("de_mirage", rnd=1))
    assert [e.type for e in m.apply(MATCH_ID, frame("de_mirage", ours=12, theirs=9,
                                                    rnd=22))] == ["E11"]
    for state in ("started", "ended", "freezePeriod", "started"):
        assert m.apply(MATCH_ID, frame("de_mirage", ours=12, theirs=9, rnd=22,
                                       state=state)) == []


def test_every_overtime_has_its_own_map_point(storage, config):
    """MR12/MR3: the target moves 13, 16, 19 — and every one of them is a map
    point of its own, with its own warning."""
    add_match(storage)
    m = LiveMachine(storage, config)
    m.apply(MATCH_ID, frame("de_mirage", rnd=1))
    keys = []
    for ours, theirs, rnd in [(12, 11, 23),      # map point in regulation
                              (12, 12, 24),      # levelled, overtime starts
                              (15, 14, 29),      # map point in overtime 1
                              (15, 15, 30),      # levelled again
                              (18, 17, 35)]:     # map point in overtime 2
        for event in m.apply(MATCH_ID, frame("de_mirage", ours=ours, theirs=theirs, rnd=rnd)):
            keys.append((event.type, event.idempotency_key,
                         event.payload.get("overtime")))
    assert keys == [
        ("E11", "E11:777:map:1:point:us:13", 0),
        ("E11", "E11:777:map:1:point:us:16", 1),
        ("E11", "E11:777:map:1:point:us:19", 2),
    ]


def test_no_map_point_when_the_scores_are_level(storage, config):
    add_match(storage)
    m = LiveMachine(storage, config)
    m.apply(MATCH_ID, frame("de_mirage", rnd=1))
    for ours, theirs, rnd in [(11, 11, 22), (12, 12, 24), (15, 15, 30)]:
        assert m.apply(MATCH_ID, frame("de_mirage", ours=ours, theirs=theirs, rnd=rnd)) == []


def test_no_map_point_during_the_warmup(storage, config):
    """A leftover score in a warmup frame between maps must not fire it."""
    add_match(storage)
    m = LiveMachine(storage, config)
    m.apply(MATCH_ID, frame("de_mirage", rnd=1))
    assert m.apply(MATCH_ID, frame("de_mirage", ours=12, theirs=9, rnd=22,
                                   state="warmup", live=False)) == []


def test_map_point_and_map_end_do_not_collide(storage, config):
    """The winning round produces E6 alone — the map is over, warning about it
    is pointless."""
    add_match(storage)
    m = LiveMachine(storage, config)
    m.apply(MATCH_ID, frame("de_mirage", rnd=1))
    events = m.apply(MATCH_ID, frame("de_mirage", ours=13, theirs=9, rnd=22, state="ended"))
    assert [e.type for e in events] == ["E6"]


def test_e6_not_repeated_while_feed_keeps_sending_final_score(storage, config):
    add_match(storage)
    m = LiveMachine(storage, config)
    m.apply(MATCH_ID, frame("de_mirage", rnd=1))
    final = frame("de_mirage", ours=13, theirs=9, rnd=22, state="ended")
    assert [e.type for e in m.apply(MATCH_ID, final)] == ["E6"]
    for _ in range(10):
        assert m.apply(MATCH_ID, final) == []


def test_e6_survives_mr3_overtime(storage, config):
    """12:12 does not count as a finished map, 16:14 does."""
    add_match(storage)
    m = LiveMachine(storage, config)
    m.apply(MATCH_ID, frame("de_mirage", rnd=1))
    assert m.apply(MATCH_ID, frame("de_mirage", ours=12, theirs=12, rnd=24)) == []
    assert [e.type for e in
            m.apply(MATCH_ID, frame("de_mirage", ours=15, theirs=14, rnd=29))] == ["E11"]
    events = m.apply(MATCH_ID, frame("de_mirage", ours=16, theirs=14, rnd=30, state="ended"))
    assert [e.type for e in events] == ["E6"]
    assert events[0].payload["overtime"] is True
    assert events[0].idempotency_key == "E6:777:map:1:result:16-14"


def test_e6_survives_second_overtime(storage, config):
    add_match(storage)
    m = LiveMachine(storage, config)
    m.apply(MATCH_ID, frame("de_mirage", rnd=1))
    assert m.apply(MATCH_ID, frame("de_mirage", ours=15, theirs=15, rnd=30)) == []
    events = m.apply(MATCH_ID, frame("de_mirage", ours=19, theirs=17, rnd=36))
    assert [e.type for e in events] == ["E6"]


def test_score_follows_the_team_not_the_side(storage, config):
    """After the break the sides swap. Tie to ctTeamId/tTeamId rather than to
    the side, otherwise the score flips halfway through the map."""
    add_match(storage)
    m = LiveMachine(storage, config)
    m.apply(MATCH_ID, frame("de_mirage", rnd=1))
    events = m.apply(MATCH_ID, frame("de_mirage", ours=13, theirs=4, rnd=17, we_are_ct=False))
    assert (events[0].payload["score_team"], events[0].payload["score_opponent"]) == (13, 4)


# ---------------------------------------------------------------- guards

def test_frame_without_our_team_is_dropped(storage, config):
    add_match(storage)
    m = LiveMachine(storage, config)
    alien = LiveFrame(map_name="de_mirage", current_round=1, round_state="started",
                      live=True, ct_team_id=1, ct_team_name="A", ct_score=13,
                      t_team_id=2, t_team_name="B", t_score=3,
                      regulation=12, overtime=3)
    assert m.apply(MATCH_ID, alien) == []
    assert storage.map_results(MATCH_ID) == []


def test_empty_map_name_frame_is_ignored():
    """In transitional frames mapName can be empty — a map start must not be
    announced from it."""
    assert parse_scoreboard({"mapName": "", "currentRound": 1}) is None
    assert parse_scoreboard({"currentRound": 1}) is None


def test_map_number_comes_from_the_page_lineup(storage, config):
    """The feed only knows the map name. If the number were counted as
    "recorded plus one", connecting mid-series would give the second map the
    first one's number — the page updates late and may not have recorded the
    first one yet."""
    add_match(storage, lineup=("Mirage", "Dust2", "Ancient"))
    m = LiveMachine(storage, config)
    events = m.apply(MATCH_ID, frame("de_dust2", rnd=1))
    assert events[0].payload["map_number"] == 2


def test_without_lineup_numbering_falls_back_to_counting(storage, config):
    add_match(storage, lineup=None)
    m = LiveMachine(storage, config)
    events = m.apply(MATCH_ID, frame("de_dust2", rnd=1))
    assert events[0].payload["map_number"] == 1


# ------------------------------------------------ the end of the match by maps


def win_the_map(machine, name, ours, theirs):
    machine.apply(MATCH_ID, frame(name, rnd=1))
    return machine.apply(MATCH_ID, frame(name, ours=ours, theirs=theirs, rnd=20))


def test_the_feed_calls_the_match_finished_on_the_last_map(storage, config):
    """The page reports this too, but minutes later — it has to notice the
    status flip first. Here it is known the moment the last map ends."""
    add_match(storage)
    storage.set_state(MATCH_ID, "LIVE", source="t")
    storage.set_best_of(MATCH_ID, 3)
    m = LiveMachine(storage, config)

    assert [e.type for e in win_the_map(m, "de_mirage", 13, 5)] == ["E6"]
    events = win_the_map(m, "de_dust2", 13, 7)
    assert [e.type for e in events] == ["E6", "E7"]

    finished = events[1]
    assert finished.idempotency_key == "E7:777:finished:2-0"
    assert finished.payload["won"] is True
    assert len(finished.payload["maps"]) == 2


def test_an_unknown_format_keeps_the_page_in_charge(storage, config):
    """Without best_of we do not guess: the page will report the end of the
    match as it did before."""
    add_match(storage)
    storage.set_state(MATCH_ID, "LIVE", source="t")
    m = LiveMachine(storage, config)
    assert [e.type for e in win_the_map(m, "de_mirage", 13, 5)] == ["E6"]


def test_a_bo3_at_one_all_is_not_finished(storage, config):
    add_match(storage)
    storage.set_state(MATCH_ID, "LIVE", source="t")
    storage.set_best_of(MATCH_ID, 3)
    m = LiveMachine(storage, config)
    win_the_map(m, "de_mirage", 13, 5)
    assert [e.type for e in win_the_map(m, "de_dust2", 7, 13)] == ["E6"]


def test_a_bo1_is_finished_by_its_only_map(storage, config):
    add_match(storage)
    storage.set_state(MATCH_ID, "LIVE", source="t")
    storage.set_best_of(MATCH_ID, 1)
    m = LiveMachine(storage, config)
    assert [e.type for e in win_the_map(m, "de_mirage", 13, 5)] == ["E6", "E7"]


def test_a_bo2_ends_level_after_both_maps(storage, config):
    """Nobody can take a majority of two, so the series ends when both maps
    have been played — and a draw is a legitimate result."""
    add_match(storage)
    storage.set_state(MATCH_ID, "LIVE", source="t")
    storage.set_best_of(MATCH_ID, 2)
    m = LiveMachine(storage, config)
    assert [e.type for e in win_the_map(m, "de_mirage", 13, 5)] == ["E6"]
    events = win_the_map(m, "de_dust2", 7, 13)
    assert [e.type for e in events] == ["E6", "E7"]
    assert events[1].payload["won"] is None
    assert events[1].idempotency_key == "E7:777:finished:1-1"
