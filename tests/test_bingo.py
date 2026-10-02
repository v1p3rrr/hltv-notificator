"""The bingo card: what fills it, and the two things that must not fill it.

The two are the whole reason this file is long. A kill arrives from the feed's
`log`, which replays its entire backlog on every connect; and a map's opening
rounds can be a knife round and a warmup that nothing in the frame marks as
one, which the server then wipes by resetting the score. Both were measured on
the recordings in docs/recon/fixtures, and both are tested here against the
shapes they really take.
"""

import gzip
import json

import pytest

from conftest import FIXTURES
from hltv_notify import settings as prefs
from hltv_notify.config import Config
from hltv_notify.models import Event
from hltv_notify.notify import format as fmt
from hltv_notify.sources.scorebot import (KillEvent, LiveFrame, PlayerLine,
                                          RoundOutcome, feed_items,
                                          kills_from_log, parse_kills)
from hltv_notify.state import bingo
from hltv_notify.state.db import utcnow
from hltv_notify.state.live_machine import LiveMachine

MATCH_ID = 777
TEAM_ID = 12857
FOE_ID = 13973
# HLTV player ids, the join between a scoreboard row and a Kill in the log.
OURS = (101, 102, 103, 104, 105)
THEIRS = (201, 202, 203, 204, 205)


def add_match(storage, lineup=("Mirage", "Dust2", "Ancient"), teams=(TEAM_ID,)):
    storage.upsert_match(
        match_id=MATCH_ID, opponent_id=FOE_ID, opponent_name="Color",
        event_name="Test Event", start_utc=utcnow(),
        url=f"https://www.hltv.org/matches/{MATCH_ID}/x",
        snapshot={}, snapshot_hash="x")
    for team_id in teams:
        storage.link_match_team(MATCH_ID, team_id)
    if lineup:
        storage.set_map_lineup(MATCH_ID, list(lineup))


def players(ids, *, kills=0, alive=True, clutches=0):
    return tuple(PlayerLine(steam_id=f"1:0:{pid}", nick=f"p{pid}", kills=kills,
                            alive=alive, clutches=clutches, player_id=pid)
                 for pid in ids)


def frame(map_name="de_mirage", *, ours=0, theirs=0, rnd=1, state="started",
          live=True, we_are_ct=True, regulation=12, overtime=3,
          our_players=None, their_players=None,
          our_history=(), their_history=()) -> LiveFrame:
    our_players = our_players if our_players is not None else players(OURS)
    their_players = their_players if their_players is not None else players(THEIRS)
    if we_are_ct:
        return LiveFrame(
            map_name=map_name, current_round=rnd, round_state=state, live=live,
            ct_team_id=TEAM_ID, ct_team_name="FORZE", ct_score=ours,
            t_team_id=FOE_ID, t_team_name="Color", t_score=theirs,
            regulation=regulation, overtime=overtime,
            ct_players=our_players, t_players=their_players,
            ct_history=our_history, t_history=their_history)
    return LiveFrame(
        map_name=map_name, current_round=rnd, round_state=state, live=live,
        ct_team_id=FOE_ID, ct_team_name="Color", ct_score=theirs,
        t_team_id=TEAM_ID, t_team_name="FORZE", t_score=ours,
        regulation=regulation, overtime=overtime,
        ct_players=their_players, t_players=our_players,
        ct_history=their_history, t_history=our_history)


def history(*results) -> tuple:
    """`history(True, False, True)` -> rounds 1, 2, 3 won / lost / won."""
    return tuple(RoundOutcome(ordinal=index, won=won)
                 for index, won in enumerate(results, start=1))


def kill(event_id, killer=OURS[0], *, weapon="ak47", smoke=False, wall=False):
    return KillEvent(event_id=event_id, killer_id=killer, killer_nick="nick",
                     weapon=weapon, through_smoke=smoke, penetrated=wall)


def machine(storage, **env) -> LiveMachine:
    return LiveMachine(storage, Config(bingo=True, bingo_live=True, **env))


def seed(m, storage, *, map_name="de_mirage"):
    """Get past the one-off seeding of the feed's replayed backlog.

    Every test that wants a kill COUNTED has to do this first, which is the
    point: the first batch after connecting is the state of the world before
    we were watching.
    """
    m.apply(MATCH_ID, frame(map_name))
    m.observe_kills(MATCH_ID, [kill(1)])


# ------------------------------------------------------- what a kill feeds

@pytest.mark.parametrize("made,expected", [
    (kill(1, weapon="ak47"), ()),
    (kill(1, weapon="ak47", smoke=True), ("smoke",)),
    (kill(1, weapon="ak47", wall=True), ("wallbang",)),
    (kill(1, weapon="hegrenade"), ("he",)),
    (kill(1, weapon="knife_karambit"), ("knife",)),
    (kill(1, weapon="knife_t"), ("knife",)),
    # The default T knife is the one that does not read like a skin, and
    # every skin is still a knife_*.
    (kill(1, weapon="inferno"), ()),
])
def test_a_kill_feeds_the_squares_it_matches(made, expected):
    assert bingo.kill_squares(made) == expected


def test_one_kill_can_feed_two_squares():
    """A grenade thrown into smoke is both, and the card counts it in both:
    they are different claims about the same kill, not one claim twice."""
    assert set(bingo.kill_squares(
        kill(1, weapon="hegrenade", smoke=True))) == {"smoke", "he"}


# ------------------------------------------------------- the round squares

