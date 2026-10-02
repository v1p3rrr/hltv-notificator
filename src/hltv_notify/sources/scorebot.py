"""HLTV's live feed — Engine.IO v3 over the polling transport.

Why polling and not websocket (measured, see docs/recon/R4): the websocket
upgrade to scorebot-lb.hltv.org returns 403 to every non-browser client — bare
`websockets` and `curl_cffi.ws_connect` alike, with Origin, Referer, a browser
UA and warmed cookies. Polling goes through. A browser starts with polling
itself, so this is a regular transport of the same protocol.

The protocol:
    GET  /socket.io/?EIO=3&transport=polling
      <- 0{"sid":..,"pingInterval":25000,"pingTimeout":60000}
      <- 40
    POST to the same URL with &sid=
      <len>:42["readyForMatch","{\\"token\\":\\"\\",\\"listId\\":\\"<id>\\"}"]
    GET  to the same URL with &sid=   (long poll)
      <- 42["scoreboard",{...}] / 42["log","{...}"]

Two details, each of which produces a silent failure with no error:
  * the readyForMatch argument must be a JSON STRING, not an object;
  * you may only subscribe AFTER packet `40`. On a reconnect it does not
    arrive together with the handshake but on a later poll.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from curl_cffi.requests import AsyncSession

from ..config import url_allowed
from ..proxy import ProxySettings

log = logging.getLogger(__name__)

SCOREBOT_BASE = "https://scorebot-lb.hltv.org/socket.io/"
ORIGIN = "https://www.hltv.org"

ROUND_WARMUP = "warmup"
ROUND_FREEZE = "freezePeriod"
ROUND_STARTED = "started"
ROUND_ENDED = "ended"


class FeedRejected(RuntimeError):
    """403: the source did not accept the client. Not a network failure — back
    off for a long while."""


class FeedUnavailable(RuntimeError):
    """A network failure or a rejected session. A reason to reconnect."""


class FeedIdle(FeedUnavailable):
    """The long poll came back on a timeout with no data.

    This is NOT a disconnect. The feed goes quiet when nothing is happening on
    the map — the whole break between maps passes like this. The connection is
    alive, the sid is valid, we simply poll again. Treating it as a disconnect
    would mean reconnecting every 45 seconds and pestering the source exactly
    while we are waiting for the next map to start.
    """


@dataclass(frozen=True)
class PlayerLine:
    """A player in a scoreboard frame.

    `kills` and `clutches` are both accumulated FOR THE MAP, which is what lets
    a round's worth of either be read as an increment against a baseline taken
    when the round began. `alive` is the only one that describes this instant.
    """

    steam_id: str
    nick: str
    kills: int
    # Defaulted because a frame is not obliged to carry them, and because every
    # test that builds a player by hand cares about the kills alone.
    alive: bool = True
    # HLTV's own count of rounds this player won as the last one alive
    # (`advancedStats.oneOnXWins`). Whether a clutch was WON is its verdict and
    # not ours: it also covers the round taken on the bomb or the clock, which
    # no reading of the alive counts can tell from a round simply running out.
    clutches: int = 0
    # HLTV's own player id (`dbId`), and the ONE field that joins a scoreboard
    # row to a `Kill` in the log: measured on the forze recording, the set of
    # `dbId` in the frames and the set of `killerId` in the log are the same
    # ten numbers. The nick cannot do this job — the log carries BOTH a nick
    # and an in-game name and swaps which is which between players (`nick`
    # "reyoz" with `name` "chronic111") — and the SIDE cannot either, because
    # the sides swap at the break while the backlog replays kills from before
    # it. None when the frame did not carry it.
    player_id: Optional[int] = None


@dataclass(frozen=True)
class RoundOutcome:
    """One decided round, from one team's point of view.

    Read off `ctMatchHistory` / `terroristMatchHistory`, which carry only
    rounds that have been DECIDED — measured on the forze recording, 12 + 11
    entries at round 23. So the list is the map's round-by-round result and
    nothing else, and `roundOrdinal` numbers it across the map rather than
    within the half.
    """

    ordinal: int
    won: bool


@dataclass(frozen=True)
class LiveFrame:
    """The scoreboard state at the moment of the frame."""

    map_name: str
    current_round: int
    round_state: str
    live: bool
    ct_team_id: Optional[int]
    ct_team_name: str
    ct_score: int
    t_team_id: Optional[int]
    t_team_name: str
    t_score: int
    regulation: int
    overtime: int
    # Which team STARTED on which side. Fixed for the whole map, unlike
    # ct_team_id, and the only way to say who was on CT in a round that is
    # already over — which the match log needs for every round of the backlog
    # the feed replays on connect.
    starting_ct: Optional[int] = None
    starting_t: Optional[int] = None
    ct_players: Tuple["PlayerLine", ...] = ()
    t_players: Tuple["PlayerLine", ...] = ()
    # The round-by-round result of the map, per side. Tied to ctTeamId/tTeamId
    # like the score and for the same reason: the histories swap hands at the
    # break, so the one read off the side alone belongs to the other team for
    # the whole second half.
    ct_history: Tuple["RoundOutcome", ...] = ()
    t_history: Tuple["RoundOutcome", ...] = ()

    def our_players(self, team_id: int) -> Tuple["PlayerLine", ...]:
        """Our team's roster. Sides swap after the break, so we go by id rather
        than by side."""
        if self.ct_team_id == team_id:
            return self.ct_players
        if self.t_team_id == team_id:
            return self.t_players
        return ()

    def their_players(self, team_id: int) -> Tuple["PlayerLine", ...]:
        """The opposing roster, from our team's id.

        How many of these are alive is the N in a 1vN, so it is tied to
        ctTeamId/tTeamId for the same reason `our_score` is: the sides swap at
        the break, and a clutch read off the side would name the wrong number
        for the whole second half.
        """
        if self.ct_team_id == team_id:
            return self.t_players
        if self.t_team_id == team_id:
            return self.ct_players
        return ()

    def our_score(self, team_id: int) -> Tuple[Optional[int], Optional[int]]:
        """The map score, oriented on our team.

        Sides swap after the break, so this must be tied to ctTeamId/tTeamId
        rather than to the sides themselves.
        """
        if self.ct_team_id == team_id:
            return self.ct_score, self.t_score
        if self.t_team_id == team_id:
            return self.t_score, self.ct_score
        return None, None

    def our_history(self, team_id: int) -> Tuple["RoundOutcome", ...]:
        """This team's round-by-round result on the map.

        By id, never by side: `ctMatchHistory` belongs to whoever is on CT in
        THIS frame, and that is the other team for the whole second half.
        """
        if self.ct_team_id == team_id:
            return self.ct_history
        if self.t_team_id == team_id:
            return self.t_history
        return ()

    def opponent_name(self, team_id: int) -> str:
        if self.ct_team_id == team_id:
            return self.t_team_name
        if self.t_team_id == team_id:
            return self.ct_team_name
        return ""

    @property
    def in_play(self) -> bool:
        """The map is being played, not warming up between maps."""
        return self.live and self.round_state != ROUND_WARMUP

    @property
    def coherent(self) -> bool:
        """Could this map have produced this score by this round.

        After N rounds at most N are decided, so `ct + t <= currentRound` is a
        physical invariant of the game — and measured on every frame of both
        recordings (4005 frames) it holds without exception. It is `<=` and
        never equality on purpose: a `freezePeriod` frame keeps the number of
        the round that just ENDED while carrying its score (round 3, 2:1), so
        the sum equals the round there and is one short of it in `started`.

        Seen live: a fresh map's first non-warmup frame claiming round 1 with
        thirteen rounds decided (9:4). Where the score came from HLTV does not
        say; what matters is that a frame that breaks this is not evidence
        about this map, and everything that reads frames must ignore it whole.

        With no round number at all (absent, or 0 — every recorded frame says
        at least 1) there is nothing to judge by, and the frame passes: the
        guard refuses only what it can prove. Reading a missing counter as
        round 0 would discard every frame of every map the moment HLTV
        dropped the field, which is a different failure from the one this
        exists for.
        """
        if self.current_round <= 0:
            return True
        return self.ct_score + self.t_score <= self.current_round


@dataclass(frozen=True)
class KillEvent:
    """One kill out of the feed's `log`.

    The log was unused by this service for a long time and for a good reason:
    on every connect the server replays the whole backlog, so anything built
    on "an entry arrived" is a barrage of duplicates (8255 `Kill` entries for
    543 real kills in the forze recording). What makes it usable at all is
    `eventId` — unique per kill, and measured STRICTLY INCREASING in arrival
    order across both recordings, all 731 kills, with no exception. So one
    number per match is the whole protection against counting a kill twice,
    and unlike a set of ids it survives a restart.

    Only the fields something reads are here. `headShot`, `noScope`,
    `killerBlind`, `attackerInAir`, the coordinates and the flash assist are
    all in the frame and all deliberately absent: a field nothing reads is a
    trap this project has already been caught by twice.
    """

    event_id: int
    # HLTV's player id, the join to `PlayerLine.player_id`. None means the
    # kill cannot be attributed to a team and is dropped.
    killer_id: Optional[int]
    killer_nick: str
    weapon: str
    through_smoke: bool
    penetrated: bool

    @property
    def is_knife(self) -> bool:
        """Every knife is a `knife_*`, skins included — `knife_butterfly`,
        `knife_karambit`, `knife_m9_bayonet`, and the default T knife
        `knife_t`, which is the one that does not read like a skin."""
        return self.weapon.startswith("knife")


def _players(raw) -> Tuple[PlayerLine, ...]:
    """One side's players. `score` in the frame means kills for the map."""
    if not isinstance(raw, list):
        return ()
    lines = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        nick = str(item.get("nick") or item.get("name") or "").strip()
        steam_id = str(item.get("steamId") or item.get("dbId") or nick)
        if not nick:
            continue
        # `advancedStats` is NOT always there: measured absent in 1176 of the
        # 21126 player entries of the map-boundary recording, all of them in the
        # warmup of a fresh map. Indexing it straight would raise inside the
        # frame loop, which is the one place an exception costs the whole feed.
        advanced = item.get("advancedStats")
        if not isinstance(advanced, dict):
            advanced = {}
        try:
            player_id = int(item["dbId"])
        except (KeyError, TypeError, ValueError):
            # The join to the log is lost for this player, nothing more. Every
            # recorded frame carried it; a frame that does not is not a reason
            # to drop the player from the scoreboard.
            player_id = None
        lines.append(PlayerLine(steam_id=steam_id, nick=nick,
                                kills=int(item.get("score") or 0),
                                alive=bool(item.get("alive", True)),
                                clutches=int(advanced.get("oneOnXWins") or 0),
                                player_id=player_id))
    return tuple(lines)


