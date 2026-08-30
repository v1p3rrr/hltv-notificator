"""Multikills by players of a tracked team — an alert so a highlight can be clipped.

Computed from scoreboard frames, NOT from the Kill events in the log. The
reason is the same one that keeps the log unused everywhere: on every connect
the feed replays its backlog, and alerts would rain down for long-finished
rounds. In a frame every player carries the kills accumulated over the map, so
it is enough to remember them at the start of a round and watch the increment.
That also gives the alert at the fourth kill rather than at the end of the
round.

Errors lean the safe way: after a reconnect mid-round the baseline is taken
afresh, so a multikill may be MISSED but never invented.
"""

from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Optional, Set, Tuple

from ..sources.scorebot import PlayerLine

log = logging.getLogger(__name__)

ACE = 5
WARMUP = "warmup"


class MultikillTracker:
    """State for one match. Lives in the worker's memory.

    Deliberately not written to the database: this is one round's data, it is
    meaningless after a restart, and the protection against repeats already
    sits on the event key.
    """

    def __init__(self, threshold: int = 4):
        self.threshold = max(2, threshold)
        self._key: Optional[Tuple[str, int]] = None
        self._baseline: Dict[str, int] = {}
        self._alerted: Set[Tuple[str, int]] = set()

    @property
    def levels(self) -> List[int]:
        """The thresholds worth pinging on: the threshold itself and an ace.

        Nothing in between: two messages per round is already noise, whereas
        "it became an ace" is worth a look.
        """
        return sorted({self.threshold, ACE})

    def observe(self, map_name: str, round_number: int, round_state: str,
                players: Iterable[PlayerLine]) -> List[Tuple[PlayerLine, int]]:
        """Players who JUST took a multikill, and their kills in this round."""
        players = list(players)
        key = (map_name, round_number)

        if key != self._key:
            # A new round: fix the baseline and forget the previous alerts.
            self._key = key
            self._baseline = {p.steam_id: p.kills for p in players}
            self._alerted = set()
            return []

        # During warmup the kills come from deathmatch and have nothing to do
        # with the round.
        if round_state == WARMUP:
            self._baseline = {p.steam_id: p.kills for p in players}
            return []

        found: List[Tuple[PlayerLine, int]] = []
        for player in players:
            base = self._baseline.get(player.steam_id)
            if base is None:
                # The player appeared in the frame mid-round (a substitution, a
                # reconnect). Counting all their kills as this round's would be
                # wrong — take a baseline instead.
                self._baseline[player.steam_id] = player.kills
                continue
            in_round = player.kills - base
            if in_round < self.threshold:
                continue
            crossed = [level for level in self.levels
                       if in_round >= level and (player.steam_id, level) not in self._alerted]
            if not crossed:
                continue
            # Mark ALL crossed thresholds but report once: a player can jump
            # from three straight to an ace between two frames.
            for level in crossed:
                self._alerted.add((player.steam_id, level))
            found.append((player, in_round))
        return found
