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
    """A player in a scoreboard frame. `kills` are accumulated FOR THE MAP."""

    steam_id: str
    nick: str
    kills: int


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
    ct_players: Tuple["PlayerLine", ...] = ()
    t_players: Tuple["PlayerLine", ...] = ()

    def our_players(self, team_id: int) -> Tuple["PlayerLine", ...]:
        """Our team's roster. Sides swap after the break, so we go by id rather
        than by side."""
        if self.ct_team_id == team_id:
            return self.ct_players
        if self.t_team_id == team_id:
            return self.t_players
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
        lines.append(PlayerLine(steam_id=steam_id, nick=nick,
                                kills=int(item.get("score") or 0)))
    return tuple(lines)


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
        ct_players=_players(payload.get("CT")),
        t_players=_players(payload.get("TERRORIST")),
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


def frames_from_packets(packets: List[str]) -> List[LiveFrame]:
    """Scoreboard frames out of a batch of packets. The log event is not needed.

    Decisions are made from scoreboard and not from log deliberately: on every
    connect the feed replays the whole event backlog (150 MatchStarted events
    were counted over a two-map series), so building transitions on it means
    guaranteed duplicates.
    """
    frames: List[LiveFrame] = []
    for packet in packets:
        if not packet.startswith("42"):
            continue
        try:
            name, payload = json.loads(packet[2:])
        except (ValueError, TypeError):
            continue
        if name != "scoreboard" or not isinstance(payload, dict):
            continue
        frame = parse_scoreboard(payload)
        if frame is not None:
            frames.append(frame)
    return frames