def test_pistol_rounds_are_the_first_round_of_every_half():
    """Under MR12/MR3: 1 and 13 in regulation, then one per overtime half.
    A match that reaches a second overtime has four of them on one map."""
    assert bingo.pistol_rounds(12, 3, upto=12) == (1,)
    assert bingo.pistol_rounds(12, 3, upto=13) == (1, 13)
    assert bingo.pistol_rounds(12, 3, upto=30) == (1, 13, 25, 28)
    assert bingo.pistol_rounds(12, 3, upto=36) == (1, 13, 25, 28, 31, 34)
    # MR15, the old format, still works out of the numbers the feed sends.
    assert bingo.pistol_rounds(15, 3, upto=16) == (1, 16)


def test_pistols_won_counts_only_the_pistol_rounds():
    won = history(True, True, True, False, False, False, False, False, False,
                  False, False, False, True)          # rounds 1 and 13 taken
    assert bingo.pistols_won(won, 12, 3) == 2
    lost_the_second = history(*([True] + [False] * 12))
    assert bingo.pistols_won(lost_the_second, 12, 3) == 1


def test_a_streak_runs_across_the_half():
    """The sides swap at the break; a run of won rounds does not care. The
    halves arrive as two lists and are one ordered sequence."""
    rounds = history(False, False, False, False, False, False, False, False,
                     False, False, True, True, True, True)   # 11-14
    assert bingo.longest_streak(rounds) == 4


def test_a_streak_does_not_leap_a_round_the_history_never_carried():
    """A gap in the ordinals is a round we cannot claim was won. Joining
    across it would invent the streak the card is asking for."""
    rounds = (RoundOutcome(1, True), RoundOutcome(2, True),
              RoundOutcome(4, True), RoundOutcome(5, True))
    assert bingo.longest_streak(rounds) == 2


def test_overtime_is_both_teams_at_regulation_not_the_round_count():
    """13:11 is twenty-four rounds played and a map won in regulation. Reading
    the SUM would call it an overtime."""
    assert not bingo.in_overtime(13, 11, 12)
    assert bingo.in_overtime(12, 12, 12)
    assert bingo.in_overtime(15, 13, 12)


# ------------------------------------------------------------ aggregation

def test_counted_squares_add_up_and_a_streak_does_not():
    rows = [{"map_number": 1, "square": "smoke", "value": 3},
            {"map_number": 2, "square": "smoke", "value": 2},
            {"map_number": 1, "square": "streak", "value": 4},
            {"map_number": 2, "square": "streak", "value": 4}]
    total = bingo.totals(rows)
    assert total["smoke"] == 5
    # Four in a row on each of two maps is still four in a row, never eight.
    assert total["streak"] == 4


def test_the_card_always_has_all_nine_squares():
    card = bingo.card({"smoke": 4})
    assert len(card) == len(bingo.SQUARES)
    assert [item["key"] for item in card] == list(bingo.KEYS)
    assert [item for item in card if item["key"] == "smoke"][0]["done"] is True
    assert bingo.closed_count({"smoke": 4}) == 1


# --------------------------------------------------- the replayed backlog

def test_the_first_batch_of_kills_is_recorded_and_not_counted(storage):
    """On connecting, the feed replays the whole match — in the forze
    recording 406 kills in the first batch, reaching back to a map played
    before we were watching. There is nothing in a Kill that says which map it
    belongs to, so the mark is set and none of it is counted."""
    add_match(storage)
    m = machine(storage)
    m.apply(MATCH_ID, frame())
    assert m.observe_kills(MATCH_ID, [kill(10, smoke=True),
                                      kill(11, smoke=True)]) == []
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "smoke") == 0
    assert storage.bingo_watermark(MATCH_ID) == 11


def test_a_kill_past_the_mark_counts_once_however_often_it_is_replayed(storage):
    add_match(storage)
    m = machine(storage)
    seed(m, storage)
    events = m.observe_kills(MATCH_ID, [kill(20, smoke=True)])
    assert [e.type for e in events] == ["E16"]
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "smoke") == 1
    # The next connect replays it, together with everything before it.
    assert m.observe_kills(MATCH_ID, [kill(1), kill(20, smoke=True)]) == []
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "smoke") == 1


def test_the_mark_never_moves_backwards(storage):
    """A reconnect's backlog ends earlier than the live stream did. A mark
    that followed it down would count everything in between a second time."""
    add_match(storage)
    m = machine(storage)
    seed(m, storage)
    m.observe_kills(MATCH_ID, [kill(50, smoke=True)])
    m.observe_kills(MATCH_ID, [kill(20)])
    assert storage.bingo_watermark(MATCH_ID) == 50


def test_kills_arriving_before_any_frame_are_dropped(storage):
    """There is no map to put them on. Putting them on whatever the next frame
    says is how a kill from map one is counted on map two."""
    add_match(storage)
    m = machine(storage)
    m.observe_kills(MATCH_ID, [kill(1)])          # seeds
    assert m.observe_kills(MATCH_ID, [kill(2, smoke=True)]) == []
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "smoke") == 0


# --------------------------------------------------------- whose kill it is

def test_a_kill_is_attributed_by_player_id_not_by_side(storage):
    """The sides swap at the break and the backlog replays kills from before
    it, so `killerSide` is the wrong answer half the time. `dbId` is not."""
    add_match(storage)
    m = machine(storage)
    seed(m, storage)
    # Second half: our team is on T now, the same players.
    m.apply(MATCH_ID, frame(ours=7, theirs=5, rnd=13, we_are_ct=False))
    m.observe_kills(MATCH_ID, [kill(30, killer=OURS[2], smoke=True)])
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "smoke") == 1


