"""Round highlights for players of a tracked team: multikills and clutches.

The contract these check is "the round is resolved ONCE" — at its end, or the
moment a player can no longer add to it by dying. Every alert the old
mid-round tracker produced is still here, it simply arrives at the end of the
round it belongs to.
"""

import pytest

from hltv_notify.sources.scorebot import PlayerLine
from hltv_notify.state.highlights import RoundTracker

MAP = "Mirage"


def line(nick, kills, *, alive=True, clutches=0) -> PlayerLine:
    return PlayerLine(steam_id=nick, nick=nick, kills=kills,
                      alive=alive, clutches=clutches)


def players(**kills) -> list:
    """Everybody alive and nobody clutching — the ordinary frame."""
    return [line(nick, value) for nick, value in kills.items()]


def told(found) -> list:
    return [(h.player.nick, h.kills, h.clutch_against) for h in found]


def tracker(threshold=4, clutch=0) -> RoundTracker:
    """Clutches off unless a test asks for them, so the multikill cases stay
    about the multikill."""
    return RoundTracker(multikill=threshold, clutch=clutch)


# --------------------------------------------------------------------------
# The multikill, as it always worked, now reported at the end of the round.

def test_new_round_only_sets_the_baseline():
    t = tracker()
    assert t.observe(MAP, 5, "started", players(ropz=10, ZywOo=8)) == []


def test_four_kills_in_a_round_are_reported_when_it_ends():
    t = tracker()
    t.observe(MAP, 5, "started", players(ropz=10, ZywOo=8))
    assert t.observe(MAP, 5, "started", players(ropz=14, ZywOo=8)) == []
    found = t.observe(MAP, 5, "ended", players(ropz=14, ZywOo=8))
    assert told(found) == [("ropz", 4, 0)]


def test_the_report_happens_once():
    """A frame arrives several times a second, and `ended` stands for many."""
    t = tracker()
    t.observe(MAP, 5, "started", players(ropz=10))
    assert len(t.observe(MAP, 5, "ended", players(ropz=14))) == 1
    for _ in range(10):
        assert t.observe(MAP, 5, "ended", players(ropz=14)) == []


def test_an_ace_is_one_message_and_not_two():
    """The old tracker alerted at the bar and again at the ace. A round that
    ends in an ace is one moment and gets one message, saying five."""
    t = tracker(threshold=4)
    t.observe(MAP, 5, "started", players(ropz=10))
    assert t.observe(MAP, 5, "started", players(ropz=14)) == []   # the bar, held
    assert t.observe(MAP, 5, "started", players(ropz=15)) == []
    assert told(t.observe(MAP, 5, "ended", players(ropz=15))) == [("ropz", 5, 0)]


def test_three_kills_are_not_reported():
    t = tracker()
    t.observe(MAP, 5, "started", players(ropz=10))
    assert t.observe(MAP, 5, "ended", players(ropz=13)) == []


def test_kills_do_not_leak_between_rounds():
    """Kills in a frame are accumulated FOR THE MAP, so without resetting the
    baseline every subsequent round would look like a multikill."""
    t = tracker()
    t.observe(MAP, 5, "started", players(ropz=10))
    t.observe(MAP, 5, "ended", players(ropz=14))
    t.observe(MAP, 6, "freezePeriod", players(ropz=14))     # a new round
    assert t.observe(MAP, 6, "started", players(ropz=16)) == []
    assert len(t.observe(MAP, 6, "ended", players(ropz=18))) == 1


def test_new_map_resets_everything():
    t = tracker()
    t.observe(MAP, 5, "started", players(ropz=10))
    t.observe("Nuke", 1, "started", players(ropz=0))
    assert t.observe("Nuke", 1, "ended", players(ropz=3)) == []


def test_warmup_kills_are_ignored():
    """Warmup is deathmatch, and kills from it have nothing to do with the round."""
    t = tracker()
    t.observe("Nuke", 1, "warmup", players(ropz=0))
    assert t.observe("Nuke", 1, "warmup", players(ropz=25)) == []
    # after the warmup, multikills are counted from the current baseline
    assert t.observe("Nuke", 1, "ended", players(ropz=27)) == []


