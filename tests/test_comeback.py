"""Comebacks: the swing in the score difference, not a streak.

The two shapes the owner asked for are both here: ten rounds without reply,
and twelve taken with two given away. A streak counter would have found only
the first.
"""

from hltv_notify.state.comeback import ComebackTracker


def play(rounds: str, threshold: int = 9) -> ComebackTracker:
    """`a` is a round for us, `b` for them. The tracker sees every score."""
    tracker = ComebackTracker(threshold)
    ours = theirs = 0
    tracker.observe(ours, theirs)
    for char in rounds:
        if char == "a":
            ours += 1
        else:
            theirs += 1
        tracker.observe(ours, theirs)
    return tracker


def final(rounds: str):
    return rounds.count("a"), rounds.count("b")


def verdict(rounds: str, threshold: int = 9, overtime: bool = False):
    return play(rounds, threshold).verdict(*final(rounds), overtime=overtime)


def test_ten_rounds_without_reply():
    """3:11 down, 13:11 up."""
    got = verdict("aaa" + "b" * 11 + "a" * 10)
    assert got["comeback_from_team"] == 3
    assert got["comeback_from_opponent"] == 11
    assert got["comeback_swing"] == 10
    assert got["comeback_result"] == "won"


def test_twelve_taken_with_two_given_away():
    """1:7 down, 13:9 up — the case a streak counter cannot see."""
    got = verdict("a" + "b" * 7 + "a" * 6 + "b" + "a" * 3 + "b" + "a" * 3)
    assert (got["comeback_from_team"], got["comeback_from_opponent"]) == (1, 7)
    assert got["comeback_swing"] == 10
    assert got["comeback_result"] == "won"


def test_a_blowout_is_not_a_comeback():
    """13:1 after conceding the opening round is a rout, not a comeback: the
    swing is twelve and there was never a hole to climb out of."""
    assert verdict("b" + "a" * 13) is None


def test_a_small_swing_is_not_a_comeback():
    """6:9 down, 13:9 up — seven, below the threshold."""
    assert verdict("ab" * 6 + "bbb" + "a" * 7) is None


def test_a_comeback_stopped_in_overtime():
    """1:10 down, level at 12:12, and the map still lost."""
    rounds = "a" + "b" * 10 + "a" * 11 + "b" * 2 + "b" * 4
    got = verdict(rounds, overtime=True)
    assert (got["comeback_from_team"], got["comeback_from_opponent"]) == (1, 10)
    assert got["comeback_result"] == "stopped"
    assert got["comeback_overtime"] is True


def test_the_opponents_comeback_is_reported_too():
    """We led 10:1, they got to 10:12, we took it 13:12. Their run is the
    story of the map, and it was denied."""
    got = verdict("a" * 5 + "b" + "a" * 5 + "b" * 11 + "a" * 3)
    assert (got["comeback_from_team"], got["comeback_from_opponent"]) == (10, 1)
    assert got["comeback_swing"] == 11
    assert got["comeback_result"] == "stopped"


def test_the_bigger_run_is_the_story():
    """Both sides had one; the map belongs to the bigger."""
    # They come back from 0:9 to 10:9 (a run of 10 for them, counted our way
    # as a fall), then we take five in a row — a rise of five.
    rounds = "a" * 9 + "b" * 10 + "a" * 4
    got = verdict(rounds)
    assert got["comeback_swing"] == 10
    assert (got["comeback_from_team"], got["comeback_from_opponent"]) == (9, 0)


def test_zero_switches_it_off():
    assert verdict("aaa" + "b" * 11 + "a" * 10, threshold=0) is None


def test_a_map_without_a_run_says_nothing():
    assert verdict("ab" * 4 + "a" * 9) is None


def test_the_deficit_floor_follows_the_threshold():
    """Half the swing is the smallest hole worth the word."""
    assert ComebackTracker(9).min_deficit == 4
    assert ComebackTracker(4).min_deficit == 2
    # Never below two: "a comeback from 0:1" is not a phrase.
    assert ComebackTracker(2).min_deficit == 2