def _history(raw) -> Tuple[RoundOutcome, ...]:
    """One side's round-by-round result, both halves in one ordered list.

    The feed splits it into `firstHalf` and `secondHalf`; `roundOrdinal`
    numbers the rounds across the whole map, so the halves are concatenated
    and sorted rather than kept apart — a run of won rounds does not stop at
    the break, and neither does the numbering.

    `type` is the outcome from THIS side's point of view, and the only value
    that means a loss is the literal `lost`; the rest (`CTs_Win`,
    `Terrorists_Win`, `Target_Bombed`, `Target_Saved`, `Bomb_Defused`) are all
    wins. A round with no readable ordinal is dropped: a round we cannot place
    cannot be part of a streak, and placing it by position would renumber
    every round after it.
    """
    if not isinstance(raw, dict):
        return ()
    outcomes = []
    for half in ("firstHalf", "secondHalf"):
        for item in raw.get(half) or []:
            if not isinstance(item, dict):
                continue
            try:
                ordinal = int(item["roundOrdinal"])
            except (KeyError, TypeError, ValueError):
                continue
            outcome = str(item.get("type") or "").strip()
            if not outcome:
                continue
            outcomes.append(RoundOutcome(ordinal=ordinal, won=outcome != "lost"))
    outcomes.sort(key=lambda item: item.ordinal)
    return tuple(outcomes)


