"""Запись сырых кадров scorebot в gzip-JSONL — фикстура для replay-тестов.

Транспорт — Engine.IO v3 polling через curl_cffi (см. scripts/eio3.py:
websocket-апгрейд отдаёт 403 не-браузерным клиентам).

Подряд идущие байт-идентичные кадры схлопываются в {"kind":"repeat","n":N}:
именно такие повторы обязана переживать дедупликация, а объём фикстуры они
раздувают. Обрывы и переподключения помечаются явно — replay-тест на
дедупликацию опирается на них.
"""

import argparse
import gzip
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eio3 import Eio3Client, SessionRejected  # noqa: E402


def record(match_id: str, out_path: str, duration: int, referer: str = "") -> None:
    deadline = time.time() + duration
    attempt = 0
    with gzip.open(out_path, "at", encoding="utf-8") as out:

        def emit(rec: dict) -> None:
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()

        emit({"t": time.time(), "kind": "meta", "match_id": match_id,
              "transport": "eio3-polling"})
        prev, repeat = None, 0
        while time.time() < deadline:
            try:
                client = Eio3Client(match_id, referer=referer or None)
                client.connect()
                client.subscribe()
                attempt = 0
                emit({"t": time.time(), "kind": "connect", "sid": client.sid})
                for packet in client.events(deadline):
                    if packet == prev:
                        repeat += 1
                        continue
                    if repeat:
                        emit({"t": time.time(), "kind": "repeat", "n": repeat})
                        repeat = 0
                    emit({"t": time.time(), "kind": "frame", "raw": packet})
                    prev = packet
            except Exception as exc:  # noqa: BLE001 - обрыв это норма
                if repeat:
                    emit({"t": time.time(), "kind": "repeat", "n": repeat})
                    repeat = 0
                emit({"t": time.time(), "kind": "disconnect",
                      "error": f"{type(exc).__name__}: {exc}"})
                attempt += 1
                # 403 — это не сетевой сбой, а «отойди». Ретраить часто нельзя.
                if isinstance(exc, SessionRejected):
                    time.sleep(min(120 * attempt, 900))
                else:
                    time.sleep(min(2 ** attempt, 30))
        if repeat:
            emit({"t": time.time(), "kind": "repeat", "n": repeat})
        emit({"t": time.time(), "kind": "end"})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("match_id")
    ap.add_argument("out")
    ap.add_argument("--duration", type=int, default=3600)
    ap.add_argument("--referer", default="")
    a = ap.parse_args()
    record(a.match_id, a.out, a.duration, a.referer)
