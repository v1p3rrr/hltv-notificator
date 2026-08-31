"""Upgrading an old database.

The schema changed many times, and the database is not a cache: it holds the
journal of what was sent. If a migration loses it, the service treats
everything as new and sends the notifications again. So here a database of the
FIRST version is assembled and opened with the current code.
"""

import sqlite3

import pytest

from hltv_notify.state.db import Storage

# Exactly the schema of stage one: no teams, no subscribers, none of the
# machines' private memos.
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
    """The most important part: without the journal the service resends it all."""
    storage = Storage(legacy_db)
    keys = [row["idempotency_key"] for row in
            storage.conn.execute("SELECT idempotency_key FROM sent_events")]
    assert "E6:1:map:1:result:13-10" in keys
    storage.close()


def test_matches_and_results_survive(legacy_db):
    storage = Storage(legacy_db)
    match = storage.get_match(1)
    assert match["opponent_name"] == "Color"
    assert match["team_id"] is None          # the column appeared, the value is empty
    results = storage.map_results(1)
    assert (results[0]["map_name"], results[0]["score_team"]) == ("Mirage", 13)
    storage.close()


def test_new_columns_appear(legacy_db):
    storage = Storage(legacy_db)
    state = {row["name"] for row in storage.conn.execute("PRAGMA table_info(match_state)")}
    assert {"live_map_name", "page_seen_utc", "regulation_rounds",
            "overtime_rounds"} <= state
    outbox = {row["name"] for row in storage.conn.execute("PRAGMA table_info(outbox)")}
    # event_type and match_id let the queue tell, at SEND time, whether the
    # live card has to move below the message it is about to deliver.
    assert {"chat_id", "event_type", "match_id"} <= outbox
    cards = {row["name"] for row in
             storage.conn.execute("PRAGMA table_info(live_messages)")}
    assert {"bury_seq", "posted_seq"} <= cards
    storage.close()


def test_a_row_queued_before_the_upgrade_moves_no_card(legacy_db):
    """Old outbox rows have no event_type, and must not be guessed at.

    A card that stays where it is beats one that moves for the wrong reason —
    and there is no match_id on those rows to move the right card anyway.
    """
    storage = Storage(legacy_db)
    storage.conn.execute(
        "INSERT INTO outbox (chat_id, idempotency_key, body, next_attempt_utc, "
        "created_utc) VALUES ('1', 'E12:1:map:1:half', 'x', '2020-01-01', '2020-01-01')")
    row = storage.conn.execute(
        "SELECT * FROM outbox ORDER BY id DESC LIMIT 1").fetchone()
    assert row["event_type"] is None and row["match_id"] is None
    storage.close()


def test_new_tables_appear(legacy_db):
    storage = Storage(legacy_db)
    tables = {row["name"] for row in storage.conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"subscribers", "teams", "match_teams", "reminders", "live_messages"} <= tables
    storage.close()


def test_service_works_on_a_migrated_database(legacy_db, config):
    """Not "it opened" but "it works": create a subscriber, a team and an event."""
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
    """The main danger of an upgrade. Keys in the old database were written
    without a recipient, and the new code prefixes them with the chat. Without
    a one-off key migration the service would treat EVERYTHING it had already
    sent as new on the first run."""
    from hltv_notify.models import Event
    from hltv_notify.notify.outbox import Notifier

    storage = Storage(legacy_db)
    storage.adopt_legacy_event_keys("111")      # what __main__ does at startup
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
    assert storage.adopt_legacy_event_keys("111") == 0   # the flag in meta
    keys = [row["idempotency_key"] for row in
            storage.conn.execute("SELECT idempotency_key FROM sent_events")]
    assert keys == ["111|E6:1:map:1:result:13-10"]
    storage.close()


def test_reminder_keys_gain_the_start_they_now_carry(tmp_path):
    """E10's key gained the start so a moved match gets a new reminder. What
    is already in the journal has to be rewritten with it, or the first run
    after the upgrade sends a second reminder for a start already announced.
    """
    from datetime import timedelta

    from hltv_notify.config import Config
    from hltv_notify.notify.outbox import Notifier
    from hltv_notify.reminders import ReminderScheduler
    from hltv_notify.state.db import iso, utcnow

    path = tmp_path / "old.db"
    chat = "111"
    start = utcnow() + timedelta(minutes=10)

    # A database written by the previous version: the reminder has gone out.
    first = Storage(path)
    first.add_subscriber(chat)
    first.add_team(chat, 12857, "forze-reload", "FORZE Reload")
    first.add_reminder(chat, 15)
    first.upsert_match(
        match_id=900, team_id=12857, opponent_id=1, opponent_name="Color",
        event_name="GLuck", start_utc=start, url="https://www.hltv.org/matches/900/x",
        snapshot={}, snapshot_hash="h")
    first.link_match_team(900, 12857)
    first.conn.execute(
        "INSERT INTO sent_events (idempotency_key, event_type, match_id, created_utc) "
        "VALUES (?, 'E10', 900, ?)", (f"{chat}|E10:900:remind:15", iso(utcnow())))
    first.set_meta("e10_keys_have_start", "")     # the flag the old version lacked
    first.close()

    # The new code opens it and rewrites the key on the way in.
    storage = Storage(path)
    keys = [row["idempotency_key"] for row in
            storage.conn.execute("SELECT idempotency_key FROM sent_events")]
    assert keys == [f"{chat}|E10:900:{iso(start)}:remind:15"]

    # And therefore says nothing about a start it has already announced.
    config = Config(chat_id=chat, bot_token="t")
    notifier = Notifier(storage, config, telegram=None)
    for event in ReminderScheduler(storage, config).due():
        notifier.enqueue(event)
    assert storage.pending_count() == 0
    storage.close()