def parse_scoreboard(payload: dict) -> Optional[LiveFrame]:
    """A scoreboard frame into an observation. None means the frame is unusable.

    An empty mapName shows up in transitional frames: drawing conclusions from
    it would send "map started" with an empty name.
    """
    map_name = (payload.get("mapName") or "").strip()
    if not map_name:
        return None
    return LiveFrame(
        map_name=map_name,
        current_round=int(payload.get("currentRound") or 0),
        round_state=str(payload.get("currentRoundState") or ""),
        live=bool(payload.get("live")),
        ct_team_id=payload.get("ctTeamId"),
        ct_team_name=str(payload.get("ctTeamName") or ""),
        ct_score=int(payload.get("ctTeamScore") or 0),
        t_team_id=payload.get("tTeamId"),
        # The side names are asymmetric in the feed: ctTeamName, but
        # terroristTeamName.
        t_team_name=str(payload.get("terroristTeamName") or ""),
        t_score=int(payload.get("tTeamScore") or 0),
        regulation=int(payload.get("regulationHalfLength") or 12),
        overtime=int(payload.get("overtimeHalfLength") or 3),
        starting_ct=payload.get("startingCt"),
        starting_t=payload.get("startingT"),
        ct_players=_players(payload.get("CT")),
        t_players=_players(payload.get("TERRORIST")),
        ct_history=_history(payload.get("ctMatchHistory")),
        t_history=_history(payload.get("terroristMatchHistory")),
    )