def test_the_opponents_kills_do_not_fill_our_card(storage):
    add_match(storage)
    m = machine(storage)
    seed(m, storage)
    assert m.observe_kills(MATCH_ID, [kill(30, killer=THEIRS[0], smoke=True)]) == []
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "smoke") == 0


def test_each_tracked_team_keeps_its_own_card(storage):
    """Two tracked teams playing each other is one match and two cards. A
    single card would credit one side with the other's grenade kills, and a
    count cannot be turned around at render time the way a score can."""
    add_match(storage, teams=(TEAM_ID, FOE_ID))
    m = machine(storage)
    seed(m, storage)
    m.observe_kills(MATCH_ID, [kill(30, killer=OURS[0], smoke=True),
                               kill(31, killer=THEIRS[0], smoke=True)])
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "smoke") == 1
    assert storage.bingo_value(MATCH_ID, 1, FOE_ID, "smoke") == 1


def test_a_square_read_off_the_history_is_not_counted_twice(storage):
    """The pistol count is RECOMPUTED from the frame on every frame, and the
    high-water mark that keeps it from repeating lives in memory. So the write
    has to be a MAX and not an addition: a worker re-created mid-map — by
    `reconcile`, by a 403 cooldown, by a restart — recomputes the whole value
    again, and added, a team that took two pistol rounds is reported as having
    taken four, then six."""
    add_match(storage)
    won = history(*([True] + [False] * 11 + [True]))
    lost = history(*([False] + [True] * 11 + [False]))
    for _ in range(3):
        # A FRESH machine each time: the same database, nothing remembered.
        machine(storage).apply(MATCH_ID, frame(
            ours=2, theirs=11, rnd=13, our_history=won, their_history=lost))
        assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "pistol") == 2
    assert bingo.totals(storage.bingo_rows(MATCH_ID, TEAM_ID))["pistol"] == 2


def test_a_moment_from_a_kill_names_the_right_opponent(storage):
    """An event born from a kill has no frame of its own, and `_context` reads
    the opponent's NAME off the frame — falling back to `matches.opponent_name`,
    which is the CANONICAL team's opponent. Built on a fabricated empty frame,
    a moment about the second tracked team named that team as its own
    opponent: the "FORZE — FORZE" trap by the back door."""
    add_match(storage, teams=(TEAM_ID, FOE_ID))
    storage.add_team("1", TEAM_ID, "forze", "FORZE")
    storage.add_team("1", FOE_ID, "navi", "Natus Vincere")
    m = machine(storage)
    seed(m, storage)
    events = m.observe_kills(MATCH_ID, [kill(40, killer=THEIRS[0], smoke=True)])
    moment = next(e for e in events if e.payload["team_id"] == FOE_ID)
    assert moment.payload["team_name"] == "Natus Vincere"
    assert moment.payload["opponent"] == "FORZE"
    assert moment.payload["opponent_id"] == TEAM_ID


# ------------------------------------------------------- the knife round

def test_a_round_of_nothing_but_knives_is_the_knife_round(storage):
    """Measured: all 16 knife kills of the forze recording are from the knife
    rounds before its two maps, and a knife round is not marked `warmup` — it
    scores on the board like any other. The signal that works is that nobody
    in it used anything else."""
    add_match(storage)
    m = machine(storage)
    seed(m, storage)
    m.observe_kills(MATCH_ID, [kill(40, weapon="knife_t"),
                               kill(41, killer=THEIRS[0], weapon="knife_karambit")])
    # Still held: the round is not over, so the question is not answered yet.
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "knife") == 0
    m.apply(MATCH_ID, frame(rnd=2))
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "knife") == 0


def test_a_knife_kill_in_a_real_round_counts(storage):
    add_match(storage)
    m = machine(storage)
    seed(m, storage)
    m.observe_kills(MATCH_ID, [kill(40, weapon="knife_t"),
                               kill(41, killer=THEIRS[0], weapon="ak47")])
    events = m.apply(MATCH_ID, frame(rnd=2))
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "knife") == 1
    assert [e.payload["square"] for e in events if e.type == "E16"] == ["knife"]


def test_a_warmup_deathmatch_does_not_vouch_for_the_knife_round(storage):
    """The warmup carries the round number the knife round is about to use, so
    an AWP kill in the deathmatch would answer the knife round's question for
    it — and answer it wrongly. Nothing a warmup produces may be read at all."""
    add_match(storage)
    m = machine(storage)
    seed(m, storage)
    m.apply(MATCH_ID, frame(rnd=1, state="warmup", live=False))
    m.observe_kills(MATCH_ID, [kill(40, weapon="awp")])          # deathmatch
    m.apply(MATCH_ID, frame(rnd=1, state="started"))             # the knife round
    m.observe_kills(MATCH_ID, [kill(41, weapon="knife_t"),
                               kill(42, killer=THEIRS[0], weapon="knife_karambit")])
    m.apply(MATCH_ID, frame(rnd=2))
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "knife") == 0


def test_a_knife_kill_in_the_last_round_of_a_map_is_not_lost(storage):
    """The round it waits on never turns over, so the map's end releases it."""
    add_match(storage)
    m = machine(storage)
    seed(m, storage)
    m.apply(MATCH_ID, frame(ours=12, theirs=5, rnd=18))
    m.observe_kills(MATCH_ID, [kill(40, weapon="knife_karambit"),
                               kill(41, killer=THEIRS[0], weapon="awp")])
    m.apply(MATCH_ID, frame(ours=13, theirs=5, rnd=19))      # map taken
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "knife") == 1