def test_player_appearing_mid_round_is_not_credited():
    """A substitution or a reconnect must not look like a multikill."""
    t = tracker()
    t.observe(MAP, 5, "started", players(ropz=10))
    assert t.observe(MAP, 5, "started", players(ropz=10, newcomer=30)) == []
    found = t.observe(MAP, 5, "ended", players(ropz=10, newcomer=34))
    assert told(found) == [("newcomer", 4, 0)]


def test_threshold_is_configurable():
    t = tracker(threshold=3)
    t.observe(MAP, 5, "started", players(ropz=10))
    assert len(t.observe(MAP, 5, "ended", players(ropz=13))) == 1


def test_threshold_has_a_sane_floor():
    """A threshold of 1 would turn the alert into a firehose."""
    assert tracker(threshold=1).multikill == 2


def test_zero_is_off_and_is_not_floored():
    """Zero means nobody is waiting for one — it must not become the minimum,
    or one person switching multikills off would switch them on again at 2."""
    t = tracker(threshold=0)
    assert t.multikill == 0
    t.observe(MAP, 5, "started", players(ropz=10))
    assert t.observe(MAP, 5, "ended", players(ropz=15)) == []


def test_reconnect_can_miss_but_never_invents():
    """After a reconnect mid-round the baseline is taken afresh. A multikill
    may be missed — a deliberate trade, but there are never false alerts."""
    t = tracker()
    t.observe(MAP, 5, "started", players(ropz=10))
    t.observe(MAP, 5, "started", players(ropz=13))
    fresh = tracker()                       # as if the worker had been recreated
    fresh.observe(MAP, 5, "started", players(ropz=13))
    assert fresh.observe(MAP, 5, "ended", players(ropz=14)) == []


# --------------------------------------------------------------------------
# Not waiting for a round a player can no longer add to.

def test_a_dead_player_is_reported_at_once():
    """His kills are final the moment he dies, and the alert exists to be acted
    on while the moment is still clippable."""
    t = tracker(threshold=3)
    t.observe(MAP, 5, "started", players(ropz=10))
    found = t.observe(MAP, 5, "started", [line("ropz", 13, alive=False)])
    assert told(found) == [("ropz", 3, 0)]


def test_a_player_reported_on_death_is_not_reported_again():
    t = tracker(threshold=3)
    t.observe(MAP, 5, "started", players(ropz=10))
    assert len(t.observe(MAP, 5, "started", [line("ropz", 13, alive=False)])) == 1
    assert t.observe(MAP, 5, "started", [line("ropz", 13, alive=False)]) == []
    assert t.observe(MAP, 5, "ended", [line("ropz", 13, alive=False)]) == []


def test_a_living_player_still_waits():
    """He may yet take a fourth, or the round may yet become a clutch."""
    t = tracker(threshold=3)
    t.observe(MAP, 5, "started", players(ropz=10))
    assert t.observe(MAP, 5, "started", players(ropz=13)) == []


def test_the_last_man_alive_is_not_reported_early_even_once_dead():
    """A clutch can be CREDITED after he dies — the bomb he planted goes off.

    Reporting a bare multikill there would produce exactly the split message
    this rewrite exists to prevent.
    """
    t = tracker(threshold=3, clutch=2)
    t.observe(MAP, 5, "started", players(ropz=10, mate=4))
    # ropz alone against two
    t.observe(MAP, 5, "started",
              [line("ropz", 13), line("mate", 4, alive=False)],
              [line("a", 0), line("b", 0)])
    # and now he dies too — nothing yet
    assert t.observe(MAP, 5, "started",
                     [line("ropz", 13, alive=False), line("mate", 4, alive=False)],
                     [line("a", 0), line("b", 0)]) == []
    # the bomb goes off, HLTV credits the 1v2
    found = t.observe(MAP, 5, "ended",
                      [line("ropz", 13, clutches=1, alive=False),
                       line("mate", 4, alive=False)])
    assert told(found) == [("ropz", 3, 2)]


# --------------------------------------------------------------------------
# Clutches.