def decode_payload(body: bytes) -> List[str]:
    """Split a polling response into individual packets.

    Framing: each packet is a byte 0x00 (string) or 0x01 (binary), then the
    length ONE DIGIT PER BYTE (the value 0..9, not ASCII), then 0xff, then the
    body. A textual variant "<len>:<body>" also occurs.

    This has to work on bytes: decoding to text turns the length digits into
    \\ufffd and breaks the parsing.
    """
    packets: List[str] = []
    i = 0
    while i < len(body):
        if body[i] in (0x00, 0x01):
            i += 1
            digits = []
            while i < len(body) and body[i] != 0xFF:
                digits.append(str(body[i]))
                i += 1
            i += 1
            length = int("".join(digits) or "0")
            packets.append(body[i:i + length].decode("utf-8", "replace"))
            i += length
        else:
            match = re.match(rb"(\d+):", body[i:])
            if not match:
                break
            length = int(match.group(1))
            start = i + match.end()
            packets.append(body[start:start + length].decode("utf-8", "replace"))
            i = start + length
    return packets


class ScorebotClient:
    """One connection per match. Reconnecting is the caller's business."""

    def __init__(self, match_id: int, *, referer: Optional[str] = None,
                 impersonate: str = "chrome",
                 proxy: Optional[ProxySettings] = None):
        self.match_id = str(match_id)
        # The referer comes from the database, and into the database from the
        # HLTV page. A real request is made to it (the cookie warm-up), so a
        # foreign address is not taken at all: the feed does not break, only
        # the warm-up is lost.
        if referer and not url_allowed(referer):
            log.warning("foreign match address skipped, warming up without it: %s",
                        referer)
            referer = None
        self.referer = referer
        self._impersonate = impersonate
        # The proxy settings rather than a ready-made dict: the client talks to
        # TWO hosts — scorebot-lb.hltv.org and the match page for the warm-up —
        # and a NO_PROXY exception may cover only one of them.
        self._proxy = proxy or ProxySettings()
        self._session: Optional[AsyncSession] = None
        self.sid: Optional[str] = None
        self.ping_interval = 25.0
        self._last_ping = 0.0
        self._buffer: List[str] = []
        self._ready = False

    # ------------------------------------------------------------------

    def _url(self, with_sid: bool = True) -> str:
        url = f"{SCOREBOT_BASE}?EIO=3&transport=polling&t={int(time.time() * 1000)}"
        if with_sid and self.sid:
            url += f"&sid={self.sid}"
        return url

    @property
    def _headers(self) -> dict:
        headers = {"Origin": ORIGIN}
        if self.referer:
            headers["Referer"] = self.referer
        return headers

    @staticmethod
    def _check(response) -> None:
        if response.status_code == 403:
            raise FeedRejected("403 on polling — the session burned out")
        if response.status_code >= 400:
            raise FeedUnavailable(f"HTTP {response.status_code}")

    async def connect(self) -> None:
        self._session = AsyncSession(impersonate=self._impersonate)
        # Warm-up: the match page sets Cloudflare cookies on .hltv.org.
        if self.referer:
            try:
                await self._session.get(self.referer, timeout=30,
                                        proxies=self._proxy.for_url(self.referer))
            except Exception as exc:  # noqa: BLE001 - the warm-up is optional
                log.debug("session warm-up failed: %s", exc)

        try:
            url = self._url(with_sid=False)
            response = await self._session.get(url, headers=self._headers, timeout=30,
                                               proxies=self._proxy.for_url(url))
        except Exception as exc:  # noqa: BLE001 - network
            raise FeedUnavailable(f"{type(exc).__name__}: {exc}") from exc
        self._check(response)

        packets = decode_payload(response.content)
        handshake = next((p for p in packets if p.startswith("0{")), None)
        if handshake is None:
            raise FeedUnavailable("no handshake in the response")
        info = json.loads(handshake[1:])
        self.sid = info["sid"]
        self.ping_interval = info.get("pingInterval", 25000) / 1000
        self._last_ping = time.time()
        rest = [p for p in packets if p is not handshake]
        self._buffer.extend(rest)
        self._ready = "40" in rest

    async def subscribe(self, wait_polls: int = 3) -> None:
        """Subscribing strictly AFTER packet `40`.

        Send readyForMatch earlier and the server silently ignores the
        subscription: the connection is alive, there are no frames. And no
        error either.
        """
        while not self._ready and wait_polls > 0:
            packets = await self._poll_raw()
            self._buffer.extend(packets)
            self._ready = "40" in packets
            wait_polls -= 1
        if not self._ready:
            raise FeedUnavailable("packet 40 never arrived — the subscription "
                                  "would have been ignored")
        payload = json.dumps({"token": "", "listId": self.match_id})
        await self._send("42" + json.dumps(["readyForMatch", payload]))

    async def _send(self, packet: str) -> None:
        assert self._session is not None
        try:
            url = self._url()
            response = await self._session.post(
                url, data=f"{len(packet)}:{packet}",
                headers={**self._headers, "Content-Type": "text/plain;charset=UTF-8"},
                timeout=30, proxies=self._proxy.for_url(url))
        except Exception as exc:  # noqa: BLE001 - network
            raise FeedUnavailable(f"{type(exc).__name__}: {exc}") from exc
        self._check(response)

    async def _poll_raw(self, timeout: int = 45) -> List[str]:
        assert self._session is not None
        if time.time() - self._last_ping >= self.ping_interval:
            await self._send("2")
            self._last_ping = time.time()
        try:
            url = self._url()
            response = await self._session.get(url, headers=self._headers,
                                               timeout=timeout,
                                               proxies=self._proxy.for_url(url))
        except Exception as exc:  # noqa: BLE001 - network
            if "timed out" in str(exc).lower() or type(exc).__name__ == "Timeout":
                raise FeedIdle("long poll with no data") from exc
            raise FeedUnavailable(f"{type(exc).__name__}: {exc}") from exc
        self._check(response)
        return decode_payload(response.content)

    async def poll(self, timeout: int = 45) -> List[str]:
        if self._buffer:
            buffered, self._buffer = self._buffer, []
            return buffered
        return await self._poll_raw(timeout)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


