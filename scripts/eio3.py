"""A minimal Engine.IO v3 / socket.io v2 client over the polling transport.

Why polling and not websocket (verified 2026-08-29): the websocket upgrade to
scorebot-lb.hltv.org returns 403 to every non-browser client — bare
`websockets` and `curl_cffi.ws_connect` alike, with Origin, Referer, a browser
UA and warmed Cloudflare cookies. Polling goes through. A browser starts with
polling itself, so this is a regular transport of the same protocol.

EIO v3 framing in polling: packets follow one another, each starting with 0x00
(string) or 0x01 (binary), then the length one digit per byte (the value 0..9,
NOT ASCII), then 0xff, then the body. A textual variant "<len>:<body>" also
occurs. It has to be parsed as bytes: decoding to text turns the length digits
into \ufffd.
"""

import json
import re
import time
from typing import Iterator, List, Optional

from curl_cffi import requests

SCOREBOT_BASE = "https://scorebot-lb.hltv.org/socket.io/"


class SessionRejected(RuntimeError):
    """The source answered 403: the session burned out, a new one with a
    warm-up and a pause is needed."""


def decode_payload(body: bytes) -> List[str]:
    """Splits a polling response into individual packets."""
    packets: List[str] = []
    i = 0
    while i < len(body):
        if body[i] in (0x00, 0x01):
            i += 1
            digits = []
            while i < len(body) and body[i] != 0xFF:
                digits.append(str(body[i]))
                i += 1
            i += 1  # skip the 0xff
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


class Eio3Client:
    """Subscribing to a match and reading frames. One connection per match."""

    def __init__(self, match_id: str, referer: Optional[str] = None, impersonate: str = "chrome"):
        self.match_id = str(match_id)
        self.referer = referer
        self.session = requests.Session(impersonate=impersonate)
        self.headers = {"Origin": "https://www.hltv.org"}
        if referer:
            self.headers["Referer"] = referer
        self.sid: Optional[str] = None
        self.ping_interval = 25.0
        self._last_ping = 0.0
        self._buffer: List[str] = []
        self._ready = False

    def _url(self, with_sid: bool = True) -> str:
        url = f"{SCOREBOT_BASE}?EIO=3&transport=polling&t={int(time.time() * 1000)}"
        if with_sid and self.sid:
            url += f"&sid={self.sid}"
        return url

    @staticmethod
    def _check(resp) -> None:
        if resp.status_code == 403:
            raise SessionRejected("403 on polling — the session burned out")
        resp.raise_for_status()

    def connect(self) -> None:
        # Warm-up: the match page sets Cloudflare cookies on .hltv.org.
        if self.referer:
            self.session.get(self.referer, timeout=30)
        resp = self.session.get(self._url(with_sid=False), headers=self.headers, timeout=30)
        self._check(resp)
        packets = decode_payload(resp.content)
        handshake = next(p for p in packets if p.startswith("0{"))
        info = json.loads(handshake[1:])
        self.sid = info["sid"]
        self.ping_interval = info.get("pingInterval", 25000) / 1000
        self._last_ping = time.time()
        rest = [p for p in packets if p is not handshake]
        self._buffer.extend(rest)
        self._ready = "40" in rest

    def subscribe(self, wait_polls: int = 3) -> None:
        """Subscribing strictly AFTER packet `40` (the namespace connect).

        Send readyForMatch earlier and the server silently ignores the
        subscription: the connection is alive, `40` arrives, there are no
        frames. Observed on a reconnect, where `40` came not with the handshake
        but on the next poll.
        """
        while not self._ready and wait_polls > 0:
            packets = self._poll_raw()
            self._buffer.extend(packets)
            self._ready = "40" in packets
            wait_polls -= 1
        if not self._ready:
            raise RuntimeError("packet 40 never arrived — the subscription would have been ignored")
        payload = json.dumps({"token": "", "listId": self.match_id})
        self._send("42" + json.dumps(["readyForMatch", payload]))

    def _send(self, packet: str) -> None:
        resp = self.session.post(
            self._url(),
            data=f"{len(packet)}:{packet}",
            headers={**self.headers, "Content-Type": "text/plain;charset=UTF-8"},
            timeout=30,
        )
        self._check(resp)

    def _poll_raw(self, timeout: int = 45) -> List[str]:
        if time.time() - self._last_ping >= self.ping_interval:
            self._send("2")
            self._last_ping = time.time()
        resp = self.session.get(self._url(), headers=self.headers, timeout=timeout)
        self._check(resp)
        return decode_payload(resp.content)

    def poll(self, timeout: int = 45) -> List[str]:
        if self._buffer:
            buffered, self._buffer = self._buffer, []
            return buffered
        return self._poll_raw(timeout)

    def events(self, deadline: float) -> Iterator[str]:
        """Frames until the deadline. Reconnecting is the caller's business."""
        while time.time() < deadline:
            for packet in self.poll():
                yield packet
