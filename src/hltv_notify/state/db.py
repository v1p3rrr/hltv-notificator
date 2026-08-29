"""SQLite: состояние, журнал отправленных событий и очередь исходящих.

Ядро защиты от дублей — уникальный индекс на sent_events.idempotency_key.
Проверка «есть ли уже в базе» отдельным запросом была бы гонкой, вставка —
нет. Запись события и постановка сообщения в очередь идут одной транзакцией,
иначе падение между ними потеряло бы уведомление.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
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

CREATE TABLE IF NOT EXISTS match_state (
    match_id           INTEGER PRIMARY KEY REFERENCES matches(match_id),
    state              TEXT NOT NULL,
    current_map_number INTEGER,
    current_map_name   TEXT,
    current_map_score  TEXT,
    series_score       TEXT,
    last_seen_utc      TEXT NOT NULL,
    last_source        TEXT NOT NULL,
    pending_start_utc  TEXT,
    pending_since_utc  TEXT,
    progress_hash      TEXT,
    progress_since_utc TEXT
);

CREATE TABLE IF NOT EXISTS map_results (
    match_id       INTEGER NOT NULL,
    map_number     INTEGER NOT NULL,
    map_name       TEXT NOT NULL,
    score_team     INTEGER NOT NULL,
    score_opponent INTEGER NOT NULL,
    overtime       INTEGER NOT NULL DEFAULT 0,
    recorded_utc   TEXT NOT NULL,
    PRIMARY KEY (match_id, map_number)
);

CREATE TABLE IF NOT EXISTS sent_events (
    idempotency_key TEXT PRIMARY KEY,
    event_type      TEXT NOT NULL,
    match_id        INTEGER,
    created_utc     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
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
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(status, next_attempt_utc);

CREATE TABLE IF NOT EXISTS raw_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    source TEXT NOT NULL,
    url    TEXT,
    status TEXT,
    body   TEXT
);
CREATE INDEX IF NOT EXISTS raw_log_ts ON raw_log(ts_utc);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Storage:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Колонки, добавленные позже: CREATE TABLE IF NOT EXISTS их не
        дотянет на уже существующей базе, а терять базу нельзя — в ней журнал
        отправленных событий."""
        added = {
            "match_state": {
                "progress_hash": "TEXT",
                "progress_since_utc": "TEXT",
            },
        }
        for table, columns in added.items():
            existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for column, ddl in columns.items():
                if column not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def close(self) -> None:
        self.conn.close()

    # ---------- матчи ----------

    def get_match(self, match_id: int) -> Optional[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM matches WHERE match_id = ?", (match_id,))
        return cur.fetchone()

    def all_matches(self) -> List[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM matches ORDER BY start_utc"))

    def tracked_match_ids(self) -> List[int]:
        """Матчи, которые сервис считает актуальными: ещё не пропавшие."""
        return [row["match_id"] for row in self.conn.execute(
            "SELECT match_id FROM matches WHERE missing_since_utc IS NULL")]

    def upcoming_matches(self, now: Optional[datetime] = None) -> List[sqlite3.Row]:
        now = now or utcnow()
        return list(self.conn.execute(
            "SELECT m.*, s.state AS state FROM matches m "
            "LEFT JOIN match_state s ON s.match_id = m.match_id "
            "WHERE m.start_utc >= ? AND m.missing_since_utc IS NULL "
            "ORDER BY m.start_utc",
            (iso(now),),
        ))

    def upsert_match(self, *, match_id: int, opponent_id: Optional[int], opponent_name: str,
                     event_name: str, start_utc: datetime, url: str, snapshot: Dict[str, Any],
                     snapshot_hash: str, match_format: Optional[str] = None) -> None:
        now = iso(utcnow())
        self.conn.execute(
            """
            INSERT INTO matches (match_id, opponent_id, opponent_name, event_name, match_format,
                                 start_utc, url, snapshot, snapshot_hash, first_seen_utc, updated_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(match_id) DO UPDATE SET
                opponent_id   = excluded.opponent_id,
                opponent_name = excluded.opponent_name,
                event_name    = excluded.event_name,
                match_format  = COALESCE(excluded.match_format, matches.match_format),
                start_utc     = excluded.start_utc,
                url           = excluded.url,
                snapshot      = excluded.snapshot,
                snapshot_hash = excluded.snapshot_hash,
                updated_utc   = excluded.updated_utc,
                missing_since_utc = NULL
            """,
            (match_id, opponent_id, opponent_name, event_name, match_format,
             iso(start_utc), url, json.dumps(snapshot, ensure_ascii=False), snapshot_hash,
             now, now),
        )

    def mark_missing(self, match_id: int, when: Optional[datetime] = None) -> None:
        self.conn.execute(
            "UPDATE matches SET missing_since_utc = ? "
            "WHERE match_id = ? AND missing_since_utc IS NULL",
            (iso(when or utcnow()), match_id),
        )

    # ---------- состояние ----------

    def get_state(self, match_id: int) -> Optional[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM match_state WHERE match_id = ?", (match_id,))
        return cur.fetchone()

    def set_state(self, match_id: int, state: str, *, source: str,
                  current_map_number: Optional[int] = None,
                  current_map_name: Optional[str] = None,
                  current_map_score: Optional[str] = None,
                  series_score: Optional[str] = None) -> None:
        self.conn.execute(
            """
            INSERT INTO match_state (match_id, state, current_map_number, current_map_name,
                                     current_map_score, series_score, last_seen_utc, last_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(match_id) DO UPDATE SET
                state = excluded.state,
                current_map_number = COALESCE(excluded.current_map_number, match_state.current_map_number),
                current_map_name   = COALESCE(excluded.current_map_name, match_state.current_map_name),
                current_map_score  = COALESCE(excluded.current_map_score, match_state.current_map_score),
                series_score       = COALESCE(excluded.series_score, match_state.series_score),
                last_seen_utc = excluded.last_seen_utc,
                last_source   = excluded.last_source
            """,
            (match_id, state, current_map_number, current_map_name, current_map_score,
             series_score, iso(utcnow()), source),
        )

    def set_pending_start(self, match_id: int, start: Optional[datetime],
                          since: Optional[datetime]) -> None:
        """Кандидат на новое время начала.

        E2 не шлётся сразу: серия правок за короткое окно схлопывается, а
        возврат к исходному времени внутри окна событием не считается.
        """
        self.conn.execute(
            "UPDATE match_state SET pending_start_utc = ?, pending_since_utc = ? "
            "WHERE match_id = ?",
            (iso(start) if start else None, iso(since) if since else None, match_id),
        )

    def set_progress(self, match_id: int, signature: str, since: datetime) -> None:
        """Отпечаток продвижения матча и момент, когда он последний раз менялся.
        По нему отличается «матч на технической паузе» от «сервис ослеп»."""
        self.conn.execute(
            "UPDATE match_state SET progress_hash = ?, progress_since_utc = ? WHERE match_id = ?",
            (signature, iso(since), match_id),
        )

    def record_map_result(self, *, match_id: int, map_number: int, map_name: str,
                          score_team: int, score_opponent: int, overtime: bool) -> None:
        self.conn.execute(
            """
            INSERT INTO map_results (match_id, map_number, map_name, score_team,
                                     score_opponent, overtime, recorded_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(match_id, map_number) DO UPDATE SET
                map_name = excluded.map_name,
                score_team = excluded.score_team,
                score_opponent = excluded.score_opponent,
                overtime = excluded.overtime
            """,
            (match_id, map_number, map_name, score_team, score_opponent,
             1 if overtime else 0, iso(utcnow())),
        )

    def map_results(self, match_id: int) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM map_results WHERE match_id = ? ORDER BY map_number", (match_id,)))

    def active_matches(self, now: Optional[datetime] = None, *,
                       lookahead_minutes: int = 30,
                       lookbehind_hours: int = 12) -> List[sqlite3.Row]:
        """Матчи, которые имеет смысл опрашивать постранично.

        Окно назад нужно, потому что матч запросто начинается позже расписания;
        окно вперёд — чтобы поймать фактический старт. Круглосуточно активный
        опрос не ведётся: команда может не играть неделями.
        """
        now = now or utcnow()
        return list(self.conn.execute(
            "SELECT m.*, s.state AS state FROM matches m "
            "LEFT JOIN match_state s ON s.match_id = m.match_id "
            "WHERE m.missing_since_utc IS NULL "
            "  AND (s.state IS NULL OR s.state NOT IN ('FINISHED', 'CANCELLED')) "
            "  AND m.start_utc <= ? AND m.start_utc >= ? "
            "ORDER BY m.start_utc",
            (iso(now + timedelta(minutes=lookahead_minutes)),
             iso(now - timedelta(hours=lookbehind_hours))),
        ))

    # ---------- события и очередь ----------

    def record_event(self, *, idempotency_key: str, event_type: str,
                     match_id: Optional[int], body: str) -> bool:
        """Одной транзакцией: журнал + очередь. False — событие уже отправлялось."""
        now = iso(utcnow())
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                "INSERT INTO sent_events (idempotency_key, event_type, match_id, created_utc) "
                "VALUES (?, ?, ?, ?)",
                (idempotency_key, event_type, match_id, now),
            )
            self.conn.execute(
                "INSERT INTO outbox (idempotency_key, body, next_attempt_utc, created_utc) "
                "VALUES (?, ?, ?, ?)",
                (idempotency_key, body, now, now),
            )
        except sqlite3.IntegrityError:
            self.conn.execute("ROLLBACK")
            return False
        self.conn.execute("COMMIT")
        return True

    def due_outbox(self, limit: int = 10) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM outbox WHERE status = 'pending' AND next_attempt_utc <= ? "
            "ORDER BY id LIMIT ?",
            (iso(utcnow()), limit),
        ))

    def mark_sent(self, outbox_id: int, telegram_message_id: Optional[int]) -> None:
        self.conn.execute(
            "UPDATE outbox SET status = 'sent', sent_utc = ?, telegram_message_id = ? "
            "WHERE id = ?",
            (iso(utcnow()), telegram_message_id, outbox_id),
        )

    def mark_retry(self, outbox_id: int, attempts: int, delay_seconds: float) -> None:
        self.conn.execute(
            "UPDATE outbox SET attempts = ?, next_attempt_utc = ? WHERE id = ?",
            (attempts, iso(utcnow() + timedelta(seconds=delay_seconds)), outbox_id),
        )

    def pending_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE status = 'pending'").fetchone()[0]

    def sent_event_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM sent_events").fetchone()[0]

    # ---------- сырые ответы ----------

    def log_raw(self, source: str, url: str, status: str, body: str, keep_days: int) -> None:
        self.conn.execute(
            "INSERT INTO raw_log (ts_utc, source, url, status, body) VALUES (?, ?, ?, ?, ?)",
            (iso(utcnow()), source, url, status, body),
        )
        self.conn.execute(
            "DELETE FROM raw_log WHERE ts_utc < ?",
            (iso(utcnow() - timedelta(days=keep_days)),),
        )

    # ---------- meta ----------

    def get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