@dataclass(frozen=True)
class LogEntry:
    """One entry of the feed's `log`, whatever its type.

    The log is an append-only stream on the server, and a connect replays all
    of it. `kind` is the key the entry arrived under and `data` is that key's
    object, untouched: the types and their shapes are measured in R4 and
    nothing here guesses at a field that was not seen.

    Only `Kill` and `Assist` carry an id (`eventId` / `killEventId`). Every
    other type is anonymous, which is what `matchlog.new_entries` exists to
    work around.
    """

    kind: str
    data: dict

    @property
    def event_id(self) -> Optional[int]:
        """The kill this entry IS, or the kill it belongs to.

        An `Assist` names the kill it assisted (`killEventId`), which is both
        how it is folded into that kill's line and how it is placed in the
        stream. Nothing else has one.
        """
        for field_name in ("eventId", "killEventId"):
            try:
                return int(self.data[field_name])
            except (KeyError, TypeError, ValueError):
                continue
        return None


def parse_log(payload: str) -> Tuple[LogEntry, ...]:
    """One log event into its entries, OLDEST FIRST.

    The feed sends them newest first — measured, every packet of both
    recordings is in descending `eventId` — and everything downstream reads
    them as a stream in time order: the round an entry belongs to, the running
    count, the high-water mark, the match log's own ordering. So they are
    turned round here, once, rather than at each of those places.

    The payload is a JSON STRING holding `{"log": [{"<Type>": {...}}, ...]}`,
    not an object. Every type is kept, including the ones no decision is made
    from: the match log prints them, and an entry dropped here would also
    shift the positions the match log counts by.
    """
    try:
        body = json.loads(payload)
    except (ValueError, TypeError):
        return ()
    if not isinstance(body, dict):
        return ()
    entries: List[LogEntry] = []
    for item in body.get("log") or []:
        if not isinstance(item, dict):
            continue
        for kind, data in item.items():
            if isinstance(data, dict):
                entries.append(LogEntry(kind=str(kind), data=data))
    entries.reverse()
    return tuple(entries)


