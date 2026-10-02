"""The bingo card: nine things worth noticing over a match, counted passively.

The card is the team's own — "win two pistol rounds", "four kills through
smoke", "a knife kill" — and it is read as a MATCH, not as a map: that is what
"overtime on ANY map" and "win the match" mean, and it is why the counters
aggregate across the series. The per-map summary is an interim report on the
same numbers.

Where the numbers come from, and why that is not obvious:

* **Four of the squares need the feed's `log`**, which this service had never
  read. A scoreboard frame says who has how many kills; it cannot say that one
  of them went through smoke, through a wall, out of a grenade or with a
  knife. Only `Kill` entries carry that, and they carry it reliably — measured
  across both recordings' 731 unique kills, `throughSmoke` on 40,
  `penetrated` on 28, `hegrenade` on 4 and a `knife_*` on 16 — the same
  figures as R4's table, which is the source of truth for them.
* **The log replays its whole backlog on every connect**, which is exactly why
  it was left alone. The way in is `KillEvent.event_id`: unique per kill and
  strictly increasing in arrival order over all 731 recorded kills, so a
  single high-water mark per match deduplicates it and survives a restart.
* **The round-based squares come from the frame's own history**
  (`ctMatchHistory`), not from the log's `RoundEnd` — same backlog problem,
  and the history is a statement about the map rather than an event.
* **The ace reuses `RoundTracker`** with its bar set to five. It already
  solves the hard part (a round is credited only with what was seen inside it,
  because the feed skips rounds) and solving it a second time here would be
  two places to get it wrong.

Two things are counted by the service and NOT by this module, because they are
already events with their own machinery: taking the match (E7, конец матча)
and an overtime starting (E13). The bingo reads the same facts off the score
rather than off those events, deliberately — E13 is gated on a per-subscriber
setting that is off by default, so a square that waited for it would never be
filled in for most readers.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..sources.scorebot import KillEvent, LiveFrame, RoundOutcome

log = logging.getLogger(__name__)

# How the counts of several maps become the match's number.
SUM = "sum"   # kills, pistol rounds: they add up over the series
MAX = "max"   # a streak, a flag: the best map's answer is the match's answer


@dataclass(frozen=True)
class Square:
    """One cell of the card.

    `target` is the number printed on the picture, and it is deliberately not
    configurable: the card is a fixed thing people tick off while watching, and
    a per-person target would make two readers disagree about whether a square
    is closed.
    """

    key: str
    label: str
    target: int
    aggregate: str = SUM
    unit: str = ""
    unit_one: str = ""
    # What one occurrence reads like in a live message, with the player's nick
    # substituted. Empty where a square has no per-occurrence form of its own.
    moment: str = ""

    def describe(self, value: int) -> str:
        unit = self.unit_one if value == 1 and self.unit_one else self.unit
        return f"{value} {unit}".strip() if unit else str(value)

    def closed(self, value: int) -> bool:
        return value >= self.target


# In the order of the picture, read left to right, top to bottom.
SQUARES: Tuple[Square, ...] = (
    Square("pistol", "Win 2 pistol rounds", 2, SUM,
           unit="pistol rounds", unit_one="pistol round",
           moment="took the pistol round"),
    Square("smoke", "4 kills through smoke", 4, SUM,
           unit="kills", unit_one="kill", moment="killed through smoke"),
    Square("he", "2 kills with a HE grenade", 2, SUM,
           unit="kills", unit_one="kill", moment="killed with a grenade"),
    Square("streak", "Win 4 rounds in a row", 4, MAX,
           unit="rounds in a row", unit_one="round",
           moment="made it 4 rounds in a row"),
    # No `moment`: taking the match is ticked from E7's payload
    # (`record_win`) and never produces an Occurrence, so a per-occurrence
    # text here is a field nothing reads — the trap this project has already
    # been caught by twice. E7 itself is the message about the win.
    Square("win", "Win the match", 1, MAX),
    Square("wallbang", "3 kills through a wall", 3, SUM,
           unit="kills", unit_one="kill", moment="killed through a wall"),
    Square("knife", "A knife kill", 1, SUM,
           unit="kills", unit_one="kill", moment="killed with a knife"),
    Square("ace", "An ace", 1, SUM, unit="aces", unit_one="ace",
           moment="aced the round"),
    Square("overtime", "Overtime on any map", 1, MAX, moment="went to overtime"),
)

BY_KEY: Dict[str, Square] = {square.key: square for square in SQUARES}
KEYS: Tuple[str, ...] = tuple(square.key for square in SQUARES)

# The bar that makes a round an ace. Five, because a team has five players and
# there is nothing above it — `RoundTracker` is handed this and nothing else.
ACE_KILLS = 5

# A kill is only a bingo kill when the map is being PLAYED. Measured: all 16
# knife kills in the forze recording are from the knife rounds before the two
# maps, and a knife round scores on the board like any other — it is not
# `warmup` by the time it is played. So this guard is necessary and it is not
# sufficient; `KnifeHold` and the score-reset watch below are the rest of it.
#
# How long an exact return of the pre-reset score still reads as a server that
# crashed and was restored rather than as a map starting over. A restore is a
# matter of seconds; what it must not be confused with is the real map
# reaching the same score again, and the first round after a restart cannot be
# decided in under a minute — it carries the freeze time and the restart
# ceremony with it.
RESET_RESTORE_SECONDS = 60.0
# And how long a 0:0 has to stand before the reset is believed. Anything
# counted before it is then dropped, and anything already SENT about it is
# struck through. Deliberately longer than the window above, so that the two
# answers can never both be available for one candidate.
RESET_CONFIRM_SECONDS = 180.0


def kill_squares(kill: KillEvent) -> Tuple[str, ...]:
    """Which squares one kill feeds. More than one is normal — a grenade kill
    through smoke is both, and the card counts it in both."""
    found: List[str] = []
    if kill.through_smoke:
        found.append("smoke")
    if kill.penetrated:
        found.append("wallbang")
    if kill.weapon == "hegrenade":
        found.append("he")
    if kill.is_knife:
        found.append("knife")
    return tuple(found)


def pistol_rounds(regulation: int, overtime: int, upto: int) -> Tuple[int, ...]:
    """The ordinals of the pistol rounds up to and including `upto`.

    The first round of every HALF, which is more than the two obvious ones: a
    regulation half starts at 1 and at `regulation + 1`, and every overtime
    half starts `overtime` rounds after the one before it. Under MR12/MR3 that
    is 1, 13, 25, 28, 31, … — and a match that goes to a second overtime has
    four of them on one map.
    """
    if regulation < 1 or upto < 1:
        return ()
    found = [1]
    if regulation + 1 <= upto:
        found.append(regulation + 1)
    if overtime >= 1:
        ordinal = 2 * regulation + 1
        while ordinal <= upto:
            found.append(ordinal)
            ordinal += overtime
    return tuple(number for number in found if number <= upto)


def pistols_won(history: Sequence[RoundOutcome], regulation: int,
                overtime: int) -> int:
    """How many pistol rounds this team has taken on the map."""
    if not history:
        return 0
    won = {item.ordinal for item in history if item.won}
    last = max(item.ordinal for item in history)
    return sum(1 for ordinal in pistol_rounds(regulation, overtime, last)
               if ordinal in won)


def longest_streak(history: Sequence[RoundOutcome]) -> int:
    """The longest run of rounds won back to back on the map.

    The run is not stopped by the break: the halves are one ordered list and
    "four in a row" does not care where the sides swapped. It IS stopped by a
    gap in the ordinals — a round the history does not carry is a round we
    cannot claim was won, and joining across it would invent a streak.
    """
    best = run = 0
    previous: Optional[int] = None
    for item in history:
        if not item.won or (previous is not None and item.ordinal != previous + 1):
            run = 0
        if item.won:
            run += 1
            best = max(best, run)
        previous = item.ordinal
    return best


def in_overtime(ours: int, theirs: int, regulation: int) -> bool:
    """Is this map in an overtime.

    Both teams at `regulation`, never the sum: at 13:11 the sum is also 24 and
    the map is over in regulation, which is the opposite of what this asks.
    """
    return regulation >= 1 and ours >= regulation and theirs >= regulation


def totals(rows: Iterable) -> Dict[str, int]:
    """Several maps' counts into the match's, by each square's own rule.

    This is the one place the two aggregates are applied, and it has to be:
    SUM in SQL would quietly turn "four rounds in a row" into "eight" the
    moment a team managed four on each of two maps.
    """
    found: Dict[str, int] = {}
    for row in rows:
        square = BY_KEY.get(row["square"])
        if square is None:
            # A square that has been removed from the card since the row was
            # written. Dropped rather than shown under its raw key.
            continue
        value = int(row["value"] or 0)
        if square.aggregate == MAX:
            found[square.key] = max(found.get(square.key, 0), value)
        else:
            found[square.key] = found.get(square.key, 0) + value
    return found


def card(counts: Dict[str, int]) -> List[dict]:
    """The nine squares as a message renders them, in the picture's order.

    Every square every time, closed or not: the card is a card, and a summary
    that printed only what happened would stop being one you tick off.
    """
    return [{"key": square.key, "label": square.label,
             "value": counts.get(square.key, 0), "target": square.target,
             "done": square.closed(counts.get(square.key, 0)),
             "text": square.describe(counts.get(square.key, 0))}
            for square in SQUARES]


def closed_count(counts: Dict[str, int]) -> int:
    return sum(1 for square in SQUARES if square.closed(counts.get(square.key, 0)))


# The match as a whole rather than any of its maps. Taking the match is the
# one square no map can answer, and giving it a map's number would put it into
# that map's summary, where it would be a claim about a series that was still
# running.
MATCH_WIDE = 0


def enabled(storage, config) -> bool:
    """Is anybody keeping the card at all.

    The lowest bar in use, like every other per-reader thing here, and in ONE
    place: with nobody keeping it the log is not parsed, no kill is classified
    and no counter is written.
    """
    from .. import settings             # here, to keep this module importable

    return storage.threshold_in_use(
        "bingo", settings.default_for(config, "bingo")) > 0


def record_win(storage, config, match_id: int, finished: dict) -> None:
    """Tick "win the match" for whoever took it, from E7's payload.

    Shared by both machines, exactly like `summary_events` and for the same
    reason: either of them can be the one that reaches the end of the match
    first. The feed does when it knows the format; the page does when the feed
    never ran for the last map, or when the format was never reported and
    `LiveMachine._event_e7` therefore stayed silent. Written in only one of
    them, the winner's card went out from the other with "Win the match" still
    open — a card that contradicts the E7 sitting right above it.

    Read off the series score rather than counted, and written for the WINNER
    only: the card is each team's own, and a subscriber following the losing
    side has a square that stays open.
    """
    if not enabled(storage, config):
        return
    ours = int(finished.get("series_team") or 0)
    theirs = int(finished.get("series_opponent") or 0)
    if ours == theirs:
        return
    winner = finished.get("team_id") if ours > theirs else finished.get("opponent_id")
    if not winner:
        return
    tracked = storage.match_team_ids(match_id)
    if tracked and winner not in tracked:
        # The match was taken by a team nobody follows. Nothing to tick.
        return
    storage.raise_bingo(match_id, MATCH_WIDE, int(winner), "win", 1)


def summary_events(storage, config, match_id: int, *, context_for,
                   map_number: Optional[int], map_name: str) -> List:
    """The card after a map (E17) and after the match (E18).

    Shared by both machines on purpose. The end of a map and the end of a
    match are each announced by whichever of them gets there first — the feed
    knows at the winning round, the page knows when it notices the status — so
    a summary written into only one of them would simply not arrive for a
    match whose format the page never reported. The key is the same from
    either side, so when both do get there the unique index swallows the
    second in silence, exactly as it does for E6 and E7 themselves.

    The key also names the TEAM, and that is not decoration either. One card
    is built per tracked team, and the journal key is `<chat>|<key>`: a
    subscriber following BOTH teams of a match would otherwise have the second
    card swallowed as a duplicate of the first and be shown one side of a
    match it follows from both. Same reasoning as E9's key carrying the
    player's steam id and E16's carrying the team.

    Silent when the feed never ran for this match: with no watermark nothing
    was ever counted, and a card of nine zeroes is not a summary of a match,
    it is a claim about one nobody watched.
    """
    from ..models import Event          # here, to keep this module importable,
                                        # by the sources, which models is not

    if not enabled(storage, config):
        return []
    if storage.bingo_watermark(match_id) is None:
        return []
    canonical = storage.canonical_team(match_id) or config.team_id
    tracked = storage.match_team_ids(match_id) or [canonical]
    finished = map_number is None
    events = []
    for team_id in tracked:
        total = totals(storage.bingo_rows(match_id, team_id))
        payload = {"squares": card(total), "closed": closed_count(total)}
        if finished:
            key = f"E18:{match_id}:{team_id}:bingo"
        else:
            on_map = totals(storage.bingo_rows(match_id, team_id,
                                               map_number=map_number))
            payload.update({
                "map_squares": {name: on_map.get(name, 0) for name in KEYS},
                "map_number": map_number,
                "map_name": map_name,
            })
            key = f"E17:{match_id}:map:{map_number}:{team_id}:bingo"
        events.append(Event(
            type="E18" if finished else "E17",
            idempotency_key=key,
            match_id=match_id,
            payload={**context_for(team_id), **payload}))
    return events


@dataclass(frozen=True)
class Occurrence:
    """One thing that happened, feeding one square for one team.

    `anchor` is what makes the idempotency key unique, and each kind supplies
    its own: a kill has its `eventId`, a pistol round its ordinal, a streak its
    length. Nothing here is keyed on the count, which a reconnect can retake.

    `absolute` says what `amount` MEANS, and it is the difference between a
    thing that happened and a number that was read. A kill is an event: it is
    added, and nothing else can tell you about it twice. A pistol round is
    recomputed from the frame's own history on every frame, so `amount` is
    what the square stands at FOR THIS MAP and the write must be a MAX rather
    than an addition. Added, it is counted again in full every time the
    in-memory high-water mark is lost — a worker re-created by `reconcile`, a
    403 cooldown, a restart — and a team that took two pistol rounds is
    reported as having taken four.
    """

    square: str
    team_id: int
    anchor: str
    amount: int = 1
    nick: str = ""
    round_number: Optional[int] = None
    weapon: str = ""
    absolute: bool = False


@dataclass
class _MapState:
    """Everything the tracker remembers about one map.

    In memory on purpose, like every other tracker here: it is the current
    map's working state, a restart in the middle of a map can understate it,
    and the counts it feeds are in the database where a restart cannot touch
    them.
    """

    # The round the kills arriving now belong to, and what that round has
    # produced so far. See KnifeHold below.
    round_number: Optional[int] = None
    knife_held: List[Occurrence] = field(default_factory=list)
    other_kill: bool = False
    # The highest reported value per team, so a square that is recomputed from
    # the history on every frame only speaks when it has moved.
    reported: Dict[Tuple[int, str], int] = field(default_factory=dict)
    # The score reset watch.
    high_sum: int = 0
    last_score: Tuple[int, int] = (0, 0)
    pending_since: Optional[float] = None
    pending_score: Tuple[int, int] = (0, 0)


class BingoTracker:
    """One match's bingo bookkeeping, held in the live worker's memory.

    It does not touch the database: it says what happened, and the machine
    writes the counts and builds the events. That split is what keeps the two
    awkward rules — the knife round and the score reset — in one readable
    place instead of spread through `LiveMachine.apply`.
    """

    def __init__(self, clock=time.monotonic):
        # Injectable so the reset windows can be tested without waiting three
        # minutes. monotonic and not wall time: this measures a duration, and
        # the host's clock is allowed to jump.
        self._clock = clock
        self._maps: Dict[str, _MapState] = {}
        # HLTV player id -> team id, learned from every frame and kept for the
        # whole match. This is how a kill is attributed, and it is the only
        # way that works: the log's own `killerSide` is the side at the time of
        # the kill, and the backlog replays kills from before the break.
        self._teams: Dict[int, int] = {}

    # ------------------------------------------------------------------

    def _state(self, map_name: str) -> _MapState:
        if map_name not in self._maps:
            self._maps[map_name] = _MapState()
        return self._maps[map_name]

    def team_of(self, player_id: Optional[int]) -> Optional[int]:
        if player_id is None:
            return None
        return self._teams.get(player_id)

    def learn_roster(self, frame: LiveFrame) -> None:
        """Who plays for whom, from the frame's two arrays.

        Both arrays every time: the sides swap at the break, and the mapping
        this builds is to the TEAM, which does not.
        """
        for players, team_id in ((frame.ct_players, frame.ct_team_id),
                                 (frame.t_players, frame.t_team_id)):
            if not team_id:
                continue
            for player in players:
                if player.player_id is not None:
                    self._teams[player.player_id] = int(team_id)

    # ---------- the knife round ----------

    def _open_round(self, state: _MapState, round_number: int) -> List[Occurrence]:
        """Close the round that was open and start the new one.

        A knife kill is held until its round is over, because a knife kill in a
        round that produced NOTHING ELSE is not a highlight, it is the knife
        round — or a warmup nobody has called off yet. A knife kill in a round
        with real weapons in it is the thing on the card. The feed cannot tell
        us which it was at the moment of the kill, so the question is answered
        when the round is.
        """
        released: List[Occurrence] = []
        if state.knife_held:
            if state.other_kill:
                released = list(state.knife_held)
            else:
                log.info("round %s produced knife kills and nothing else — "
                         "a knife round, not counted", state.round_number)
        state.knife_held = []
        state.other_kill = False
        state.round_number = round_number
        return released

    # ---------- the score reset ----------

    def _watch_reset(self, state: _MapState, map_name: str,
                     ours: int, theirs: int) -> bool:
        """True once a reset of the map's score is believed.

        The owner's description of what this is for, measured on live HLTV: a
        map opens in warmup, the knife round is PLAYED and scores 1:0, a couple
        of idle rounds follow, and only then is the score put back to 0:0 for
        the real map, after which it never resets again. Everything before that
        last 0:0 belongs to no map and must go.

        What it must not do is fire on a server that crashed, showed 0:0 and
        was restored to the score it had. The two look identical in the frame
        that shows 0:0; what tells them apart is what comes NEXT, so a
        candidate waits. The old score coming back exactly cancels it; nothing
        coming back for RESET_CONFIRM_SECONDS confirms it. The real map cannot
        counterfeit the cancel in that time — its first round carries a freeze
        period and a restart ceremony and cannot be decided inside a minute.
        """
        now = self._clock()
        total = ours + theirs
        confirmed = False

        if state.pending_since is not None:
            if (ours, theirs) == state.pending_score and \
                    now - state.pending_since <= RESET_RESTORE_SECONDS:
                log.info("%s: the score came back to %d:%d — the 0:0 was a "
                         "restored server, not a reset", map_name,
                         *state.pending_score)
                state.pending_since = None
            elif now - state.pending_since >= RESET_CONFIRM_SECONDS:
                log.warning("%s: the score reset from %d:%d and stayed reset — "
                            "dropping everything counted before it",
                            map_name, *state.pending_score)
                state.pending_since = None
                state.high_sum = total
                confirmed = True
        elif total == 0 and state.high_sum > 0:
            log.info("%s: the score dropped to 0:0 from %d:%d — waiting to see "
                     "whether it is a reset or a restored server", map_name,
                     *state.last_score)
            state.pending_since = now
            state.pending_score = state.last_score

        state.high_sum = max(state.high_sum, total)
        if total:
            # Remembered only while there is something to remember: the score
            # to compare a restore against is the one BEFORE the zero, so a
            # 0:0 frame must not overwrite it.
            state.last_score = (ours, theirs)
        return confirmed

    def reset_pending_since(self, map_name: str) -> Optional[float]:
        """When the candidate reset of this map's score was first seen.

        The machine needs it to decide which messages a confirmed reset has to
        strike through: everything sent about this map before that moment.
        """
        state = self._maps.get(map_name)
        return state.pending_since if state else None

    # ---------- the frame ----------

    def observe_frame(self, map_name: str, frame: LiveFrame, team_ids: Iterable[int],
                      ours: int, theirs: int, in_play: bool) -> Tuple[List[Occurrence], bool]:
        """One frame in; the squares it moved, and whether the score reset.

        The history-driven squares are recomputed from the frame every time
        rather than accumulated, so they cannot drift: the feed skips rounds,
        and a count built by adding one per observed win would miss every round
        it never saw. `reported` is only there to keep the frame from saying
        the same thing four times a second.
        """
        self.learn_roster(frame)
        state = self._state(map_name)
        reset = self._watch_reset(state, map_name, ours, theirs)
        if reset:
            state.reported.clear()
            state.knife_held = []
            state.other_kill = False

        found: List[Occurrence] = []
        if frame.current_round and frame.current_round != state.round_number:
            found.extend(self._open_round(state, frame.current_round))

        if not in_play:
            return found, reset

        overtime = in_overtime(ours, theirs, frame.regulation)
        for team_id in team_ids:
            history = frame.our_history(team_id)
            found.extend(self._raise(
                state, team_id, "pistol",
                pistols_won(history, frame.regulation, frame.overtime)))
            found.extend(self._raise(
                state, team_id, "streak", longest_streak(history)))
            if overtime:
                # Read off the score and not off the history: the history says
                # who won which round, never which round belongs to an
                # overtime. Raised for every tracked team because an overtime
                # is the map's property — both of them are in it.
                found.extend(self._raise(state, team_id, "overtime", 1))
        return found, reset

    def _raise(self, state: _MapState, team_id: int, square: str,
               value: int) -> List[Occurrence]:
        """Report a square recomputed from the frame, once per value it reaches.

        The two aggregates part company here, and they have to. A SUM square
        gets one occurrence per STEP — not one per frame and not one per jump,
        because the feed skips rounds and a pistol count can go from one to two
        inside a single frame, and the second pistol round is its own moment
        whether or not we saw the frame in between. A MAX square gets ONE
        occurrence carrying the new value: "four rounds in a row" is a single
        thing that happened, and stepping it would announce the first, second
        and third round of the run as if each were news.

        What does NOT part company is the write: everything that comes out of
        here is `absolute`, because everything that comes out of here was READ
        off the frame rather than witnessed. `state.reported` lives in memory
        and the counters live in the database, so a step the tracker has
        forgotten would otherwise be added a second time — see `Occurrence`.
        The step number IS the per-map value at that step, which is what makes
        the MAX write exact rather than merely safe.
        """
        if value <= 0:
            return []
        key = (team_id, square)
        seen = state.reported.get(key, 0)
        if value <= seen:
            return []
        state.reported[key] = value
        if BY_KEY[square].aggregate == MAX:
            return [Occurrence(square=square, team_id=team_id,
                               anchor=f"{square}:{value}", amount=value,
                               absolute=True)]
        return [Occurrence(square=square, team_id=team_id,
                           anchor=f"{square}:{step}", amount=step,
                           absolute=True)
                for step in range(seen + 1, value + 1)]

    def close_map(self, map_name: str) -> List[Occurrence]:
        """The map is over: let go of anything the last round was holding.

        Without this a knife kill in the final round would be dropped, because
        the round it is waiting on never turns over. It is released on the same
        terms as any other — only if that round had a real weapon in it too.
        """
        state = self._maps.get(map_name)
        if state is None:
            return []
        return self._open_round(state, state.round_number or 0)

    # ---------- the log ----------

    def observe_kills(self, map_name: str, kills: Iterable[KillEvent],
                      wanted_teams: Sequence[int],
                      in_play: bool) -> List[Occurrence]:
        """Kills in; the squares they feed.

        Kills of ANY player are looked at, because whether a round was a knife
        round is a question about the round and not about our team: a knife
        round is one where nobody at all used anything else.
        """
        if not in_play:
            # Nothing a warmup produces may be read, and that includes the
            # "there was a real weapon in this round" mark. The warmup's
            # deathmatch kills carry the round number the knife round is about
            # to use, so counting them would answer the knife round's question
            # for it — with the wrong answer. The whole batch, because `in_play`
            # is the frame's and does not change inside one.
            return []
        state = self._state(map_name)
        found: List[Occurrence] = []
        for kill in kills:
            squares = kill_squares(kill)
            if not kill.is_knife:
                # The round had a real weapon in it, so it was not the knife
                # round — recorded whoever fired it.
                state.other_kill = True
            team_id = self.team_of(kill.killer_id)
            if team_id is None or team_id not in wanted_teams:
                continue
            for square in squares:
                occurrence = Occurrence(
                    square=square, team_id=team_id,
                    anchor=f"kill:{kill.event_id}", nick=kill.killer_nick,
                    round_number=state.round_number, weapon=kill.weapon)
                if square == "knife":
                    # Held until the round is over — see `_open_round`.
                    state.knife_held.append(occurrence)
                else:
                    found.append(occurrence)
        return found
