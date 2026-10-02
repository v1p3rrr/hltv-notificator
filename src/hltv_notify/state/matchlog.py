"""The match transcript: every event of the feed's log, in readable English.

Not a decision and not a notification — a record, kept for a couple of days so
that "what actually happened in that round" can be answered after the fact.
Everything else in this service is built to be CERTAIN before it speaks; this
is built to be complete, and the difference shows in what it is allowed to do:
it prints what the source said, including the types no machine reads.

Two things make it work, and both come out of the stream itself rather than
out of the frames.

**The map and the round number are derived from the log.** `MatchStarted`
carries the map name, and `RoundEnd` carries the score AFTER the round — whose
sum IS that round's number, in regulation and overtime alike, because every
round adds exactly one to it. So the backlog the feed replays on connect lays
itself out across its own maps and rounds, and a service that joined a match
halfway still writes a transcript that starts at round one. Reading the map off
the current FRAME instead would stamp map one's rounds with map two's name.

**The position is an exact cursor.** The server's log is append-only and a
connect replays it from the beginning, so "how many entries we have taken" says
precisely where to resume. Which of the two kinds of packet is in hand — a
replay from the start or the next few entries — is told by the ids: a packet
whose first identified entry is one already seen is a replay. This is what the
anonymous types need; `RoundStart` is literally `{}` and has nothing else to be
told apart by.

What the source does NOT have, and what therefore is not here: a defuser. The
log has no `BombDefused` entry at all — a defuse is visible only as
`RoundEnd.winType == "Bomb_Defused"`, with no player on it. The round's line
says the bomb was defused and does not invent who did it. Same doctrine as the
clutch that cannot be sized.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from ..sources.scorebot import LiveFrame, LogEntry

log = logging.getLogger(__name__)

CT = "CT"
T = "TERRORIST"

# How the round ended, in words. Only the three the source's own name states
# outright are glossed: `Target_Bombed`, `Bomb_Defused` and `Target_Saved` say
# what happened to the bomb and nothing else could be meant. `CTs_Win` and
# `Terrorists_Win` do NOT say whether the round was taken by elimination or by
# the clock, so they are not glossed into either. The raw code is printed
# beside the gloss in every case: this is a diagnostic file, and the thing the
# source actually said is the thing worth keeping.
WIN_TYPES = {
    "Target_Bombed": "the bomb exploded",
    "Bomb_Defused": "the bomb was defused",
    "Target_Saved": "the bomb was never planted",
}


def ct_half(round_number: int, regulation: int, overtime: int) -> int:
    """Which half of the map a round belongs to, counting from zero.

    Rounds 1..regulation are the first half and regulation+1..2*regulation the
    second; after that every overtime half is `overtime` rounds long. An even
    half has the teams on the sides they started the map on, an odd one has
    them swapped — including the first overtime half, which puts them back on
    their original sides.
    """
    if regulation < 1:
        return 0
    if round_number <= regulation:
        return 0
    if round_number <= 2 * regulation:
        return 1
    if overtime < 1:
        return 1
    return 2 + (round_number - 2 * regulation - 1) // overtime


def team_on(side: str, round_number: int, frame: LiveFrame) -> Optional[int]:
    """Which team was on this side in this round.

    From `startingCt`/`startingT`, which are fixed for the whole map, never
    from `ctTeamId`, which is only true of the frame in hand. The match log
    writes rounds that are already over — the whole backlog of them on the
    first connect — and reading the current frame's sides would name the wrong
    team for every round of the other half.
    """
    if frame.starting_ct is None or frame.starting_t is None:
        return None
    swapped = ct_half(round_number, frame.regulation, frame.overtime) % 2 == 1
    on_ct = frame.starting_t if swapped else frame.starting_ct
    on_t = frame.starting_ct if swapped else frame.starting_t
    return on_ct if side == CT else on_t


@dataclass
class Transcript:
    """Where the log stream has got to, one map at a time.

    Everything here is read off the stream, which is why a replayed backlog
    places itself correctly: the map comes from `MatchStarted`, the round from
    the score `RoundEnd` carries, and `Restart` puts both back to the start of
    a map that is being set up again.
    """

    map_name: str = ""
    # The score sum after the last round that ENDED. The next round is one
    # past it, which is how an entry arriving mid-round is numbered without
    # waiting for that round to finish.
    decided: int = 0

    @property
    def round_number(self) -> int:
        return self.decided + 1

    def observe(self, entry: LogEntry) -> None:
        if entry.kind == "MatchStarted":
            self.map_name = str(entry.data.get("map") or "") or self.map_name
            self.decided = 0
        elif entry.kind == "Restart":
            # The server is setting the map up again — a knife round, a
            # technical pause. Whatever was decided before it was not this
            # map's.
            self.decided = 0
        elif entry.kind == "RoundEnd":
            total = _score(entry.data)
            if total is not None:
                # The round's own score is the authority on its number, not
                # our running count: a RoundEnd lost to a dropped connection
                # would otherwise shift every round after it by one.
                self.decided = total

    def round_of(self, entry: LogEntry) -> int:
        """The round an entry belongs to. A RoundEnd belongs to the round it
        ends, which its own score names; everything else to the one in play."""
        if entry.kind == "RoundEnd":
            total = _score(entry.data)
            if total is not None:
                return total
        return self.round_number


def _score(data: dict) -> Optional[int]:
    """Rounds decided, from a RoundEnd's score. None when it cannot be read."""
    try:
        return int(data["counterTerroristScore"]) + int(data["terroristScore"])
    except (KeyError, TypeError, ValueError):
        return None


