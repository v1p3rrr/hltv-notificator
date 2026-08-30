"""The end of a map from the score. Critical: it must not break on MR3 overtimes.

The thresholds are derived from the format the source itself reports, not
hardcoded as a number. That is why non-standard formats are checked here too.
"""

import pytest

from hltv_notify.scoring import map_completed, rounds_to_win

MR12 = {"regulation": 12, "overtime": 3}


# ---------------------------------------------------------------- regulation

@pytest.mark.parametrize("a,b", [(13, 0), (13, 5), (13, 11), (11, 13), (0, 13)])
def test_regulation_win(a, b):
    verdict = map_completed(a, b, **MR12)
    assert verdict.completed is True
    assert verdict.overtime_number == 0


@pytest.mark.parametrize("a,b", [(0, 0), (5, 7), (11, 11), (12, 11), (11, 12)])
def test_regulation_still_running(a, b):
    assert map_completed(a, b, **MR12).completed is False


def test_twelve_all_is_not_a_finished_map():
    """12:12 is the start of an overtime, not the end of the map. The naive
    "whoever reaches 13 first" breaks right here."""
    assert map_completed(12, 12, **MR12).completed is False


# ---------------------------------------------------------------- overtimes

@pytest.mark.parametrize("a,b", [(13, 12), (14, 12), (15, 12), (14, 14), (15, 14)])
def test_first_overtime_still_running(a, b):
    """In an MR3 overtime the winner is whoever takes 4 rounds out of 6, that
    is, reaches 16. Anything less means the game goes on."""
    assert map_completed(a, b, **MR12).completed is False


@pytest.mark.parametrize("a,b", [(16, 12), (16, 13), (16, 14), (14, 16)])
def test_first_overtime_won(a, b):
    verdict = map_completed(a, b, **MR12)
    assert verdict.completed is True
    assert verdict.overtime_number == 1


def test_fifteen_all_goes_to_a_second_overtime():
    assert map_completed(15, 15, **MR12).completed is False


@pytest.mark.parametrize("a,b", [(16, 15), (17, 15), (18, 17), (18, 18)])
def test_second_overtime_still_running(a, b):
    assert map_completed(a, b, **MR12).completed is False


@pytest.mark.parametrize("a,b", [(19, 17), (19, 16), (17, 19)])
def test_second_overtime_won(a, b):
    verdict = map_completed(a, b, **MR12)
    assert verdict.completed is True
    assert verdict.overtime_number == 2


def test_third_overtime():
    assert map_completed(21, 18, **MR12).completed is False
    assert map_completed(22, 20, **MR12).completed is True
    assert map_completed(22, 20, **MR12).overtime_number == 3


def test_long_overtime_chain_does_not_drift():
    """The thresholds must step exactly by `overtime`: 13, 16, 19, 22, 25, 28..."""
    thresholds = []
    for high in range(13, 40):
        low = high - 2
        if map_completed(high, low, **MR12).completed:
            thresholds.append(high)
    assert thresholds[:6] == [13, 16, 19, 22, 25, 28]


# ---------------------------------------------------------------- formats

def test_legacy_mr15():
    """The old format: regulation up to 16, the same MR3 overtimes."""
    mr15 = {"regulation": 15, "overtime": 3}
    assert map_completed(16, 14, **mr15).completed is True
    assert map_completed(15, 15, **mr15).completed is False
    assert map_completed(16, 15, **mr15).completed is False   # already overtime
    assert map_completed(19, 17, **mr15).completed is True


def test_short_format():
    """The short MR8 format: a win at 9."""
    short = {"regulation": 8, "overtime": 3}
    assert map_completed(9, 7, **short).completed is True
    assert map_completed(8, 8, **short).completed is False
    assert map_completed(12, 10, **short).completed is True


def test_overtime_length_is_taken_from_the_source():
    """If a tournament plays MR5 overtimes, the thresholds must shift."""
    mr12_ot5 = {"regulation": 12, "overtime": 5}
    assert map_completed(16, 14, **mr12_ot5).completed is False   # not enough for MR5
    assert map_completed(18, 16, **mr12_ot5).completed is True


# ---------------------------------------------------------------- edge cases

@pytest.mark.parametrize("a,b", [(None, 13), (13, None), (None, None)])
def test_missing_score_is_not_a_finished_map(a, b):
    assert map_completed(a, b, **MR12).completed is False


def test_nonsense_format_does_not_crash():
    assert map_completed(13, 5, regulation=0, overtime=0).completed is False


def test_forfeit_like_score_is_not_guessed():
    """A forfeit-like score such as 1:0 does not obey arithmetic: it cannot
    tell you the map was played out. The match page covers those cases."""
    assert map_completed(1, 0, **MR12).completed is False


# ---------------------------------------------------------------- match point

def test_rounds_to_win_matches_the_same_thresholds():
    assert rounds_to_win(12, 5, **MR12) == 1
    assert rounds_to_win(11, 5, **MR12) == 2
    assert rounds_to_win(12, 12, **MR12) == 4     # up to 16
    assert rounds_to_win(15, 14, **MR12) == 1
    assert rounds_to_win(15, 15, **MR12) == 4     # up to 19
    assert rounds_to_win(13, 11, **MR12) == 0     # the map is already taken
