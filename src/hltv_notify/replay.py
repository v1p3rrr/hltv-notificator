"""Прогон записанного дампа живого фида через машину состояний.

Живой источник для тестов не годится: матчи нужной команды редки, а конец
карты, овертайм и обрыв связи по заказу не воспроизводятся. Поэтому дампы
реальных матчей лежат в docs/recon/fixtures и прогоняются отсюда.

Один и тот же дамп обязан всегда давать один и тот же список событий — это
регрессионный тест. А прогон дважды подряд не должен добавлять ни одного
уведомления: ровно то, что происходит при реконнекте, когда фид присылает
полное состояние заново.

Запуск: python -m hltv_notify.replay <файл.jsonl.gz> [--team-id N]
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
    """Записи дампа. Хвост может быть оборван, если процесс записи убили."""
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
                        help="прогнать дамп дважды и сравнить число событий")
    args = parser.parse_args(argv)

    config = Config()
    if args.team_id is not None:
        config = Config(team_id=args.team_id)

    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "replay.db")
        _prepare(storage, args.match_id)

        first = replay(args.dump, storage, config, args.match_id)
        print(f"событий за первый прогон: {len(first)}")
        for event in first:
            print(f"  {event.type}  {event.idempotency_key}")

        if args.twice:
            second = replay(args.dump, storage, config, args.match_id)
            print(f"событий за повторный прогон: {len(second)}")
            if second:
                print("ОШИБКА: повторный прогон породил события — дедупликация не держит")
                return 1
            print("повторный прогон не добавил ничего — дедупликация держит")
        storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