# ------------------------------------------------------ the score reset

def clock():
    """A hand-wound monotonic clock, so the three-minute wait is not real."""
    state = {"now": 0.0}

    def read():
        return state["now"]

    read.advance = lambda seconds: state.__setitem__("now", state["now"] + seconds)
    return read


def test_a_score_that_resets_and_stays_reset_wipes_the_map(storage):
    """The owner's case, measured on live HLTV: warmup, a knife round that
    SCORES 1:0, a couple of idle rounds, and only then 0:0 for the real map.
    Everything before that last 0:0 belongs to no map."""
    add_match(storage)
    m = machine(storage)
    tick = clock()
    m._bingo[MATCH_ID] = bingo.BingoTracker(clock=tick)
    seed(m, storage)
    m.apply(MATCH_ID, frame(ours=1, theirs=0, rnd=2))
    m.observe_kills(MATCH_ID, [kill(40, smoke=True), kill(41, weapon="hegrenade")])
    m.apply(MATCH_ID, frame(ours=1, theirs=0, rnd=3))
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "smoke") == 1
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "he") == 1

    m.apply(MATCH_ID, frame(ours=0, theirs=0, rnd=1))        # the reset
    tick.advance(bingo.RESET_CONFIRM_SECONDS + 1)
    m.apply(MATCH_ID, frame(ours=0, theirs=0, rnd=1))
    # Everything, not only the knife: those rounds could carry an ace or a
    # grenade kill just as easily, and they happened on a map that was
    # thrown away.
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "smoke") == 0
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "he") == 0


def test_a_crashed_server_that_gets_its_score_back_is_not_a_reset(storage):
    """0:0 and then the same score again is a server that was restored. The
    frame showing 0:0 is identical in both cases; what tells them apart is
    what comes next, so the candidate waits."""
    add_match(storage)
    m = machine(storage)
    tick = clock()
    m._bingo[MATCH_ID] = bingo.BingoTracker(clock=tick)
    seed(m, storage)
    m.apply(MATCH_ID, frame(ours=7, theirs=5, rnd=13))
    m.observe_kills(MATCH_ID, [kill(40, smoke=True)])
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "smoke") == 1

    m.apply(MATCH_ID, frame(ours=0, theirs=0, rnd=1))
    tick.advance(5)
    m.apply(MATCH_ID, frame(ours=7, theirs=5, rnd=13))       # back as it was
    tick.advance(bingo.RESET_CONFIRM_SECONDS + 1)
    m.apply(MATCH_ID, frame(ours=7, theirs=5, rnd=13))
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "smoke") == 1


def test_what_was_counted_after_the_reset_survives_it(storage):
    """The confirmation comes three minutes late, by which time the real map
    is running and its kills are in the same counters. Rolling those back
    would punish the map for the warmup's sins."""
    add_match(storage)
    m = machine(storage)
    tick = clock()
    m._bingo[MATCH_ID] = bingo.BingoTracker(clock=tick)
    seed(m, storage)
    m.apply(MATCH_ID, frame(ours=1, theirs=0, rnd=2))
    m.observe_kills(MATCH_ID, [kill(40, smoke=True)])        # the warmup's
    m.apply(MATCH_ID, frame(ours=0, theirs=0, rnd=1))        # the reset
    m.observe_kills(MATCH_ID, [kill(50, smoke=True)])        # the real map's
    tick.advance(bingo.RESET_CONFIRM_SECONDS + 1)
    m.apply(MATCH_ID, frame(ours=0, theirs=0, rnd=1))
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "smoke") == 1


def test_a_confirmed_reset_asks_for_the_sent_messages_to_be_struck_through(storage):
    add_match(storage)
    m = machine(storage)
    tick = clock()
    m._bingo[MATCH_ID] = bingo.BingoTracker(clock=tick)
    seed(m, storage)
    m.apply(MATCH_ID, frame(ours=1, theirs=0, rnd=2))
    m.observe_kills(MATCH_ID, [kill(40, smoke=True)])
    m.apply(MATCH_ID, frame(ours=0, theirs=0, rnd=1))
    tick.advance(bingo.RESET_CONFIRM_SECONDS + 1)
    m.apply(MATCH_ID, frame(ours=0, theirs=0, rnd=1))
    retractions = m.take_retractions()
    assert [(match, map_number) for match, map_number, _ in retractions] \
        == [(MATCH_ID, 1)]
    # Taken once: a second call has nothing left to hand over.
    assert m.take_retractions() == []


# ------------------------------------------------------------- the events

def test_every_kill_is_its_own_moment_and_a_streak_speaks_once(storage):
    """The two aggregates part company here. Every kill through smoke is a
    thing that happened; "two rounds in a row" is the same run still going."""
    add_match(storage)
    m = machine(storage)
    seed(m, storage)
    two = m.observe_kills(MATCH_ID, [kill(40, smoke=True), kill(41, smoke=True)])
    assert len(two) == 2

    events = []
    for last in range(1, 6):
        events += [e for e in m.apply(MATCH_ID, frame(
            ours=last, theirs=0, rnd=last + 1,
            our_history=history(*([True] * last))))
            if e.payload.get("square") == "streak"]
    # Nothing at 1, 2 or 3 rounds in a row; one message at 4; nothing at 5.
    assert [e.payload["count"] for e in events] == [4]


