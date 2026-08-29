"""Живой фид HLTV — Engine.IO v3 поверх polling-транспорта.

Почему polling, а не websocket (замерено, см. docs/recon/R4): апгрейд до
websocket отдаёт 403 любому не-браузерному клиенту — и голому `websockets`, и
`curl_cffi.ws_connect`, с Origin, Referer, браузерным UA и прогретыми куками.
Polling проходит. Браузер и сам начинает с polling, так что это штатный
транспорт того же протокола.

Протокол:
    GET  /socket.io/?EIO=3&transport=polling
      <- 0{"sid":..,"pingInterval":25000,"pingTimeout":60000}
      <- 40
    POST тем же URL с &sid=
      <len>:42["readyForMatch","{\\"token\\":\\"\\",\\"listId\\":\\"<id>\\"}"]
    GET  тем же URL с &sid=   (long-poll)
      <- 42["scoreboard",{...}] / 42["log","{...}"]

Две детали, каждая из которых даёт молчаливый отказ без ошибки:
  * аргумент readyForMatch обязан быть JSON-СТРОКОЙ, а не объектом;
  * подписываться можно только ПОСЛЕ пакета `40`. На реконнекте он приходит
    не вместе с handshake, а следующим poll'ом.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from curl_cffi.requests import AsyncSession

log = logging.getLogger(__name__)

SCOREBOT_BASE = "https://scorebot-lb.hltv.org/socket.io/"
ORIGIN = "https://www.hltv.org"

ROUND_WARMUP = "warmup"
ROUND_FREEZE = "freezePeriod"
ROUND_STARTED = "started"
ROUND_ENDED = "ended"


class FeedRejected(RuntimeError):
    """403: источник не принял клиента. Не сетевой сбой — отступаем надолго."""


class FeedUnavailable(RuntimeError):
    """Сетевой сбой или отвергнутая сессия. Повод переподключиться."""


class FeedIdle(FeedUnavailable):
    """Long-poll вернулся по таймауту без данных.

    Это НЕ обрыв. Фид молчит, когда на карте ничего не происходит — в
    перерыве между картами так проходит вся пауза. Соединение живо, sid
    действителен, надо просто опросить снова. Считать это обрывом значит
    переподключаться каждые 45 секунд и зря дёргать источник ровно тогда,
    когда мы ждём начала следующей карты.
    """


@dataclass(frozen=True)
class PlayerLine:
    """Игрок в кадре табло. `kills` — накопленные фраги ЗА КАРТУ."""

    steam_id: str
    nick: str
    kills: int


@dataclass(frozen=True)
class LiveFrame:
    """Состояние табло на момент кадра."""

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
        """Состав нашей команды. Стороны меняются после перерыва, поэтому
        определяем по id, а не по стороне."""
        if self.ct_team_id == team_id:
            return self.ct_players
        if self.t_team_id == team_id:
            return self.t_players
        return ()

    def our_score(self, team_id: int) -> Tuple[Optional[int], Optional[int]]:
        """Счёт карты, ориентированный на нашу команду.

        Стороны меняются после перерыва, поэтому привязываться надо к
        ctTeamId/tTeamId, а не к самим сторонам.
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
        """Карта в игре, а не в разминке между картами."""
        return self.live and self.round_state != ROUND_WARMUP


