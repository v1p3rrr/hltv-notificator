"""Replay записанного живого матча.

Дамп снят с реального матча FORZE Reload (2397053): 2150 записей, 1977
кадров, 15 подключений и 14 обрывов за прогон. Такое по заказу не
воспроизведёшь, поэтому он и лежит в репозитории.

Обязательные по ТЗ проверки на дедупликацию — здесь: прогон дважды подряд и
прогон с искусственным обрывом посередине.
"""

from pathlib import Path

import pytest

from conftest import FIXTURES
from hltv_notify.notify.outbox import Notifier
from hltv_notify.replay import _prepare, frames, replay
from hltv_notify.state.db import Storage
from hltv_notify.state.live_machine import LiveMachine

DUMP = FIXTURES / "scorebot-2397053-forze.jsonl.gz"
MATCH_ID = 2397053
LINEUP = ["Mirage", "Dust2", "Ancient"]


@pytest.fixture()
def prepared(storage):
    _prepare(storage, MATCH_ID)
    storage.set_map_lineup(MATCH_ID, LINEUP)
    return storage


def test_dump_is_readable():
    assert DUMP.exists()
    assert sum(1 for _ in frames(DUMP)) > 500


def test_replay_produces_the_expected_events(prepared, config):
    """Записанный матч всегда даёт один и тот же список событий."""
    events = replay(DUMP, prepared, config, MATCH_ID)
    assert [e.type for e in events] == ["E5", "E6"]
    assert [e.idempotency_key for e in events] == [
        "E5:2397053:map:2:started:Dust2",
        "E6:2397053:map:2:result:10-13",
    ]


def test_replayed_score_matches_the_site(prepared, config):
    """На сайте эта карта закончилась 10:13 не в нашу пользу."""
    events = replay(DUMP, prepared, config, MATCH_ID)
    e6 = events[-1]
    assert (e6.payload["score_team"], e6.payload["score_opponent"]) == (10, 13)
    assert (e6.payload["series_team"], e6.payload["series_opponent"]) == (0, 1)
    assert e6.payload["map_name"] == "Dust2"
    assert e6.payload["overtime"] is False


def test_running_the_same_dump_twice_adds_nothing(prepared, config):
    """Главная ловушка проекта: фид присылает полное состояние заново."""
    first = replay(DUMP, prepared, config, MATCH_ID)
    second = replay(DUMP, prepared, config, MATCH_ID)
    assert len(first) == 2
    assert second == []


def test_break_in_the_middle_and_full_state_again(prepared, config):
    """Искусственный обрыв: половина кадров, затем всё с начала — ровно то,
    что происходит при реконнекте посреди карты."""
    machine = LiveMachine(prepared, config)
    all_frames = list(frames(DUMP))
    half = len(all_frames) // 2

    produced = []
    for frame in all_frames[:half]:
        produced += machine.apply(MATCH_ID, frame)
    # «обрыв» — и фид отдаёт историю сначала
    for frame in all_frames:
        produced += machine.apply(MATCH_ID, frame)

    assert [e.type for e in produced] == ["E5", "E6"]


def test_notifications_are_not_duplicated_across_replays(tmp_path, config):
    """Тот же прогон, но через нотификатор: число уведомлений не меняется."""
    storage = Storage(tmp_path / "replay.db")
    _prepare(storage, MATCH_ID)
    storage.set_map_lineup(MATCH_ID, LINEUP)
    notifier = Notifier(storage, config, telegram=None)

    for _ in range(3):
        for event in replay(DUMP, storage, config, MATCH_ID):
            notifier.enqueue(event)

    assert storage.sent_event_count() == 2
    assert storage.pending_count() == 2
    storage.close()


def test_map_result_is_recorded_once(prepared, config):
    replay(DUMP, prepared, config, MATCH_ID)
    replay(DUMP, prepared, config, MATCH_ID)
    rows = prepared.map_results(MATCH_ID)
    assert len(rows) == 1
    assert (rows[0]["map_number"], rows[0]["map_name"]) == (2, "Dust2")
    assert (rows[0]["score_team"], rows[0]["score_opponent"]) == (10, 13)


# ----------------------------------------------------------------------
# Второй дамп: живой матч с границей карты и настоящим мультикиллом
# ----------------------------------------------------------------------

BOUNDARY = FIXTURES / "scorebot-2396936-map-boundary.jsonl.gz"
MOUZ_MATCH = 2396936
MOUZ_ID = 4494


@pytest.fixture()
def mouz(storage):
    _prepare(storage, MOUZ_MATCH)
    storage.set_map_lineup(MOUZ_MATCH, ["Ancient", "Mirage", "Nuke"])
    return storage


@pytest.fixture()
def mouz_config():
    from hltv_notify.config import Config
    return Config(team_id=MOUZ_ID, team_name="MOUZ")


def test_boundary_dump_gives_map_start_multikill_and_map_end(mouz, mouz_config):
    """Запись сделана с живого матча BLAST: карта Ancient от начала до конца."""
    events = replay(BOUNDARY, mouz, mouz_config, MOUZ_MATCH)
    assert [e.type for e in events] == ["E5", "E9", "E6"]


def test_real_multikill_is_detected(mouz, mouz_config):
    """xertioN взял 4 фрага в 15-м раунде — событие рождено по приросту
    фрагов в кадрах табло, без единого обращения к логу."""
    e9 = [e for e in replay(BOUNDARY, mouz, mouz_config, MOUZ_MATCH) if e.type == "E9"][0]
    assert e9.payload["nick"] == "xertioN"
    assert e9.payload["kills"] == 4
    assert e9.payload["round"] == 15
    assert e9.payload["map_name"] == "Ancient"


def test_real_map_end_score(mouz, mouz_config):
    e6 = [e for e in replay(BOUNDARY, mouz, mouz_config, MOUZ_MATCH) if e.type == "E6"][0]
    assert (e6.payload["score_team"], e6.payload["score_opponent"]) == (13, 4)
    assert e6.payload["map_number"] == 1


def test_boundary_dump_is_idempotent(mouz, mouz_config):
    replay(BOUNDARY, mouz, mouz_config, MOUZ_MATCH)
    assert replay(BOUNDARY, mouz, mouz_config, MOUZ_MATCH) == []
