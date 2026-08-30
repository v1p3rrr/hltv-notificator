"""Multikill alerts for players of a tracked team."""

import pytest

from hltv_notify.sources.scorebot import PlayerLine
from hltv_notify.state.multikill import MultikillTracker

MAP = "Mirage"


def players(**kills) -> list:
    return [PlayerLine(steam_id=nick, nick=nick, kills=value)
            for nick, value in kills.items()]


def tracker(threshold=4) -> MultikillTracker:
    return MultikillTracker(threshold)


def test_new_round_only_sets_the_baseline():
    t = tracker()
    assert t.observe(MAP, 5, "started", players(ropz=10, ZywOo=8)) == []


def test_four_kills_in_a_round_alert():
    t = tracker()
    t.observe(MAP, 5, "started", players(ropz=10, ZywOo=8))
    found = t.observe(MAP, 5, "started", players(ropz=14, ZywOo=8))
    assert [(p.nick, kills) for p, kills in found] == [("ropz", 4)]


def test_alert_fires_once_per_round():
    """A frame arrives several times a second — the report must happen once."""
    t = tracker()
    t.observe(MAP, 5, "started", players(ropz=10))
    assert len(t.observe(MAP, 5, "started", players(ropz=14))) == 1
    for _ in range(10):
        assert t.observe(MAP, 5, "started", players(ropz=14)) == []


def test_ace_gets_its_own_alert():
    t = tracker()
    t.observe(MAP, 5, "started", players(ropz=10))
    assert len(t.observe(MAP, 5, "started", players(ropz=14))) == 1
    found = t.observe(MAP, 5, "started", players(ropz=15))
    assert [(p.nick, kills) for p, kills in found] == [("ropz", 5)]


def test_jump_straight_to_ace_alerts_once():
    """Between two frames a player can jump straight to five — one message."""
    t = tracker()
    t.observe(MAP, 5, "started", players(ropz=10))
    found = t.observe(MAP, 5, "started", players(ropz=15))
    assert [(p.nick, kills) for p, kills in found] == [("ropz", 5)]


def test_three_kills_are_not_reported():
    t = tracker()
    t.observe(MAP, 5, "started", players(ropz=10))
    assert t.observe(MAP, 5, "started", players(ropz=13)) == []


def test_kills_do_not_leak_between_rounds():
    """Kills in a frame are accumulated FOR THE MAP, so without resetting the
    baseline every subsequent round would look like a multikill."""
    t = tracker()
    t.observe(MAP, 5, "started", players(ropz=10))
    t.observe(MAP, 5, "started", players(ropz=14))
    t.observe(MAP, 6, "freezePeriod", players(ropz=14))     # a new round
    assert t.observe(MAP, 6, "started", players(ropz=16)) == []
    assert len(t.observe(MAP, 6, "started", players(ropz=18))) == 1


def test_new_map_resets_everything():
    t = tracker()
    t.observe(MAP, 5, "started", players(ropz=10))
    t.observe("Nuke", 1, "started", players(ropz=0))
    assert t.observe("Nuke", 1, "started", players(ropz=3)) == []


def test_warmup_kills_are_ignored():
    """Warmup is deathmatch, and kills from it have nothing to do with the round."""
    t = tracker()
    t.observe("Nuke", 1, "warmup", players(ropz=0))
    assert t.observe("Nuke", 1, "warmup", players(ropz=25)) == []
    # after the warmup, multikills are counted from the current baseline
    assert t.observe("Nuke", 1, "started", players(ropz=27)) == []


def test_player_appearing_mid_round_is_not_credited():
    """A substitution or a reconnect must not look like a multikill."""
    t = tracker()
    t.observe(MAP, 5, "started", players(ropz=10))
    assert t.observe(MAP, 5, "started", players(ropz=10, newcomer=30)) == []
    assert t.observe(MAP, 5, "started", players(ropz=10, newcomer=34))[0][0].nick == "newcomer"


def test_threshold_is_configurable():
    t = tracker(threshold=3)
    t.observe(MAP, 5, "started", players(ropz=10))
    assert len(t.observe(MAP, 5, "started", players(ropz=13))) == 1


def test_threshold_has_a_sane_floor():
    """A threshold of 1 would turn the alert into a firehose."""
    assert tracker(threshold=1).threshold == 2
    assert tracker(threshold=0).levels == [2, 5]


def test_reconnect_can_miss_but_never_invents():
    """After a reconnect mid-round the baseline is taken afresh. A multikill
    may be missed — a deliberate trade, but there are never false alerts."""
    t = tracker()
    t.observe(MAP, 5, "started", players(ropz=10))
    t.observe(MAP, 5, "started", players(ropz=13))
    fresh = tracker()                       # as if the worker had been recreated
    fresh.observe(MAP, 5, "started", players(ropz=13))
    assert fresh.observe(MAP, 5, "started", players(ropz=14)) == []
