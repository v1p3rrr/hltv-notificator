"""Deciding that a map is over, from the score alone.

Why this is separate from the match page. The maps section on HLTV updates
late — observed on match 2397091: the real score was 12:11 while the section
still showed 5:7, the result of the previous half. A "map over" notification
driven by the page therefore arrives not at the winning round but whenever
HLTV gets around to updating its statuses. The live feed knows the round score
immediately, so the map-over decision is made from the score and the page is
left as confirmation.

No hardcoded "13 rounds" anywhere. The format comes from the source itself:
`regulationHalfLength` and `overtimeHalfLength` in the scoreboard frame
(observed 12 and 3), and the same values in the `data-max-rounds-regulation`
and `data-max-rounds-overtime` attributes on the match page. That is why the
rule works equally for MR12, for the retired MR15 and for short formats.

How it is computed. A half lasts `regulation` rounds, so regulation time is
2*regulation rounds and a win there happens at `regulation + 1`, provided the
opponent did not reach `regulation`. If both reached `regulation` (12:12),
overtime starts: each overtime is two halves of `overtime` rounds, and winning
it means taking `overtime + 1` of them. Hence the thresholds 13, then 16, then
19 and so on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

DEFAULT_REGULATION = 12
DEFAULT_OVERTIME = 3


@dataclass(frozen=True)
class MapVerdict:
    completed: bool
    overtime_number: int = 0

    def __bool__(self) -> bool:
        return self.completed


def _overtime_number(low: int, regulation: int, overtime: int) -> int:
    """Which overtime is being played, judging by the trailing side's score.

    Counted from the lower score, not the higher one: the winner of an overtime
    is ahead of the opponent, so the higher score would give the wrong number.

    The trailing side can take at most `overtime` rounds per overtime, so its
    score is what tells you how many overtimes are behind us. The edge case is
    a tie at the overtime ceiling (15:15 under MR12/MR3): that overtime ended
    level, the next one is running, and the target is already 19, not 16.
    """
    if low < regulation:
        return 0
    return (low - regulation) // overtime + 1


def map_completed(score_a: Optional[int], score_b: Optional[int], *,
                  regulation: int = DEFAULT_REGULATION,
                  overtime: int = DEFAULT_OVERTIME) -> MapVerdict:
    """Whether the map is over at this score.

    Returns a verdict rather than a bare bool so the caller can tell a win in
    regulation from a win in overtime without recomputing the same thing.
    """
    if score_a is None or score_b is None:
        return MapVerdict(False)
    if regulation < 1 or overtime < 1:
        return MapVerdict(False)

    high, low = max(score_a, score_b), min(score_a, score_b)
    if low < 0 or high < 0:
        return MapVerdict(False)

    # Nobody has taken the deciding round of regulation yet.
    if high < regulation + 1:
        return MapVerdict(False)

    # Win in regulation: the opponent fell short of `regulation`.
    if low < regulation:
        return MapVerdict(True, 0)

    # Both reached `regulation` — overtimes are being played.
    played = _overtime_number(low, regulation, overtime)
    threshold = regulation + played * overtime + 1
    if high >= threshold and low <= threshold - 2:
        return MapVerdict(True, played)
    return MapVerdict(False, played)


def rounds_to_win(score_a: int, score_b: int, *,
                  regulation: int = DEFAULT_REGULATION,
                  overtime: int = DEFAULT_OVERTIME) -> int:
    """How many rounds the leader still needs to win the map.

    Needed for "match point"-style judgements: it is computed from the same
    thresholds as map completion itself, so the two cannot drift apart.
    """
    # The same guard as in map_completed: without it a format with overtime=0
    # (the field went missing in a frame and zero was substituted) would crash
    # the call with a division by zero.
    if regulation < 1 or overtime < 1:
        return 0
    high, low = max(score_a, score_b), min(score_a, score_b)
    if low < regulation:
        return max(0, regulation + 1 - high)
    played = _overtime_number(low, regulation, overtime)
    return max(0, regulation + played * overtime + 1 - high)
