"""Comebacks on a map: a big run that turned the map around, or nearly did.

The measure is NOT "N rounds in a row". Two real shapes of the same story:

    down  3:11, won 13:11 — ten taken without reply
    down  1:7,  won 13:9  — twelve taken, two given away

What they share is the swing in the score DIFFERENCE: −8 to +2 is ten, −6 to
+4 is ten as well. That is exactly "a lot won, few given away", and a streak
count would have missed the second one entirely.

So the map's difference is followed round by round, and the biggest rise and
the biggest fall are remembered. A rise is our comeback, a fall is theirs, and
either is worth saying: a comeback made, a comeback given away and a comeback
denied are the same fact told from different sides.

There is a floor on the deficit as well as on the swing. Without it a 13:1 win
would be announced as "a comeback from 0:1": the swing is twelve and there was
never a hole to climb out of. Half the swing is the smallest hole worth the
word, so it is derived rather than being a second setting.

The tracker holds one map and lives in memory, like MultikillTracker. It
survives feed reconnects, which are frequent; a restart in the middle of a map
loses the earlier rounds, and then a comeback comes out understated or missing
— but never invented.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Tuple

log = logging.getLogger(__name__)

Score = Tuple[int, int]

WON = "won"
STOPPED = "stopped"


@dataclass
class _Run:
    """The best swing one way: where it started, where it peaked, how big."""
    size: int = 0
    low: Score = (0, 0)
    peak: Score = (0, 0)


class ComebackTracker:
    """The score of ONE map, followed for swings.

    Scores are always given from the canonical team's point of view, the same
    as everywhere else — turning them around for a particular recipient is the
    renderer's job.
    """

    def __init__(self, threshold: int = 9):
        self.threshold = threshold
        # The extreme reached each way, and the best run measured from it.
        self._min_diff: Optional[int] = None
        self._min_at: Score = (0, 0)
        self._max_diff: Optional[int] = None
        self._max_at: Score = (0, 0)
        self._ours = _Run()
        self._theirs = _Run()

    @property
    def enabled(self) -> bool:
        return self.threshold > 0

    @property
    def min_deficit(self) -> int:
        """The smallest hole that counts as one. See the module docstring."""
        return max(2, self.threshold // 2)

    def observe(self, ours: int, theirs: int) -> None:
        if not self.enabled:
            return
        diff = ours - theirs
        score = (ours, theirs)

        if self._min_diff is None or diff < self._min_diff:
            self._min_diff = diff
            self._min_at = score
        elif diff - self._min_diff > self._ours.size:
            self._ours = _Run(diff - self._min_diff, self._min_at, score)

        if self._max_diff is None or diff > self._max_diff:
            self._max_diff = diff
            self._max_at = score
        elif self._max_diff - diff > self._theirs.size:
            self._theirs = _Run(self._max_diff - diff, self._max_at, score)

    # ------------------------------------------------------------------

    def verdict(self, ours: int, theirs: int, *,
                overtime: bool = False) -> Optional[dict]:
        """The map's comeback story, ready to go into the event payload.

        `ours`/`theirs` is the final score — it decides whether the run was
        completed or stopped short. Returns None when nothing on this map
        deserves the word.
        """
        if not self.enabled:
            return None

        we_came_back = self._qualifies(self._ours, trailing_is_ours=True)
        they_came_back = self._qualifies(self._theirs, trailing_is_ours=False)
        if not we_came_back and not they_came_back:
            return None

        if we_came_back and they_came_back:
            # Both had a run — the map's story is the bigger one. On a tie the
            # winner's, because that is the run that decided the map.
            if self._ours.size == self._theirs.size:
                ours_wins_it = ours > theirs
            else:
                ours_wins_it = self._ours.size > self._theirs.size
        else:
            ours_wins_it = we_came_back

        run = self._ours if ours_wins_it else self._theirs
        completed = (ours > theirs) if ours_wins_it else (theirs > ours)
        return {
            "comeback_from_team": run.low[0],
            "comeback_from_opponent": run.low[1],
            "comeback_to_team": run.peak[0],
            "comeback_to_opponent": run.peak[1],
            "comeback_swing": run.size,
            "comeback_result": WON if completed else STOPPED,
            "comeback_overtime": bool(overtime),
        }

    def _qualifies(self, run: _Run, *, trailing_is_ours: bool) -> bool:
        if run.size < self.threshold:
            return False
        behind, ahead = run.low if trailing_is_ours else run.low[::-1]
        return ahead - behind >= self.min_deficit