def _players(raw) -> Tuple[PlayerLine, ...]:
    """Игроки стороны. `score` в кадре — это фраги за карту."""
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
    """Кадр scoreboard в наблюдение. None — кадр непригоден.

    Пустой mapName встречается в переходных кадрах: строить по нему выводы
    нельзя, иначе «карта началась» прилетит с пустым названием.
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
        # Имена сторон в фиде лежат асимметрично: ctTeamName, но terroristTeamName.
        t_team_name=str(payload.get("terroristTeamName") or ""),
        t_score=int(payload.get("tTeamScore") or 0),
        regulation=int(payload.get("regulationHalfLength") or 12),
        overtime=int(payload.get("overtimeHalfLength") or 3),
        ct_players=_players(payload.get("CT")),
        t_players=_players(payload.get("TERRORIST")),
    )


def decode_payload(body: bytes) -> List[str]:
    """Разбор polling-ответа на отдельные пакеты.

    Framing: каждый пакет — байт 0x00 (строковый) или 0x01 (бинарный), затем
    длина ПО ОДНОЙ ЦИФРЕ В БАЙТЕ (значение 0..9, не ASCII), затем 0xff, затем
    тело. Встречается и текстовый вариант "<len>:<тело>".

    Работать надо по байтам: при декодировании в текст цифры длины
    превращаются в \\ufffd и разбор ломается.
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
    """Одно соединение на матч. Реконнект — забота вызывающего."""

    def __init__(self, match_id: int, *, referer: Optional[str] = None,
                 impersonate: str = "chrome"):
        self.match_id = str(match_id)
        self.referer = referer
        self._impersonate = impersonate
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
            raise FeedRejected("403 на polling — сессия сгорела")
        if response.status_code >= 400:
            raise FeedUnavailable(f"HTTP {response.status_code}")

    async def connect(self) -> None:
        self._session = AsyncSession(impersonate=self._impersonate)
        # Прогрев: страница матча ставит куки Cloudflare на .hltv.org.
        if self.referer:
            try:
                await self._session.get(self.referer, timeout=30)
            except Exception as exc:  # noqa: BLE001 - прогрев не обязателен
                log.debug("прогрев сессии не удался: %s", exc)

        try:
            response = await self._session.get(self._url(with_sid=False),
                                               headers=self._headers, timeout=30)
        except Exception as exc:  # noqa: BLE001 - сеть
            raise FeedUnavailable(f"{type(exc).__name__}: {exc}") from exc
        self._check(response)

        packets = decode_payload(response.content)
        handshake = next((p for p in packets if p.startswith("0{")), None)
        if handshake is None:
            raise FeedUnavailable("в ответе нет handshake")
        info = json.loads(handshake[1:])
        self.sid = info["sid"]
        self.ping_interval = info.get("pingInterval", 25000) / 1000
        self._last_ping = time.time()
        rest = [p for p in packets if p is not handshake]
        self._buffer.extend(rest)
        self._ready = "40" in rest

    async def subscribe(self, wait_polls: int = 3) -> None:
        """Подписка строго ПОСЛЕ пакета `40`.

        Если отправить readyForMatch раньше, сервер молча проигнорирует
        подписку: соединение живо, кадров нет. Ошибки при этом никакой.
        """
        while not self._ready and wait_polls > 0:
            packets = await self._poll_raw()
            self._buffer.extend(packets)
            self._ready = "40" in packets
            wait_polls -= 1
        if not self._ready:
            raise FeedUnavailable("не дождались пакета 40 — подписка была бы проигнорирована")
        payload = json.dumps({"token": "", "listId": self.match_id})
        await self._send("42" + json.dumps(["readyForMatch", payload]))

    async def _send(self, packet: str) -> None:
        assert self._session is not None
        try:
            response = await self._session.post(
                self._url(), data=f"{len(packet)}:{packet}",
                headers={**self._headers, "Content-Type": "text/plain;charset=UTF-8"},
                timeout=30)
        except Exception as exc:  # noqa: BLE001 - сеть
            raise FeedUnavailable(f"{type(exc).__name__}: {exc}") from exc
        self._check(response)

    async def _poll_raw(self, timeout: int = 45) -> List[str]:
        assert self._session is not None
        if time.time() - self._last_ping >= self.ping_interval:
            await self._send("2")
            self._last_ping = time.time()
        try:
            response = await self._session.get(self._url(), headers=self._headers,
                                               timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - сеть
            if "timed out" in str(exc).lower() or type(exc).__name__ == "Timeout":
                raise FeedIdle("long-poll без данных") from exc
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
    """Кадры scoreboard из пачки пакетов. Событие log здесь не нужно.

    Решения принимаются по scoreboard, а не по log намеренно: при каждом
    подключении фид проигрывает бэклог событий заново (на серии из двух карт
    насчитывалось 150 событий MatchStarted), и строить на нём переходы —
    гарантированные дубли.
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