def new_entries(entries: Sequence[LogEntry], *, position: int,
                first_id: Optional[int]) -> Tuple[List[LogEntry], int, Optional[int]]:
    """What of this packet has not been recorded, the position after it, and
    the id the match's stream starts at.

    A packet is one of two things. A connect replays the whole log from the
    beginning, so its first entry is the first entry of the MATCH — that is
    the test, and it is exact. Anything else is the next few entries and all of
    them are new.

    The obvious test — "its first id is one we have already seen" — is wrong,
    and wrong in a way that destroys the cursor. The feed sends an `Assist` as
    its own packet carrying the `killEventId` of the kill before it, so that
    packet's only id EQUALS the newest one seen; read as a replay, its
    `position` of one then overwrote a cursor of two thousand, and the next
    connect's backlog rewrote the entire match. Measured on the forze
    recording: 317 round-end lines for a match with 46 rounds.

    Position and not a set of fingerprints, because most types have nothing to
    fingerprint: `RoundStart` is literally `{}`, and two bomb plants by the
    same player on the same site with the same players alive are a real pair of
    events, not a duplicate.
    """
    if not entries:
        return [], position, first_id
    head = next((entry.event_id for entry in entries
                 if entry.event_id is not None), None)
    if first_id is None:
        # The first packet of this match: it IS the stream so far.
        return list(entries), len(entries), head
    if head is not None and head == first_id:
        # A replay from the beginning. Everything up to the cursor is a repeat;
        # the cursor itself never moves backwards, because a backlog taken
        # mid-poll can be shorter than what the live stream already delivered.
        return list(entries[position:]), max(position, len(entries)), first_id
    return list(entries), position + len(entries), first_id


# ---------------------------------------------------------------- rendering

def _side(value) -> str:
    text = str(value or "").upper()
    return "T" if text == T else ("CT" if text == CT else "?")


def _who(data: dict, *prefixes: str) -> str:
    for prefix in prefixes:
        for suffix in ("Nick", "Name"):
            value = data.get(f"{prefix}{suffix}")
            if value:
                return str(value)
    return "?"


def _kill_notes(data: dict) -> List[str]:
    """What was special about the kill, in the order it reads best."""
    notes = []
    weapon = str(data.get("weapon") or "")
    if weapon:
        notes.append(weapon)
    if data.get("headShot"):
        notes.append("headshot")
    if data.get("penetrated"):
        notes.append("wallbang")
    if data.get("throughSmoke"):
        notes.append("through smoke")
    if data.get("noScope"):
        notes.append("no-scope")
    if data.get("killerBlind"):
        notes.append("killer blind")
    if data.get("attackerInAir"):
        notes.append("mid-air")
    flasher = data.get("flasherNick") or data.get("flasherName")
    if flasher:
        notes.append(f"victim flashed by {flasher}")
    return notes


