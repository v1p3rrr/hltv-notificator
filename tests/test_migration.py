"""Обновление старой базы.

Схема менялась много раз, и база — не кэш: в ней журнал отправленного. Если
миграция потеряет его, сервис сочтёт новым всё подряд и разошлёт уведомления
повторно. Поэтому здесь собирается база ПЕРВОЙ версии и открывается текущим
кодом.
"""

import sqlite3

import pytest

from hltv_notify.state.db import Storage

# Схема ровно та, что была на первом этапе: ни команд, ни подписчиков,
# ни приватных памяток машин.
LEGACY_SCHEMA = """
CREATE TABLE matches (
    match_id       INTEGER PRIMARY KEY,
    opponent_id    INTEGER,
    opponent_name  TEXT NOT NULL,
    event_name     TEXT NOT NULL,
    match_format   TEXT,
    start_utc      TEXT NOT NULL,
    url            TEXT NOT NULL,
    snapshot       TEXT NOT NULL,
    snapshot_hash  TEXT NOT NULL,
    first_seen_utc TEXT NOT NULL,
    updated_utc    TEXT NOT NULL,
    missing_since_utc TEXT
);
CREATE TABLE match_state (
    match_id           INTEGER PRIMARY KEY,
    state              TEXT NOT NULL,
    current_map_number INTEGER,
    current_map_name   TEXT,
    current_map_score  TEXT,
    series_score       TEXT,
    last_seen_utc      TEXT NOT NULL,
    last_source        TEXT NOT NULL,
    pending_start_utc  TEXT,
    pending_since_utc  TEXT
);
CREATE TABLE map_results (
    match_id       INTEGER NOT NULL,
    map_number     INTEGER NOT NULL,
    map_name       TEXT NOT NULL,
    score_team     INTEGER NOT NULL,
    score_opponent INTEGER NOT NULL,
    overtime       INTEGER NOT NULL DEFAULT 0,
    recorded_utc   TEXT NOT NULL,
    PRIMARY KEY (match_id, map_number)
);
CREATE TABLE sent_events (
    idempotency_key TEXT PRIMARY KEY,
    event_type      TEXT NOT NULL,
    match_id        INTEGER,
    created_utc     TEXT NOT NULL
);
CREATE TABLE outbox (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key     TEXT NOT NULL,
    body                TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    attempts            INTEGER NOT NULL DEFAULT 0,
    next_attempt_utc    TEXT NOT NULL,
    telegram_message_id INTEGER,
    created_utc         TEXT NOT NULL,
    sent_utc            TEXT
);
CREATE TABLE raw_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    source TEXT NOT NULL,
    url    TEXT,
    status TEXT,
    body   TEXT
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


@pytest.fixture()
def legacy_db(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO matches (match_id, opponent_name, event_name, start_utc, url, "
        "snapshot, snapshot_hash, first_seen_utc, updated_utc) "
        "VALUES (1, 'Color', 'GLuck', '2026-08-29T09:05:00+00:00', 'u', '{}', 'h', "
        "'2026-08-29T00:00:00+00:00', '2026-08-29T00:00:00+00:00')")
    conn.execute(
        "INSERT INTO match_state (match_id, state, last_seen_utc, last_source) "
        "VALUES (1, 'FINISHED', '2026-08-29T12:00:00+00:00', 'match_page')")
    conn.execute(
        "INSERT INTO sent_events (idempotency_key, event_type, match_id, created_utc) "
        "VALUES ('E6:1:map:1:result:13-10', 'E6', 1, '2026-08-29T11:00:00+00:00')")
    conn.execute(
        "INSERT INTO map_results (match_id, map_number, map_name, score_team, "
        "score_opponent, recorded_utc) VALUES (1, 1, 'Mirage', 13, 10, "
        "'2026-08-29T11:00:00+00:00')")
    conn.commit()
    conn.close()
    return path


def test_old_database_opens(legacy_db):
    storage = Storage(legacy_db)
    storage.close()


def test_journal_of_sent_events_survives(legacy_db):
    """Самое важное: без журнала сервис разошлёт всё заново."""
    storage = Storage(legacy_db)
    keys = [row["idempotency_key"] for row in
            storage.conn.execute("SELECT idempotency_key FROM sent_events")]
    assert "E6:1:map:1:result:13-10" in keys
    storage.close()


def test_matches_and_results_survive(legacy_db):
    storage = Storage(legacy_db)
    match = storage.get_match(1)
    assert match["opponent_name"] == "Color"
    assert match["team_id"] is None          # колонка появилась, значение пустое
    results = storage.map_results(1)
    assert (results[0]["map_name"], results[0]["score_team"]) == ("Mirage", 13)
    storage.close()


def test_new_columns_appear(legacy_db):
    storage = Storage(legacy_db)
    state = {row["name"] for row in storage.conn.execute("PRAGMA table_info(match_state)")}
    assert {"live_map_name", "page_seen_utc", "regulation_rounds",
            "overtime_rounds"} <= state
    outbox = {row["name"] for row in storage.conn.execute("PRAGMA table_info(outbox)")}
    assert "chat_id" in outbox
    storage.close()


def test_new_tables_appear(legacy_db):
    storage = Storage(legacy_db)
    tables = {row["name"] for row in storage.conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"subscribers", "teams", "match_teams", "reminders", "live_messages"} <= tables
    storage.close()


def test_service_works_on_a_migrated_database(legacy_db, config):
    """Не «открылась», а «работает»: заводим подписчика, команду и событие."""
    from hltv_notify.models import Event
    from hltv_notify.notify.outbox import Notifier

    storage = Storage(legacy_db)
    storage.add_subscriber("111")
    storage.add_team("111", 12857, "forze-reload", "FORZE Reload")
    storage.link_match_team(1, 12857)

    event = Event(type="E7", idempotency_key="E7:1:finished:2-0", match_id=1,
                  payload={"team_id": 12857, "opponent": "Color", "event_name": "GLuck",
                           "series_team": 2, "series_opponent": 0, "won": True,
                           "maps": [], "url": "u"})
    assert Notifier(storage, config, telegram=None).enqueue(event) is True
    assert storage.pending_count() == 1
    storage.close()


def test_already_sent_event_is_not_resent_after_upgrade(legacy_db, config):
    """Главная опасность обновления. Ключи в старой базе записаны без адресата,
    а новый код добавляет к ним префикс чата. Без разовой миграции ключей
    сервис при первом запуске счёл бы новым ВСЁ, что уже отправлял."""
    from hltv_notify.models import Event
    from hltv_notify.notify.outbox import Notifier

    storage = Storage(legacy_db)
    storage.adopt_legacy_event_keys("111")      # то, что делает __main__ при старте
    storage.add_subscriber("111")
    storage.add_team("111", 12857, "forze-reload", "FORZE Reload")
    storage.link_match_team(1, 12857)

    event = Event(type="E6", idempotency_key="E6:1:map:1:result:13-10", match_id=1,
                  payload={"team_id": 12857, "opponent": "Color", "event_name": "GLuck",
                           "map_number": 1, "map_name": "Mirage", "score_team": 13,
                           "score_opponent": 10, "series_team": 1, "series_opponent": 0,
                           "url": "u"})
    assert Notifier(storage, config, telegram=None).enqueue(event) is False
    assert storage.pending_count() == 0
    storage.close()


def test_key_adoption_runs_only_once(legacy_db):
    storage = Storage(legacy_db)
    assert storage.adopt_legacy_event_keys("111") == 1
    assert storage.adopt_legacy_event_keys("111") == 0   # флаг в meta
    keys = [row["idempotency_key"] for row in
            storage.conn.execute("SELECT idempotency_key FROM sent_events")]
    assert keys == ["111|E6:1:map:1:result:13-10"]
    storage.close()