def kills_from_log(entries: Tuple[LogEntry, ...]) -> Tuple[KillEvent, ...]:
    """The `Kill` entries as the typed events the bingo card counts."""
    kills: List[KillEvent] = []
    for entry in entries:
        if entry.kind != "Kill":
            continue
        raw = entry.data
        try:
            event_id = int(raw["eventId"])
        except (KeyError, TypeError, ValueError):
            # Without an id there is no way to tell this kill from the copy of
            # it the next connect will replay. Dropping is the only safe read.
            continue
        try:
            killer_id = int(raw["killerId"])
        except (KeyError, TypeError, ValueError):
            killer_id = None
        kills.append(KillEvent(
            event_id=event_id,
            killer_id=killer_id,
            killer_nick=str(raw.get("killerNick") or raw.get("killerName") or ""),
            weapon=str(raw.get("weapon") or ""),
            through_smoke=bool(raw.get("throughSmoke")),
            penetrated=bool(raw.get("penetrated")),
        ))
    return tuple(kills)


def parse_kills(payload: str) -> Tuple[KillEvent, ...]:
    """The `Kill` entries of one log event, oldest first."""
    return kills_from_log(parse_log(payload))


def feed_items(packets: List[str]) -> List[Tuple[str, object]]:
    """Everything usable in a batch, IN THE ORDER THE FEED SENT IT.

    `("frame", LiveFrame)` and `("log", (LogEntry, ...))`. The order is the
    point: a log entry carries no map and no round of its own, so it is placed
    by the last frame seen before it. Sorting the frames out first and the log
    afterwards would hand every entry of the batch the round the batch ENDED
    in, which across a round boundary is the wrong round and across a map
    boundary the wrong map.

    Scoreboard frames stay the only thing DECISIONS are made from — the log is
    read for what a frame cannot say (which weapon, through what, who planted)
    and for the match log, which is a transcript rather than a decision.
    Transitions are still born on comparison with stored state, because the
    log replays its whole backlog on every connect.
    """
    items: List[Tuple[str, object]] = []
    for packet in packets:
        if not packet.startswith("42"):
            continue
        try:
            name, payload = json.loads(packet[2:])
        except (ValueError, TypeError):
            continue
        if name == "scoreboard" and isinstance(payload, dict):
            frame = parse_scoreboard(payload)
            if frame is not None:
                items.append(("frame", frame))
        elif name == "log" and isinstance(payload, str):
            entries = parse_log(payload)
            if entries:
                items.append(("log", entries))
    return items


def frames_from_packets(packets: List[str]) -> List[LiveFrame]:
    """Scoreboard frames alone, for callers that have no use for the log."""
    return [item for kind, item in feed_items(packets) if kind == "frame"]