def test_no_moment_is_born_when_nobody_wants_the_stream(storage):
    """The counting still happens — it is what the summary is built from —
    but with nobody asking for per-moment messages nothing is queued."""
    add_match(storage)
    m = LiveMachine(storage, Config(bingo=True, bingo_live=False))
    seed(m, storage)
    assert m.observe_kills(MATCH_ID, [kill(40, smoke=True)]) == []
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "smoke") == 1


def test_nothing_is_counted_at_all_when_the_card_is_off(storage):
    """With the card off the log is not parsed and no counter is written —
    the whole feature costs nothing."""
    add_match(storage)
    m = LiveMachine(storage, Config(bingo=False))
    m.apply(MATCH_ID, frame())
    assert m.observe_kills(MATCH_ID, [kill(40, smoke=True)]) == []
    assert storage.bingo_watermark(MATCH_ID) is None


def test_the_map_summary_follows_the_map_result(storage):
    add_match(storage)
    m = machine(storage)
    seed(m, storage)
    m.observe_kills(MATCH_ID, [kill(40, smoke=True)])
    events = m.apply(MATCH_ID, frame(ours=13, theirs=5, rnd=18))
    types = [e.type for e in events]
    assert "E6" in types and "E17" in types
    assert types.index("E6") < types.index("E17")
    summary = next(e for e in events if e.type == "E17")
    smoke = next(s for s in summary.payload["squares"] if s["key"] == "smoke")
    assert smoke["value"] == 1 and smoke["done"] is False
    assert summary.payload["map_squares"]["smoke"] == 1


def test_no_summary_for_a_match_the_feed_never_watched(storage):
    """A card of nine zeroes is not a summary of a match, it is a claim about
    one nobody looked at."""
    add_match(storage)
    m = machine(storage)
    events = m.apply(MATCH_ID, frame(ours=13, theirs=5, rnd=18))
    assert [e.type for e in events if e.type in ("E17", "E18")] == []


def test_the_match_summary_ticks_the_win_for_the_winner_only(storage):
    add_match(storage, lineup=("Mirage",), teams=(TEAM_ID, FOE_ID))
    m = machine(storage)
    seed(m, storage)
    # After the first frame, not before it: the format is written onto the
    # match_state row, and that row is created by the first frame.
    storage.set_best_of(MATCH_ID, 1)
    events = m.apply(MATCH_ID, frame(ours=13, theirs=5, rnd=18))
    assert "E18" in [e.type for e in events]
    ours = next(e for e in events
                if e.type == "E18" and e.payload["team_id"] == TEAM_ID)
    theirs = next(e for e in events
                  if e.type == "E18" and e.payload["team_id"] == FOE_ID)
    assert next(s for s in ours.payload["squares"] if s["key"] == "win")["done"]
    assert not next(s for s in theirs.payload["squares"]
                    if s["key"] == "win")["done"]


def test_an_ace_fills_its_square(storage):
    """Five kills in a round, read off the same tracker the multikill uses —
    with the card's own bar, which does not move when a subscriber changes
    theirs."""
    add_match(storage)
    m = machine(storage)
    seed(m, storage)
    m.apply(MATCH_ID, frame(rnd=4, our_players=players(OURS, kills=0)))
    hot = (PlayerLine(steam_id="1:0:101", nick="ace", kills=5, player_id=101),) \
        + players(OURS[1:], kills=0)
    m.apply(MATCH_ID, frame(rnd=4, our_players=hot))
    events = m.apply(MATCH_ID, frame(rnd=4, state="ended", our_players=hot))
    assert [e.payload["square"] for e in events if e.type == "E16"] == ["ace"]
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "ace") == 1


# ------------------------------------------------------------- the queue

@pytest.mark.parametrize("card,stream,wanted", [
    (1, 1, True), (1, 0, False), (0, 1, False), (0, 0, False),
])
def test_a_moment_needs_both_switches(storage, card, stream, wanted):
    """`bingo` is the card and `bingo_live` is its per-moment stream. Somebody
    who turned the card off has not asked to keep receiving its moments."""
    from hltv_notify.notify.outbox import Notifier
    storage.add_subscriber("1")
    storage.set_setting("1", "bingo", card)
    storage.set_setting("1", "bingo_live", stream)
    notifier = Notifier(storage, Config(), None)
    event = Event(type="E16", idempotency_key="k", match_id=MATCH_ID,
                  payload={"square": "smoke"})
    assert notifier._wants("1", event) is wanted


def test_a_summary_needs_only_the_card(storage):
    from hltv_notify.notify.outbox import Notifier
    storage.add_subscriber("1")
    storage.set_setting("1", "bingo", 1)
    storage.set_setting("1", "bingo_live", 0)
    notifier = Notifier(storage, Config(), None)
    for event_type in ("E17", "E18"):
        assert notifier._wants("1", Event(type=event_type, idempotency_key="k",
                                          match_id=MATCH_ID, payload={})) is True


# ----------------------------------------------------------- the messages

def rendered(event_type: str, payload: dict) -> str:
    return fmt.render(Event(type=event_type, idempotency_key="k",
                            match_id=MATCH_ID, payload=payload),
                      team_name="FORZE", tz_name="UTC")


