"""Recording raw scorebot frames into gzip JSONL — a fixture for replay tests.

The transport is Engine.IO v3 polling through curl_cffi (see scripts/eio3.py:
the websocket upgrade returns 403 to non-browser clients).

Consecutive byte-identical frames are collapsed into {"kind":"repeat","n":N}:
those repeats are exactly what deduplication has to survive, and they bloat the
fixture. Disconnects and reconnects are marked explicitly — the deduplication
replay test relies on them.
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
            except Exception as exc:  # noqa: BLE001 - a disconnect is normal
                if repeat:
                    emit({"t": time.time(), "kind": "repeat", "n": repeat})
                    repeat = 0
                emit({"t": time.time(), "kind": "disconnect",
                      "error": f"{type(exc).__name__}: {exc}"})
                attempt += 1
                # 403 is not a network failure but a "back off". Frequent retries are not on.
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