def test_a_clutch_is_reported_with_the_number_it_was_against():
    t = tracker(threshold=0, clutch=2)
    t.observe(MAP, 5, "started", players(ropz=10, mate=4))
    t.observe(MAP, 5, "started",
              [line("ropz", 10), line("mate", 4, alive=False)],
              [line("a", 0), line("b", 0), line("c", 0)])
    found = t.observe(MAP, 5, "ended",
                      [line("ropz", 12, clutches=1), line("mate", 4)])
    assert told(found) == [("ropz", 2, 3)]


def test_a_clutch_below_the_bar_is_not_reported():
    t = tracker(threshold=0, clutch=3)
    t.observe(MAP, 5, "started", players(ropz=10))
    t.observe(MAP, 5, "started", [line("ropz", 10)], [line("a", 0)])
    assert t.observe(MAP, 5, "ended", [line("ropz", 11, clutches=1)]) == []


def test_a_clutch_won_with_no_kills_still_counts():
    """The bomb ran down, or he defused it as the last one alive. HLTV counts
    it, and it is a moment worth watching."""
    t = tracker(threshold=0, clutch=2)
    t.observe(MAP, 5, "started", players(ropz=10))
    t.observe(MAP, 5, "started", [line("ropz", 10)], [line("a", 0), line("b", 0)])
    found = t.observe(MAP, 5, "ended", [line("ropz", 10, clutches=1)])
    assert told(found) == [("ropz", 0, 2)]


def test_a_clutch_whose_standoff_was_never_seen_is_dropped():
    """HLTV says a round was won alone and we never saw it happen — frames lost
    across a reconnect. The bar is expressed entirely in N, so there is nothing
    honest to print: dropping is right where guessing is wrong."""
    t = tracker(threshold=0, clutch=1)
    t.observe(MAP, 5, "started", players(ropz=10))
    assert t.observe(MAP, 5, "ended", [line("ropz", 11, clutches=1)]) == []


def test_the_standoff_is_only_counted_while_the_round_is_started():
    """At half time the CT and TERRORIST arrays swap under us during `ended`
    and the alive counts pass through (1, 1) — a forged 1v1."""
    t = tracker(threshold=0, clutch=1)
    t.observe(MAP, 12, "started", players(ropz=10, mate=4))
    # the swap, seen only in `ended`
    t.observe(MAP, 12, "ended", [line("ropz", 10), line("mate", 4, alive=False)],
              [line("a", 0)])
    assert t.observe(MAP, 12, "ended", [line("ropz", 10, clutches=1)]) == []


def test_the_largest_standoff_of_the_round_is_the_one_reported():
    """The count only falls as the clutch is played out, so the maximum is the
    number the situation opened at."""
    t = tracker(threshold=0, clutch=2)
    t.observe(MAP, 5, "started", players(ropz=10))
    t.observe(MAP, 5, "started", [line("ropz", 10)],
              [line("a", 0), line("b", 0), line("c", 0)])
    t.observe(MAP, 5, "started", [line("ropz", 12)], [line("a", 0)])
    found = t.observe(MAP, 5, "ended", [line("ropz", 13, clutches=1)])
    assert told(found) == [("ropz", 3, 3)]


def test_a_lost_clutch_is_not_a_clutch():
    """`oneOnXWins` counts WINS. Standing alone against three and dying is not
    a highlight."""
    t = tracker(threshold=0, clutch=2)
    t.observe(MAP, 5, "started", players(ropz=10))
    t.observe(MAP, 5, "started", [line("ropz", 10)], [line("a", 0), line("b", 0)])
    assert t.observe(MAP, 5, "ended", [line("ropz", 11, alive=False)]) == []


def test_clutches_do_not_leak_between_rounds():
    """`oneOnXWins` is accumulated over the MAP, exactly like the kills."""
    t = tracker(threshold=0, clutch=1)
    t.observe(MAP, 5, "started", players(ropz=10))
    t.observe(MAP, 5, "started", [line("ropz", 10)], [line("a", 0)])
    assert len(t.observe(MAP, 5, "ended", [line("ropz", 11, clutches=1)])) == 1
    t.observe(MAP, 6, "freezePeriod", [line("ropz", 11, clutches=1)])
    t.observe(MAP, 6, "started", [line("ropz", 11, clutches=1)], [line("a", 0)])
    assert t.observe(MAP, 6, "ended", [line("ropz", 12, clutches=1)]) == []