def render(entry: LogEntry, *, round_number: int, frame: Optional[LiveFrame],
           name_of) -> Optional[str]:
    """One entry as a line. None for an entry with nothing to say.

    `name_of` turns a team id into a name; it is passed in because the names
    live in the database and this module is about the stream.
    """
    data = entry.data
    kind = entry.kind

    if kind == "Kill":
        notes = _kill_notes(data)
        tail = f" — {', '.join(notes)}" if notes else ""
        return (f"{_who(data, 'killer')} ({_side(data.get('killerSide'))}) killed "
                f"{_who(data, 'victim')} ({_side(data.get('victimSide'))}){tail}")

    if kind == "Assist":
        # Its own line rather than folded into the kill: the feed sends an
        # assist AFTER the kill it belongs to once the packet is in time
        # order, so folding it would mean holding every kill back to see
        # whether one follows.
        return (f"{_who(data, 'assister')} ({_side(data.get('assisterSide'))}) "
                f"assisted the kill on {_who(data, 'victim')}")

    if kind == "BombPlanted":
        # No side on this one, and none is needed: only a terrorist plants.
        alive = ""
        if data.get("ctPlayers") is not None and data.get("tPlayers") is not None:
            alive = f" ({data['ctPlayers']} CT vs {data['tPlayers']} T alive)"
        site = data.get("bombSite")
        where = f" at {site}" if site else ""
        return f"{_who(data, 'player')} (T) planted the bomb{where}{alive}"

    if kind == "Suicide":
        weapon = str(data.get("weapon") or "")
        who = f"{_who(data, 'player')} ({_side(data.get('side'))})"
        if weapon in ("", "world"):
            return f"{who} died to the world"
        return f"{who} killed themselves with {weapon}"

    if kind == "RoundEnd":
        return _round_end(data, round_number, frame, name_of)

    if kind == "RoundStart":
        return f"--- round {round_number} ---"

    if kind == "MatchStarted":
        return f"=== map: {data.get('map') or '?'} ==="

    if kind == "Restart":
        return "=== the server restarted the map ==="

    if kind == "PlayerJoin":
        return f"{_who(data, 'player')} joined"

    if kind == "PlayerQuit":
        return f"{_who(data, 'player')} ({_side(data.get('playerSide'))}) left"

    # A type the source grew since this was written. Printed raw rather than
    # dropped: the whole point of the file is to show what arrived.
    return f"{kind}: {data}"


def _round_end(data: dict, round_number: int, frame: Optional[LiveFrame],
               name_of) -> str:
    winner = str(data.get("winner") or "")
    win_type = str(data.get("winType") or "")
    gloss = WIN_TYPES.get(win_type)
    reason = f"{gloss} [{win_type}]" if gloss else (win_type or "?")

    side = _side(winner)
    who = side
    scores = ""
    if frame is not None:
        team_id = team_on(winner.upper(), round_number, frame)
        if team_id is not None:
            name = name_of(team_id)
            if name:
                who = f"{name} ({side})"
        scores = _score_line(data, round_number, frame, name_of)
    if not scores:
        scores = (f"CT {data.get('counterTerroristScore')}:"
                  f"{data.get('terroristScore')} T")
    return f"round {round_number} ended — {who} won · {reason} · {scores}"


def _score_line(data: dict, round_number: int, frame: LiveFrame, name_of) -> str:
    """The score with the teams named, which needs knowing who was on CT."""
    ct_team = team_on(CT, round_number, frame)
    t_team = team_on(T, round_number, frame)
    if ct_team is None or t_team is None:
        return ""
    ct_name, t_name = name_of(ct_team), name_of(t_team)
    if not ct_name or not t_name:
        return ""
    return (f"{ct_name} {data.get('counterTerroristScore')}:"
            f"{data.get('terroristScore')} {t_name}")


# The lines that are their own heading rather than something that happened in
# a round. They are printed flush left and everything else is indented under
# them, which is the whole of the file's structure.
HEADINGS = frozenset({"MatchStarted", "Restart", "RoundStart"})


def to_text(match_id: int, title: str, rows) -> str:
    """The stored lines as the file a person reads.

    No timestamps, and that is deliberate: the feed stamps nothing, and a
    connect delivers the whole match at once, so the only time this service
    knows is when it happened to write the line. A transcript whose times were
    all the moment of one reconnect would be worse than one with none. What
    orients a reader of a CS match is the map and the round, and both are in
    the lines themselves.
    """
    out = [f"Match {match_id}" + (f" — {title}" if title else ""),
           f"{len(rows)} lines", ""]
    for row in rows:
        text = row["text"]
        out.append(text if row["kind"] in HEADINGS else f"    {text}")
    return "\n".join(out) + "\n"