def test_the_messages_use_only_tags_telegram_knows():
    """Replies go out with parse_mode=HTML and Telegram answers 400 to a tag
    it does not know — invisibly, from inside. Same check as test_bot.py's."""
    allowed = {"b", "/b", "i", "/i", "s", "/s", "code", "/code", "a", "/a"}
    texts = [
        rendered("E16", {"square": "smoke", "label": "4 kills through smoke",
                         "moment": "killed through smoke", "nick": "Lack1",
                         "count": 2, "target": 4, "closed": False,
                         "map_name": "Mirage", "round": 7, "team_name": "FORZE",
                         "opponent": "Color", "url": "https://x/1"}),
        rendered("E17", {"squares": bingo.card({"smoke": 4}), "closed": 1,
                         "map_squares": {"smoke": 4}, "map_number": 1,
                         "map_name": "Mirage", "team_name": "FORZE",
                         "opponent": "Color", "url": "https://x/1"}),
        rendered("E18", {"squares": bingo.card({"smoke": 4, "win": 1}),
                         "closed": 2, "team_name": "FORZE",
                         "opponent": "Color", "url": "https://x/1"}),
    ]
    for text in texts:
        for tag in __import__("re").findall(r"<\s*([^ >]+)", text):
            assert tag.split("\n")[0] in allowed, (tag, text)
        assert fmt.strike(text)


def test_a_closed_square_says_so():
    text = rendered("E16", {"square": "smoke", "label": "4 kills through smoke",
                            "moment": "killed through smoke", "nick": "Lack1",
                            "count": 4, "target": 4, "closed": True,
                            "map_name": "Mirage", "round": 7,
                            "team_name": "FORZE", "opponent": "Color",
                            "url": "https://x/1"})
    assert "square closed" in text and "4/4" in text


def test_striking_through_is_idempotent_and_keeps_the_text():
    """A struck line must not be struck again — nested tags are a 400, and the
    retraction that 400s is the one that never happens."""
    once = fmt.strike("<b>Bingo</b>\nLack1 killed through smoke")
    assert once.startswith("<s><b>Bingo</b></s>")
    assert fmt.RETRACTED_MARK in once
    assert fmt.strike(once).count("<s><b>Bingo</b></s>") == 1


# --------------------------------------------------------- the recordings

def recorded(name: str):
    """Frames and kills out of a real recording, in the order it holds them."""
    with gzip.open(FIXTURES / name, "rt", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            if record.get("kind") != "frame":
                continue
            for item in feed_items([record["raw"]]):
                yield item


def test_the_log_of_a_real_recording_parses_into_kills():
    """What the fixture actually holds, so a change in the parser is caught by
    the numbers rather than by a shrug."""
    kills = {}
    for kind, item in recorded("scorebot-2397053-forze.jsonl.gz"):
        if kind == "log":
            for one in kills_from_log(item):
                kills[one.event_id] = one
    assert len(kills) == 543
    assert sum(1 for k in kills.values() if k.through_smoke) == 30
    assert sum(1 for k in kills.values() if k.penetrated) == 24
    assert sum(1 for k in kills.values() if k.weapon == "hegrenade") == 3
    assert sum(1 for k in kills.values() if k.is_knife) == 16
    # Every kill can be attributed: `killerId` is the scoreboard's `dbId`.
    assert all(k.killer_id is not None for k in kills.values())


def test_kills_arrive_oldest_first_however_the_feed_sends_them():
    """The feed sends them newest first — every packet of both recordings is
    in descending eventId — and everything downstream reads them as a stream
    in time order."""
    for name in ("scorebot-2397053-forze.jsonl.gz",
                 "scorebot-2396936-map-boundary.jsonl.gz"):
        for kind, item in recorded(name):
            if kind == "log":
                ids = [one.event_id for one in kills_from_log(item)]
                assert ids == sorted(ids)


def test_a_recording_replayed_twice_adds_no_bingo(storage):
    """The deduplication test the whole design exists for: a reconnect sends
    the full state and the whole log backlog again."""
    from hltv_notify.replay import replay, _prepare
    _prepare(storage, 2397053)
    config = Config(team_id=TEAM_ID, bingo=True, bingo_live=True)
    first = replay(FIXTURES / "scorebot-2397053-forze.jsonl.gz",
                   storage, config, 2397053)
    assert [e.type for e in first].count("E16") > 0
    assert "E17" in [e.type for e in first]
    assert replay(FIXTURES / "scorebot-2397053-forze.jsonl.gz",
                  storage, config, 2397053) == []


def test_the_warmup_knife_rounds_of_a_real_recording_fill_nothing(storage):
    """All 16 knife kills in the forze recording are from the knife rounds
    before its two maps. Not one of them may reach the card."""
    from hltv_notify.replay import replay, _prepare
    _prepare(storage, 2397053)
    replay(FIXTURES / "scorebot-2397053-forze.jsonl.gz", storage,
           Config(team_id=TEAM_ID, bingo=True, bingo_live=True), 2397053)
    rows = storage.bingo_rows(2397053, TEAM_ID)
    assert bingo.totals(rows).get("knife", 0) == 0


# ---------------------------------------- the card when the page ends it

def test_the_page_sends_the_card_too(storage):
    """The feed only knows the match is over when the page told it the
    format. Where it did not, the page is the one that reports the finish —
    and a card only the feed could send would never arrive for that match."""
    from datetime import datetime, timezone

    from hltv_notify.sources.match_page import MapLine, MatchObservation
    from hltv_notify.state.match_machine import MatchMachine

    add_match(storage)
    m = machine(storage)
    seed(m, storage)
    m.observe_kills(MATCH_ID, [kill(40, smoke=True)])

    def observation(status, lines):
        return MatchObservation(
            match_id=MATCH_ID, status=status,
            start_utc=datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc),
            event_name="Test Event", event_id=1, best_of=1,
            team1_id=FOE_ID, team1_name="Color",
            team2_id=TEAM_ID, team2_name="FORZE Reload", maps=lines,
            scorebot_id=MATCH_ID if status == "live" else None)

    page = MatchMachine(storage, Config(team_id=TEAM_ID, bingo=True))
    live = MapLine(number=1, name="Mirage", score_left=5, score_right=13,
                   halves=None, has_stats=False)
    done = MapLine(number=1, name="Mirage", score_left=5, score_right=13,
                   halves=None, has_stats=True)
    page.apply(observation("live", [live]))
    events = page.apply(observation("over", [done]))
    summary = [e for e in events if e.type == "E18"]
    assert len(summary) == 1
    # The team is in the key: one card per tracked team, and the journal key
    # is `<chat>|<key>` — without it a subscriber following both teams of a
    # match has the second card swallowed as a duplicate of the first.
    assert summary[0].idempotency_key == f"E18:{MATCH_ID}:{TEAM_ID}:bingo"
    # And the page is the machine that ticked the win here: the feed never
    # reported this match finished, so a card built without `record_win` would
    # have gone out with "Win the match" open above an E7 announcing it.
    assert next(sq for sq in summary[0].payload["squares"]
                if sq["key"] == "win")["done"]
    smoke = next(s for s in summary[0].payload["squares"] if s["key"] == "smoke")
    assert smoke["value"] == 1


