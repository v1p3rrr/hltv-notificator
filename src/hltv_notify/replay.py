"""Replaying a recorded live-feed dump through the state machine.

A live source is no good for tests: matches of the team in question are rare,
and the end of a map, an overtime and a dropped connection cannot be
reproduced on demand. So dumps of real matches live in docs/recon/fixtures and
are replayed from here.

One and the same dump must always produce one and the same list of events —
that is a regression test. And replaying it twice in a row must not add a
single notification: that is exactly what happens on a reconnect, when the
feed sends the full state again.

Usage: python -m hltv_notify.replay <file.jsonl.gz> [--team-id N]
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import tempfile
from pathlib import Path
from typing import Iterator, List, Optional

from .config import Config
from .models import Event
from .sources.scorebot import LiveFrame, frames_from_packets
from .state.db import Storage, utcnow
from .state.live_machine import LiveMachine


def read_records(path: Path) -> Iterator[dict]:
    """Records from the dump. The tail may be truncated if the recording
    process was killed."""
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    return
    except EOFError:
        return


def frames(path: Path) -> Iterator[LiveFrame]:
    for record in read_records(path):
        if record.get("kind") != "frame":
            continue
        for frame in frames_from_packets([record["raw"]]):
            yield frame


def replay(path: Path, storage: Storage, config: Config, match_id: int) -> List[Event]:
    machine = LiveMachine(storage, config)
    produced: List[Event] = []
    for frame in frames(path):
        produced.extend(machine.apply(match_id, frame))
    return produced


def _prepare(storage: Storage, match_id: int) -> None:
    storage.upsert_match(
        match_id=match_id, opponent_id=None, opponent_name="—",
        event_name="replay", start_utc=utcnow(),
        url=f"https://www.hltv.org/matches/{match_id}/replay",
        snapshot={}, snapshot_hash="replay")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dump", type=Path)
    parser.add_argument("--team-id", type=int, default=None)
    parser.add_argument("--match-id", type=int, default=1)
    parser.add_argument("--twice", action="store_true",
                        help="replay the dump twice and compare the event counts")
    args = parser.parse_args(argv)

    config = Config()
    if args.team_id is not None:
        config = Config(team_id=args.team_id)

    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "replay.db")
        _prepare(storage, args.match_id)

        first = replay(args.dump, storage, config, args.match_id)
        print(f"events on the first pass: {len(first)}")
        for event in first:
            print(f"  {event.type}  {event.idempotency_key}")

        if args.twice:
            second = replay(args.dump, storage, config, args.match_id)
            print(f"events on the second pass: {len(second)}")
            if second:
                print("ERROR: the second pass produced events — deduplication is not holding")
                return 1
            print("the second pass added nothing — deduplication holds")
        storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