def test_both_facts_ride_in_one_highlight():
    """The case the feature was asked for: a kill early, then a 1v2 taken with
    two more — four kills and a clutch, one message."""
    t = tracker(threshold=4, clutch=2)
    t.observe(MAP, 5, "started", players(ropz=10, mate=4))
    t.observe(MAP, 5, "started",
              [line("ropz", 12), line("mate", 4, alive=False)],
              [line("a", 0), line("b", 0)])
    found = t.observe(MAP, 5, "ended",
                      [line("ropz", 14, clutches=1), line("mate", 4, alive=False)])
    assert told(found) == [("ropz", 4, 2)]


def test_a_round_that_clears_neither_bar_says_nothing():
    t = tracker(threshold=4, clutch=3)
    t.observe(MAP, 5, "started", players(ropz=10))
    t.observe(MAP, 5, "started", [line("ropz", 11)], [line("a", 0)])
    assert t.observe(MAP, 5, "ended", [line("ropz", 12, clutches=1)]) == []


# --------------------------------------------------------------------------
# The round that never ended.

def test_a_round_whose_end_was_missed_is_reported_when_it_is_left_behind():
    """A dropped connection over the round boundary, or an `ended` we never
    saw. The next round arriving is the last moment this can be told."""
    t = tracker(threshold=3)
    t.observe(MAP, 5, "started", players(ropz=10))
    t.observe(MAP, 5, "started", players(ropz=13))
    found = t.observe(MAP, 6, "freezePeriod", players(ropz=13))
    assert told(found) == [("ropz", 3, 0)]


def test_leaving_a_round_behind_does_not_report_it_twice():
    t = tracker(threshold=3)
    t.observe(MAP, 5, "started", players(ropz=10))
    assert len(t.observe(MAP, 5, "ended", players(ropz=13))) == 1
    assert t.observe(MAP, 6, "freezePeriod", players(ropz=13)) == []


# --------------------------------------------------------------------------
# A highlight carries its OWN round, not the frame's.

def test_a_late_report_names_the_round_it_belongs_to():
    """The round whose `ended` was lost is reported when the next one starts.

    Taking the round off the frame that triggers the flush named the NEXT one
    — in the message and in the idempotency key alike.
    """
    t = tracker(threshold=3)
    t.observe(MAP, 12, "started", players(ropz=10), score=(7, 5))
    t.observe(MAP, 12, "started", players(ropz=13), score=(7, 5))
    found = t.observe(MAP, 13, "freezePeriod", players(ropz=13), score=(8, 5))
    assert len(found) == 1
    assert found[0].round_number == 12
    assert found[0].map_name == MAP
    # ...and the score round 12 was played at, not the one after it.
    assert (found[0].score_team, found[0].score_opponent) == (7, 5)


def test_a_report_at_the_end_of_its_own_round_is_stamped_with_it():
    t = tracker(threshold=3)
    t.observe(MAP, 12, "started", players(ropz=10), score=(7, 5))
    found = t.observe(MAP, 12, "ended", players(ropz=13), score=(8, 5))
    assert (found[0].round_number, found[0].map_name) == (12, MAP)
    assert (found[0].score_team, found[0].score_opponent) == (8, 5)


def test_a_death_report_carries_the_round_it_happened_in():
    t = tracker(threshold=3)
    t.observe(MAP, 12, "started", players(ropz=10), score=(7, 5))
    found = t.observe(MAP, 12, "started", [line("ropz", 13, alive=False)], score=(7, 5))
    assert (found[0].round_number, found[0].score_team) == (12, 7)


def test_a_highlight_without_a_score_says_so_rather_than_guessing():
    """Nothing calls `observe` without a score today, but a None must travel as
    None so the caller can fall back rather than print a zero."""
    t = tracker(threshold=3)
    t.observe(MAP, 12, "started", players(ropz=10))
    found = t.observe(MAP, 12, "ended", players(ropz=13))
    assert found[0].score_team is None and found[0].score_opponent is None