# --------------------------------------------------- retracting a message

def test_a_retraction_strikes_the_message_through_and_keeps_the_journal(storage):
    """Edited, not deleted: a message that vanishes leaves the reader
    remembering a thing that never happened. And the row stays 'sent', or the
    queue would deliver it a second time."""
    from hltv_notify.notify.outbox import Notifier

    class FakeTelegram:
        def __init__(self):
            self.edits = []

        async def send_message(self, chat_id, text, **kwargs):
            return 5000 + len(self.edits)

        async def edit_message_text(self, chat_id, message_id, text, **kwargs):
            self.edits.append((chat_id, message_id, text))

    add_match(storage)
    storage.add_subscriber("1")
    storage.add_team("1", TEAM_ID, "forze", "FORZE")
    storage.set_setting("1", "bingo", 1)
    storage.set_setting("1", "bingo_live", 1)
    telegram = FakeTelegram()
    notifier = Notifier(storage, Config(dry_run=False), telegram)
    notifier.enqueue(Event(
        type="E16", idempotency_key="E16:777:map:1:k", match_id=MATCH_ID,
        payload={"square": "knife", "label": "A knife kill",
                 "moment": "killed with a knife", "nick": "Lack1", "count": 1,
                 "target": 1, "closed": True, "map_number": 1,
                 "map_name": "Mirage", "round": 2, "team_id": TEAM_ID,
                 "team_name": "FORZE", "opponent": "Color", "url": "https://x/1"}))
    import asyncio
    asyncio.run(notifier._drain())
    assert storage.pending_count() == 0

    import datetime as _dt
    later = (utcnow() + _dt.timedelta(minutes=5)).isoformat()
    assert asyncio.run(notifier.retract(MATCH_ID, 1, later)) == 1
    assert len(telegram.edits) == 1
    assert "<s>" in telegram.edits[0][2]
    assert fmt.RETRACTED_MARK in telegram.edits[0][2]
    # The journal is untouched, so nothing is sent again...
    assert storage.sent_event_count() == 1
    assert storage.pending_count() == 0
    # ...and a second retraction has nothing left to do.
    assert asyncio.run(notifier.retract(MATCH_ID, 1, later)) == 0


def test_a_dry_run_retraction_is_visible_in_the_log(storage, caplog):
    """A run against live HLTV in DRY_RUN is how this project checks itself,
    and nothing is sent to Telegram there — the rows are marked sent with a
    NULL message id. Requiring one would make the retraction invisible in the
    one place it can be verified."""
    import asyncio
    import datetime as _dt
    import logging

    from hltv_notify.notify.outbox import Notifier

    add_match(storage)
    storage.add_subscriber("1")
    storage.add_team("1", TEAM_ID, "forze", "FORZE")
    storage.set_setting("1", "bingo", 1)
    storage.set_setting("1", "bingo_live", 1)
    notifier = Notifier(storage, Config(dry_run=True), None)
    notifier.enqueue(Event(
        type="E16", idempotency_key="E16:777:map:1:k", match_id=MATCH_ID,
        payload={"square": "knife", "label": "A knife kill",
                 "moment": "killed with a knife", "nick": "Lack1", "count": 1,
                 "target": 1, "closed": True, "map_number": 1,
                 "map_name": "Mirage", "round": 2, "team_id": TEAM_ID,
                 "team_name": "FORZE", "opponent": "Color", "url": "https://x/1"}))
    asyncio.run(notifier._drain())

    later = (utcnow() + _dt.timedelta(minutes=5)).isoformat()
    with caplog.at_level(logging.INFO, logger="hltv_notify.notify.outbox"):
        assert asyncio.run(notifier.retract(MATCH_ID, 1, later)) == 1
    assert any("[retracted]" in record.message for record in caplog.records)
    # And the row is marked, so a second pass has nothing left to do.
    assert asyncio.run(notifier.retract(MATCH_ID, 1, later)) == 0