# ---------- the half and the overtime became two types ----------

def test_the_overtime_alert_keeps_its_key_across_the_rename(tmp_path):
    """E13's key carries the type, so the journal has to be rewritten.

    Without it the first run after the upgrade finds nothing matching `E13:`
    and announces an overtime it has already announced — the same mistake as
    the reminder keys before it.
    """
    from hltv_notify.state.db import Storage

    path = tmp_path / "old.db"
    storage = Storage(path)
    storage.conn.execute(
        "INSERT INTO sent_events (idempotency_key, event_type, match_id, created_utc) "
        "VALUES ('555|E12:900:map:1:overtime:1', 'E12', 900, '2026-08-01T00:00:00+00:00')")
    storage.conn.execute(
        "INSERT INTO sent_events (idempotency_key, event_type, match_id, created_utc) "
        "VALUES ('555|E12:900:map:1:half', 'E12', 900, '2026-08-01T00:00:00+00:00')")
    storage.conn.execute("DELETE FROM meta WHERE key = 'overtime_keys_are_e13'")
    storage.close()

    storage = Storage(path)
    keys = {row["idempotency_key"]: row["event_type"] for row in
            storage.conn.execute("SELECT idempotency_key, event_type FROM sent_events")}
    assert keys == {"555|E13:900:map:1:overtime:1": "E13",
                    # The half kept E12 and needed nothing.
                    "555|E12:900:map:1:half": "E12"}
    storage.close()


def test_the_overtime_rename_runs_once(tmp_path):
    from hltv_notify.state.db import Storage

    path = tmp_path / "old.db"
    storage = Storage(path)
    storage.conn.execute(
        "INSERT INTO sent_events (idempotency_key, event_type, match_id, created_utc) "
        "VALUES ('E12:900:map:1:overtime:1', 'E12', 900, '2026-08-01T00:00:00+00:00')")
    storage.conn.execute("DELETE FROM meta WHERE key = 'overtime_keys_are_e13'")
    storage.close()

    Storage(path).close()
    storage = Storage(path)
    assert storage._migrate_overtime_keys() == 0
    assert [row["idempotency_key"] for row in
            storage.conn.execute("SELECT idempotency_key FROM sent_events")] == \
        ["E13:900:map:1:overtime:1"]
    storage.close()


def test_the_phase_setting_becomes_both_of_its_halves(tmp_path):
    """Someone who had turned the pair on meant both of them.

    Dropping the row would silently switch off something they asked for.
    """
    from hltv_notify.state.db import Storage

    path = tmp_path / "old.db"
    storage = Storage(path)
    storage.conn.execute(
        "INSERT INTO subscriber_settings (chat_id, name, value) VALUES ('555', 'phase', 1)")
    storage.conn.execute("DELETE FROM meta WHERE key = 'phase_setting_split'")
    storage.close()

    storage = Storage(path)
    assert storage.setting("555", "half", 0) == 1
    assert storage.setting("555", "overtime", 0) == 1
    assert storage.conn.execute(
        "SELECT COUNT(*) FROM subscriber_settings WHERE name = 'phase'").fetchone()[0] == 0
    storage.close()


def test_a_choice_already_made_under_the_new_name_wins(tmp_path):
    """The split must not overwrite a more recent decision."""
    from hltv_notify.state.db import Storage

    path = tmp_path / "old.db"
    storage = Storage(path)
    storage.conn.execute(
        "INSERT INTO subscriber_settings (chat_id, name, value) VALUES ('555', 'phase', 1)")
    storage.conn.execute(
        "INSERT INTO subscriber_settings (chat_id, name, value) VALUES ('555', 'overtime', 0)")
    storage.conn.execute("DELETE FROM meta WHERE key = 'phase_setting_split'")
    storage.close()

    storage = Storage(path)
    assert storage.setting("555", "half", 9) == 1
    assert storage.setting("555", "overtime", 9) == 0
    storage.close()
