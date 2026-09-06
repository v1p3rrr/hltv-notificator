"""What a round produced that is worth a message: a multikill, a clutch, both.

Computed from scoreboard frames, NOT from the Kill events in the log. The
reason is the same one that keeps the log unused everywhere: on every connect
the feed replays its backlog, and alerts would rain down for long-finished
rounds. In a frame every player carries the kills AND the clutches accumulated
over the map, so it is enough to remember them when a round begins and watch
the increment.

**The round is resolved once**, when it ends — or the moment a player can no
longer add to it by dying. The multikill used to be reported the instant the
Nth kill landed, which cannot work any more: a clutch is only a clutch when the
round is WON, so it is not known until the round is over, and one message
naming both facts beats two in a row. The cost was measured across both
recordings — median 0 s, worst case 32 s, because the last kill of a multikill
is usually the kill that ends the round. What it buys, besides the combined
message, is the end of the old double ping: a player who reached the bar and
then aced produced "4k round" and then "ACE", and now produces one message
that says ACE.

**A round is credited only with what was seen DURING it.** Not with the
difference between its baseline and whatever frame happens to arrive next: the
feed skips rounds. The forze recording jumps straight from round 2 to round 8,
and a naive difference across that gap reported a ten-kill round for four
players at once. So the peak is tracked frame by frame while the round is the
current one, and a frame belonging to another round cannot contribute to it.

Errors lean the safe way: after a reconnect mid-round the baselines are taken
afresh, so a highlight may be MISSED but never invented.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple

from ..sources.scorebot import PlayerLine

log = logging.getLogger(__name__)

ACE = 5
WARMUP = "warmup"
STARTED = "started"
ENDED = "ended"


@dataclass(frozen=True)
class Highlight:
    """One player's round, once it is over.

    `clutch_against` is 0 when there was no clutch — see `_clutch_for` for why
    a clutch we could not size is reported as none at all.
    """

    player: PlayerLine
    kills: int
    clutch_against: int = 0

    @property
    def is_ace(self) -> bool:
        return self.kills >= ACE


class RoundTracker:
    """State for one team in one match. Lives in the worker's memory.

    Deliberately not written to the database: this is one round's data, it is
    meaningless after a restart, and the protection against repeats already
    sits on the event key.
    """

    def __init__(self, multikill: int = 4, clutch: int = 3):
        # Zero means the bar is off and stays zero. Only a bar that is ON has a
        # floor, and the multikill's is 2 because one kill is not a multi —
        # `Setting.smallest_on` says the same thing to the person setting it.
        self.multikill = max(2, multikill) if multikill > 0 else 0
        self.clutch = max(1, clutch) if clutch > 0 else 0
        self._key: Optional[Tuple[str, int]] = None
        # Everything below describes the round named by `_key` and nothing else.
        self._kills: Dict[str, int] = {}        # at the round's start
        self._clutches: Dict[str, int] = {}     # at the round's start
        self._peak: Dict[str, int] = {}         # the most seen DURING the round
        self._won: Set[str] = set()             # credited a clutch during it
        self._seen: Dict[str, PlayerLine] = {}  # the latest line, for the nick
        # The largest number of opponents a player faced while being the last of
        # their team alive. Written only during `started` — see observe.
        self._situation: Dict[str, int] = {}
        # Who has already been reported for the round in hand. Per PLAYER and
        # not per round, because a player who dies is reported the moment he
        # does — see `_settled`. Every flush consults it, so nobody is reported
        # twice for one round.
        self._reported: Set[str] = set()

    # ------------------------------------------------------------------

    def observe(self, map_name: str, round_number: int, round_state: str,
                players: Iterable[PlayerLine],
                opponents: Iterable[PlayerLine] = ()) -> List[Highlight]:
        """Feed one frame in; get the round's highlights when it is over.

        Returns an empty list on almost every call — a round is reported once,
        and a frame arrives several times a second.
        """
        players = list(players)
        opponents = list(opponents)
        key = (map_name, round_number)

        if key != self._key:
            # A round whose end we never saw — a missed `ended`, a reconnect
            # across the boundary — is still worth reporting, and this is the
            # last moment at which it can be. Safe even when rounds were
            # skipped, because it reports what was seen during that round and
            # this frame belongs to another one.
            leaving = self._flush(self._seen)
            self._start(key, players)
            return leaving

        # During warmup the kills come from deathmatch and have nothing to do
        # with the round.
        if round_state == WARMUP:
            self._start(key, players)
            return []

        self._track(players)

        if round_state == STARTED:
            # Only while the round is actually being played. At half time the
            # CT and TERRORIST arrays swap under us during `ended`, and the
            # alive counts pass through (1, 1) on the way — a forged 1v1 that
            # would raise N for whoever happened to be in that frame.
            self._watch(players, opponents)
            # A player whose round is already decided does not have to wait for
            # the rest of it: the whole point of the alert is to be on a
            # broadcast while the moment is still clippable.
            return self._flush([p.steam_id for p in players if self._settled(p)])

        if round_state == ENDED:
            return self._flush(self._seen)
        return []

    # ------------------------------------------------------------------

    def _start(self, key: Tuple[str, int], players: List[PlayerLine]) -> None:
        self._key = key
        self._kills = {p.steam_id: p.kills for p in players}
        self._clutches = {p.steam_id: p.clutches for p in players}
        self._peak = dict(self._kills)
        self._seen = {p.steam_id: p for p in players}
        self._won = set()
        self._situation = {}
        self._reported = set()

    def _track(self, players: List[PlayerLine]) -> None:
        """Fold this frame into what the current round has produced so far."""
        for player in players:
            if player.steam_id not in self._kills:
                # Somebody who turned up mid-round: a substitution, or a
                # reconnect putting them back into the frame. Counting the
                # whole map's work as this round's would report a 20k round;
                # counting from here reports only what we actually watched.
                self._kills[player.steam_id] = player.kills
                self._clutches[player.steam_id] = player.clutches
            self._seen[player.steam_id] = player
            self._peak[player.steam_id] = max(
                self._peak.get(player.steam_id, player.kills), player.kills)
            if player.clutches > self._clutches[player.steam_id]:
                self._won.add(player.steam_id)

    def _watch(self, players: List[PlayerLine],
               opponents: List[PlayerLine]) -> None:
        """Record a 1vN while it is standing."""
        alive = [p for p in players if p.alive]
        if len(alive) != 1:
            return
        facing = sum(1 for p in opponents if p.alive)
        if facing < 1:
            return
        last = alive[0]
        # The maximum, not the first reading: the count only falls as the
        # clutch is played out, and taking the largest survives a frame missed
        # at the moment the situation opened.
        if facing > self._situation.get(last.steam_id, 0):
            self._situation[last.steam_id] = facing

    def _settled(self, player: PlayerLine) -> bool:
        """Nothing more can happen to this player's round.

        A dead player takes no more kills, so his multikill is final and there
        is no reason to hold it back — a bar reached early in a long round
        would otherwise sit unsent for the whole of it.

        The exception is a player who was at some point the last of his team
        alive. A clutch can still be CREDITED to him after he dies: the bomb he
        planted goes off, his team takes the round, and HLTV counts the 1vN he
        was in. Waiting costs him nothing — he is dead either way — and sending
        early would report a bare multikill for a round that turns out to be a
        clutch, which is the one thing this rewrite exists to stop.
        """
        return not player.alive and player.steam_id not in self._situation

    def _flush(self, who: Iterable[str]) -> List[Highlight]:
        """Report these players' rounds, each at most once."""
        if self._key is None:
            return []

        found: List[Highlight] = []
        for steam_id in list(who):
            if steam_id in self._reported:
                continue
            player = self._seen.get(steam_id)
            base = self._kills.get(steam_id)
            if player is None or base is None:
                continue
            kills = self._peak.get(steam_id, base) - base
            against = self._clutch_for(steam_id, player)
            if not self._worth_reporting(kills, against):
                continue
            self._reported.add(steam_id)
            found.append(Highlight(player=player, kills=kills,
                                   clutch_against=against))
        return found

    def _clutch_for(self, steam_id: str, player: PlayerLine) -> int:
        """How many this player beat alone, or 0 for "no clutch here".

        Whether a clutch was WON is HLTV's verdict (`oneOnXWins`), not ours: it
        also covers the round taken on the bomb or the clock, which no reading
        of the alive counts can tell from a round that simply ran out. How many
        it was against is ours, because the feed never says.
        """
        if steam_id not in self._won:
            return 0
        against = self._situation.get(steam_id, 0)
        if against < 1:
            # HLTV says a round was won alone and we never saw the standoff —
            # frames lost across a reconnect, most likely. The bar is expressed
            # entirely in N, so there is nothing to compare it against and
            # nothing honest to print. Dropping is right where guessing is
            # wrong: an invented "1v1" understates a 1v4, and a made-up number
            # is read as a fact.
            log.debug("%s won a round alone but the standoff was never seen — "
                      "no clutch reported", player.nick)
            return 0
        return against

    def _worth_reporting(self, kills: int, against: int) -> bool:
        """Either bar clears it. Both are the LOWEST in use.

        An event is born once for everybody and narrowed down per reader in the
        queue (`outbox._wants`), so this is deliberately the more generous of
        the two questions: a round nobody's bar clears is never built, and a
        round one person's bar clears is built for everybody and then withheld.
        """
        if self.clutch > 0 and against >= self.clutch:
            return True
        return self.multikill > 0 and kills >= self.multikill