# ------------------------------------- the service lying down mid-match

def restarted(storage) -> LiveMachine:
    """A brand-new machine on the same database — a process restart, a worker
    re-created by `reconcile`, or the far side of a 403 cooldown. Everything
    the tracker held in memory is gone; everything in the database is not."""
    return machine(storage)


def test_a_restart_in_the_middle_of_a_map_doubles_nothing(storage):
    """The one failure that would be invisible: every square keeps counting
    from where the database left it, and the squares RECOMPUTED from the
    frame's history recompute to the same number rather than adding it again."""
    add_match(storage)
    won = history(*([True] * 5))
    before = machine(storage)
    seed(before, storage)
    before.apply(MATCH_ID, frame(ours=5, theirs=0, rnd=6, our_history=won))
    before.observe_kills(MATCH_ID, [kill(40, smoke=True), kill(41, wall=True)])
    snapshot = bingo.totals(storage.bingo_rows(MATCH_ID, TEAM_ID))
    assert snapshot["smoke"] == 1 and snapshot["pistol"] == 1
    assert snapshot["streak"] == 5

    # Down, and up again on the same database.
    after = restarted(storage)
    for _ in range(3):
        after.apply(MATCH_ID, frame(ours=5, theirs=0, rnd=6, our_history=won))
    assert bingo.totals(storage.bingo_rows(MATCH_ID, TEAM_ID)) == snapshot


def test_kills_missed_while_the_service_was_down_are_counted_once(storage):
    """The backlog replayed on the next connect is how they arrive, and the
    watermark is in the database, so they are new exactly once."""
    add_match(storage)
    before = machine(storage)
    seed(before, storage)
    before.observe_kills(MATCH_ID, [kill(40, smoke=True)])
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "smoke") == 1

    after = restarted(storage)
    after.apply(MATCH_ID, frame(ours=3, theirs=1, rnd=5))
    # The whole backlog: what we saw, plus what happened while we were down.
    after.observe_kills(MATCH_ID, [kill(1), kill(40, smoke=True),
                                   kill(55, smoke=True), kill(56, wall=True)])
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "smoke") == 2
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "wallbang") == 1
    # And the same backlog once more changes nothing.
    after.observe_kills(MATCH_ID, [kill(40, smoke=True), kill(55, smoke=True),
                                   kill(56, wall=True)])
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "smoke") == 2


def test_a_restart_repeats_the_keys_rather_than_the_messages(storage):
    """Nothing in the machine remembers what was SENT — the journal does. So
    what a restart has to guarantee is that the same fact produces the same
    idempotency key, or the unique index has nothing to catch it by.

    The two aggregates answer this differently, and both answers are right. A
    counted square re-announces its step under the key it used before, and the
    journal swallows it. A square whose match answer is the best map's does not
    re-announce at all: it speaks on the step that takes the match from open to
    closed, and after a restart the database already says closed — so the
    message is never built, which is one fewer thing to depend on the journal
    for."""
    add_match(storage)
    won = history(*([True] * 4))
    before = machine(storage)
    seed(before, storage)
    first = before.apply(MATCH_ID, frame(ours=4, theirs=0, rnd=5, our_history=won))
    said = {e.payload["square"]: e.idempotency_key for e in first if e.type == "E16"}
    assert said.keys() == {"pistol", "streak"}

    after = restarted(storage)
    again = after.apply(MATCH_ID, frame(ours=4, theirs=0, rnd=5, our_history=won))
    repeated = {e.payload["square"]: e.idempotency_key for e in again if e.type == "E16"}
    assert repeated == {"pistol": said["pistol"]}


def test_the_map_summary_survives_a_restart_unchanged(storage):
    add_match(storage)
    before = machine(storage)
    seed(before, storage)
    before.observe_kills(MATCH_ID, [kill(40, smoke=True), kill(41, smoke=True)])

    after = restarted(storage)
    events = after.apply(MATCH_ID, frame(ours=13, theirs=5, rnd=18))
    summary = next(e for e in events if e.type == "E17")
    smoke = next(s for s in summary.payload["squares"] if s["key"] == "smoke")
    assert smoke["value"] == 2
    assert summary.idempotency_key == f"E17:{MATCH_ID}:map:1:{TEAM_ID}:bingo"


def test_a_reset_that_happened_entirely_while_we_were_down_is_not_seen(storage):
    """The known cost, asserted so it is a decision and not a surprise.

    The reset watch is in memory, and it works by seeing the score fall. A
    service that was down for the whole of the knife round AND the reset comes
    back to a map at 0:0 with nothing to compare it against, while the
    backlog hands it the kills from before the reset. They are counted.

    What keeps this narrow: the knife kills still go (a round of nothing but
    knives is dropped whatever the reset watch thinks), and the service has to
    be down across both events. It is written up in the limitations.
    """
    add_match(storage)
    before = machine(storage)
    seed(before, storage)
    before.apply(MATCH_ID, frame(ours=1, theirs=0, rnd=2))        # knife round

    after = restarted(storage)
    after.apply(MATCH_ID, frame(ours=0, theirs=0, rnd=1))         # already reset
    after.observe_kills(MATCH_ID, [kill(60, smoke=True),           # from the warmup
                                   kill(61, weapon="knife_t")])
    after.apply(MATCH_ID, frame(ours=0, theirs=0, rnd=1))
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "smoke") == 1   # the cost
    assert storage.bingo_value(MATCH_ID, 1, TEAM_ID, "knife") == 0   # still safe
