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
-- Кому вообще шлём. Разные аккаунты Telegram ведут свои списки команд.
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id   TEXT PRIMARY KEY,
    added_utc TEXT NOT NULL,
    enabled   INTEGER NOT NULL DEFAULT 1,
    note      TEXT,
    -- Пояс отображения у каждого свой: подписчики могут жить в разных.
    timezone  TEXT,
    -- Общий тумблер «молчи»: перекрывает глушение отдельных типов.
    paused    INTEGER NOT NULL DEFAULT 0
);

-- За сколько до матча напоминать. Список свой у каждого подписчика.
CREATE TABLE IF NOT EXISTS reminders (
    chat_id        TEXT NOT NULL,
    minutes_before INTEGER NOT NULL,
    PRIMARY KEY (chat_id, minutes_before)
);

-- Команды — СВОИ у каждого подписчика: один и тот же матч может быть интересен
-- двоим, и заглушить его для одного нельзя, не заглушив для другого.
CREATE TABLE IF NOT EXISTS teams (
    chat_id      TEXT NOT NULL,
    team_id      INTEGER NOT NULL,
    slug         TEXT NOT NULL,
    name         TEXT NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    muted_events TEXT NOT NULL DEFAULT '',
    added_utc    TEXT NOT NULL,
    PRIMARY KEY (chat_id, team_id)
);

-- Какие ОТСЛЕЖИВАЕМЫЕ команды участвуют в матче. Их может быть две: если
-- отслеживаемые команды играют друг против друга, матч всё равно один, и
-- уведомления о нём должны прийти по одному разу.
CREATE TABLE IF NOT EXISTS match_teams (
    match_id INTEGER NOT NULL,
    team_id  INTEGER NOT NULL,
    PRIMARY KEY (match_id, team_id)
);

CREATE TABLE IF NOT EXISTS matches (
    match_id       INTEGER PRIMARY KEY,
    -- Каноническая перспектива: от лица какой команды считается счёт и от
    -- чьего имени пишется сообщение. Берётся детерминированно (меньший id),
    -- иначе матч двух отслеживаемых команд породил бы два зеркальных ключа
    -- идемпотентности и, значит, два уведомления вместо одного.
    team_id        INTEGER,
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
    progress_since_utc TEXT,
    -- current_map_name — поле ДЛЯ ОТОБРАЖЕНИЯ, его пишут обе машины, и «кто
    -- писал последним» там не определено. Решения по нему принимать нельзя.
    -- Ниже — приватные памятки: каждую пишет и читает ровно одна машина.
    live_map_name      TEXT,   -- последняя карта, которую видел ЖИВОЙ ФИД
    page_seen_utc      TEXT,   -- когда СТРАНИЦА МАТЧА впервые увидела матч
    regulation_rounds  INTEGER,-- формат карты: половина регламента (обычно 12)
    overtime_rounds    INTEGER -- половина овертайма (обычно 3)
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
    chat_id             TEXT NOT NULL DEFAULT '',
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

CREATE TABLE IF NOT EXISTS live_messages (
    chat_id             TEXT NOT NULL,
    match_id            INTEGER NOT NULL,
    map_number          INTEGER NOT NULL,
    telegram_message_id INTEGER,
    last_text           TEXT,
    last_edit_utc       TEXT,
    finalized           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, match_id, map_number)
);

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
        # Соединение в автокоммите, то есть каждый INSERT — отдельная
        # транзакция. С synchronous=FULL это fsync на каждую запись: в
        # контейнере первичное заполнение базы занимало 21 секунду вместо
        # полусекунды. В режиме WAL значение NORMAL безопасно относительно
        # падения процесса и теряет данные только при отключении питания —
        # для журнала уведомлений это приемлемый размен.
        self.conn.execute("PRAGMA synchronous=NORMAL")
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
                "live_map_name": "TEXT",
                "page_seen_utc": "TEXT",
                "regulation_rounds": "INTEGER",
                "overtime_rounds": "INTEGER",
            },
            "matches": {
                "team_id": "INTEGER",
            },
            "outbox": {
                "chat_id": "TEXT NOT NULL DEFAULT ''",
            },
            "subscribers": {
                "timezone": "TEXT",
                "paused": "INTEGER NOT NULL DEFAULT 0",
            },
        }
        for table, columns in added.items():
            existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for column, ddl in columns.items():
                if column not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

        # Таблицы, у которых сменился первичный ключ. ALTER TABLE такое не
        # умеет, поэтому пересоздаём. Живые сообщения — состояние одной карты,
        # потерять их не страшно: заведётся новое.
        if "chat_id" not in {row["name"] for row in
                             self.conn.execute("PRAGMA table_info(live_messages)")}:
            self.conn.execute("DROP TABLE IF EXISTS live_messages")
            self.conn.executescript(SCHEMA)

        # teams до появления подписчиков был без chat_id. Переносить некуда:
        # владельца определит первый посев из конфига.
        team_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(teams)")}
        if team_columns and "chat_id" not in team_columns:
            self.conn.execute("ALTER TABLE teams RENAME TO teams_without_owner")
            self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ---------- подписчики ----------

    def add_subscriber(self, chat_id: str, note: str = "") -> bool:
        """True — подписчик появился впервые."""
        existed = self.get_subscriber(chat_id) is not None
        self.conn.execute(
            """
            INSERT INTO subscribers (chat_id, added_utc, note) VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET enabled = 1,
                note = COALESCE(NULLIF(excluded.note, ''), subscribers.note)
            """,
            (str(chat_id), iso(utcnow()), note))
        return not existed

    def get_subscriber(self, chat_id: str):
        return self.conn.execute(
            "SELECT * FROM subscribers WHERE chat_id = ?", (str(chat_id),)).fetchone()

    def subscribers(self, *, enabled_only: bool = True) -> List[sqlite3.Row]:
        query = "SELECT * FROM subscribers"
        if enabled_only:
            query += " WHERE enabled = 1"
        return list(self.conn.execute(query + " ORDER BY added_utc"))

    def subscriber_ids(self) -> List[str]:
        return [row["chat_id"] for row in self.subscribers()]

    def set_subscriber_timezone(self, chat_id: str, timezone: Optional[str]) -> None:
        self.conn.execute("UPDATE subscribers SET timezone = ? WHERE chat_id = ?",
                          (timezone, str(chat_id)))

    def subscriber_timezone(self, chat_id: str, fallback: str) -> str:
        row = self.get_subscriber(chat_id)
        return (row["timezone"] if row and row["timezone"] else fallback)

    def set_subscriber_paused(self, chat_id: str, paused: bool) -> None:
        """Общий тумблер «молчи».

        Пока он включён, уведомления НЕ КОПЯТСЯ, а просто не создаются: смысл
        паузы в тишине, а не в том, чтобы вывалить всё разом при снятии.
        """
        self.conn.execute("UPDATE subscribers SET paused = ? WHERE chat_id = ?",
                          (1 if paused else 0, str(chat_id)))

    def subscriber_paused(self, chat_id: str) -> bool:
        row = self.get_subscriber(chat_id)
        return bool(row and row["paused"])

    # ---------- напоминания перед матчем ----------

    def add_reminder(self, chat_id: str, minutes_before: int) -> bool:
        """False — такое напоминание уже было."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO reminders (chat_id, minutes_before) VALUES (?, ?)",
            (str(chat_id), int(minutes_before)))
        return cur.rowcount > 0

    def remove_reminder(self, chat_id: str, minutes_before: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM reminders WHERE chat_id = ? AND minutes_before = ?",
            (str(chat_id), int(minutes_before)))
        return cur.rowcount > 0

    def reminders(self, chat_id: str) -> List[int]:
        return [row["minutes_before"] for row in self.conn.execute(
            "SELECT minutes_before FROM reminders WHERE chat_id = ? "
            "ORDER BY minutes_before DESC", (str(chat_id),))]

    def set_subscriber_enabled(self, chat_id: str, enabled: bool) -> bool:
        cur = self.conn.execute("UPDATE subscribers SET enabled = ? WHERE chat_id = ?",
                                (1 if enabled else 0, str(chat_id)))
        return cur.rowcount > 0

    # ---------- отслеживаемые команды ----------

    def add_team(self, chat_id: str, team_id: int, slug: str, name: str) -> bool:
        """True — команда добавлена этому подписчику впервые."""
        existed = self.get_team(chat_id, team_id) is not None
        self.conn.execute(
            """
            INSERT INTO teams (chat_id, team_id, slug, name, added_utc)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, team_id) DO UPDATE SET
                slug = excluded.slug, name = excluded.name, enabled = 1
            """,
            (str(chat_id), team_id, slug, name, iso(utcnow())))
        return not existed

    def get_team(self, chat_id: str, team_id: Optional[int]):
        if team_id is None:
            return None
        return self.conn.execute(
            "SELECT * FROM teams WHERE chat_id = ? AND team_id = ?",
            (str(chat_id), team_id)).fetchone()

    def teams(self, chat_id: Optional[str] = None, *,
              enabled_only: bool = True) -> List[sqlite3.Row]:
        """Команды подписчика, а без chat_id — все записи всех подписчиков."""
        clauses = []
        params: List[Any] = []
        if chat_id is not None:
            clauses.append("chat_id = ?")
            params.append(str(chat_id))
        if enabled_only:
            clauses.append("enabled = 1")
        query = "SELECT * FROM teams"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        return list(self.conn.execute(query + " ORDER BY team_id", params))

    def tracked_teams(self) -> List[sqlite3.Row]:
        """Уникальные команды по всем подписчикам — их и надо опрашивать.

        Одну и ту же команду могут отслеживать несколько человек, а страница
        у неё одна: опрашивать её по разу на подписчика значило бы дёргать
        источник вхолостую.
        """
        return list(self.conn.execute(
            "SELECT team_id, MIN(slug) AS slug, MIN(name) AS name FROM teams "
            "WHERE enabled = 1 GROUP BY team_id ORDER BY team_id"))

    def team_ids(self) -> List[int]:
        return [row["team_id"] for row in self.tracked_teams()]

    def subscribers_tracking(self, team_id: int) -> List[str]:
        """Кому интересна эта команда. Только включённые подписчики."""
        return [row["chat_id"] for row in self.conn.execute(
            "SELECT t.chat_id FROM teams t "
            "JOIN subscribers s ON s.chat_id = t.chat_id "
            "WHERE t.team_id = ? AND t.enabled = 1 AND s.enabled = 1",
            (team_id,))]

    def set_team_enabled(self, chat_id: str, team_id: int, enabled: bool) -> bool:
        cur = self.conn.execute(
            "UPDATE teams SET enabled = ? WHERE chat_id = ? AND team_id = ?",
            (1 if enabled else 0, str(chat_id), team_id))
        return cur.rowcount > 0

    def set_team_mutes(self, chat_id: str, team_id: int, muted: List[str]) -> None:
        self.conn.execute(
            "UPDATE teams SET muted_events = ? WHERE chat_id = ? AND team_id = ?",
            (",".join(sorted(set(muted))), str(chat_id), team_id))

    def team_mutes(self, chat_id: str, team_id: int) -> List[str]:
        row = self.get_team(chat_id, team_id)
        if row is None or not row["muted_events"]:
            return []
        return [part for part in row["muted_events"].split(",") if part]

    def team_name(self, team_id: Optional[int], fallback: str = "") -> str:
        """Имя команды — общее, не зависит от подписчика."""
        if team_id is None:
            return fallback
        row = self.conn.execute(
            "SELECT name FROM teams WHERE team_id = ? LIMIT 1", (team_id,)).fetchone()
        return row["name"] if row else fallback

    # ---------- связь матча с отслеживаемыми командами ----------

    def link_match_team(self, match_id: int, team_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO match_teams (match_id, team_id) VALUES (?, ?)",
            (match_id, team_id))

    def match_team_ids(self, match_id: int) -> List[int]:
        return [row["team_id"] for row in self.conn.execute(
            "SELECT team_id FROM match_teams WHERE match_id = ? ORDER BY team_id",
            (match_id,))]

    def canonical_team(self, match_id: int) -> Optional[int]:
        """Команда, от лица которой описывается матч.

        Меньший id из отслеживаемых участников. Выбор произволен, но обязан быть
        ДЕТЕРМИНИРОВАННЫМ: от него зависит ориентация счёта, а значит и ключ
        идемпотентности. Без этого матч двух отслеживаемых команд дал бы два
        зеркальных ключа и два уведомления об одном и том же.
        """
        ids = self.match_team_ids(match_id)
        return ids[0] if ids else None

    # ---------- матчи ----------

    def get_match(self, match_id: int) -> Optional[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM matches WHERE match_id = ?", (match_id,))
        return cur.fetchone()

    def all_matches(self) -> List[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM matches ORDER BY start_utc"))

    def tracked_match_ids(self, team_id: Optional[int] = None) -> List[int]:
        """Матчи, которые сервис считает актуальными: ещё не пропавшие.

        С team_id — только матчи этой команды. Это обязательно: матч команды А
        не должен считаться исчезнувшим лишь потому, что его нет на странице
        команды Б.
        """
        if team_id is None:
            return [row["match_id"] for row in self.conn.execute(
                "SELECT match_id FROM matches WHERE missing_since_utc IS NULL")]
        return [row["match_id"] for row in self.conn.execute(
            "SELECT m.match_id FROM matches m "
            "JOIN match_teams t ON t.match_id = m.match_id "
            "WHERE m.missing_since_utc IS NULL AND t.team_id = ?", (team_id,))]

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
                     snapshot_hash: str, match_format: Optional[str] = None,
                     team_id: Optional[int] = None) -> None:
        now = iso(utcnow())
        self.conn.execute(
            """
            INSERT INTO matches (match_id, team_id, opponent_id, opponent_name, event_name,
                                 match_format, start_utc, url, snapshot, snapshot_hash,
                                 first_seen_utc, updated_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(match_id) DO UPDATE SET
                -- Порядок именно такой: уже выбранная перспектива НЕ меняется.
                -- Если бы новая перезаписывала старую, добавление второй
                -- отслеживаемой команды перевернуло бы счёт у идущего матча,
                -- ключи идемпотентности стали бы зеркальными и всё уже
                -- отправленное разослалось бы заново.
                team_id       = COALESCE(matches.team_id, excluded.team_id),
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
            (match_id, team_id, opponent_id, opponent_name, event_name, match_format,
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

    def set_map_format(self, match_id: int, regulation: int, overtime: int) -> None:
        """Формат карты из источника. Нужен, чтобы понять «сколько раундов до
        победы» — от этого зависит срочность алертов о деградации."""
        self.conn.execute(
            "UPDATE match_state SET regulation_rounds = ?, overtime_rounds = ? "
            "WHERE match_id = ?", (regulation, overtime, match_id))

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
                     match_id: Optional[int], body: str, chat_id: str = "") -> bool:
        """Одной транзакцией: журнал + очередь. False — событие уже отправлялось.

        Ключ журнала включает адресата: одно и то же событие может касаться
        нескольких подписчиков, и каждому оно должно уйти ровно один раз.
        """
        now = iso(utcnow())
        idempotency_key = f"{chat_id}|{idempotency_key}" if chat_id else idempotency_key
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                "INSERT INTO sent_events (idempotency_key, event_type, match_id, created_utc) "
                "VALUES (?, ?, ?, ?)",
                (idempotency_key, event_type, match_id, now),
            )
            self.conn.execute(
                "INSERT INTO outbox (chat_id, idempotency_key, body, next_attempt_utc, "
                "created_utc) VALUES (?, ?, ?, ?, ?)",
                (chat_id, idempotency_key, body, now, now),
            )
        except sqlite3.IntegrityError:
            # Ключ уже есть — событие отправлялось. Это штатный исход.
            self.conn.execute("ROLLBACK")
            return False
        except Exception:
            # Любой другой сбой (диск, блокировка) обязан закрыть транзакцию.
            # Соединение в автокоммите живёт всё время работы сервиса: незакрытая
            # транзакция сломала бы КАЖДУЮ следующую отправку до перезапуска.
            self.conn.execute("ROLLBACK")
            raise
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

    def oldest_pending_utc(self) -> Optional[str]:
        """Когда создано старейшее неотправленное сообщение.

        Если оно висит долго — значит Telegram не принимает, и об этом надо
        сказать: молчащая доставка выглядит точно так же, как отсутствие
        событий.
        """
        row = self.conn.execute(
            "SELECT MIN(created_utc) AS oldest FROM outbox WHERE status = 'pending'"
        ).fetchone()
        return row["oldest"] if row and row["oldest"] else None

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

    def set_live_map(self, match_id: int, map_name: str) -> None:
        """Карта, которую видит живой фид. Пишет только живая машина.

        Отдельно от current_map_name намеренно: то поле пишут обе машины, и
        страница матча кладёт туда ПРЕДСТОЯЩУЮ карту (первую несыгранную).
        Живая машина, читая его как «предыдущую карту», не видела перехода и
        не рождала E5 вовсе.
        """
        self.conn.execute(
            "UPDATE match_state SET live_map_name = ? WHERE match_id = ?",
            (map_name, match_id))

    def mark_page_seen(self, match_id: int) -> None:
        """Отметка «страница матча этот матч уже наблюдала». Ставится один раз.

        Раньше это выводилось из last_source, но живой фид переписывает его на
        каждом кадре, и признак был вечно истинным: E6 со страницы уходил в
        молчаливую ветку всё время, пока фид на связи.
        """
        self.conn.execute(
            "UPDATE match_state SET page_seen_utc = ? "
            "WHERE match_id = ? AND page_seen_utc IS NULL",
            (iso(utcnow()), match_id))

    # ---------- живое сообщение на карту ----------

    def live_message(self, chat_id: str, match_id: int,
                     map_number: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM live_messages WHERE chat_id = ? AND match_id = ? "
            "AND map_number = ?", (str(chat_id), match_id, map_number)).fetchone()

    def save_live_message(self, chat_id: str, match_id: int, map_number: int, *,
                          telegram_message_id: Optional[int], text: str,
                          finalized: bool = False) -> None:
        """Id сообщения переживает рестарт: иначе после перезапуска сервис
        завёл бы на ту же карту второе живое сообщение."""
        self.conn.execute(
            """
            INSERT INTO live_messages (chat_id, match_id, map_number, telegram_message_id,
                                       last_text, last_edit_utc, finalized)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, match_id, map_number) DO UPDATE SET
                telegram_message_id = COALESCE(excluded.telegram_message_id,
                                               live_messages.telegram_message_id),
                last_text     = excluded.last_text,
                last_edit_utc = excluded.last_edit_utc,
                finalized     = excluded.finalized
            """,
            (str(chat_id), match_id, map_number, telegram_message_id, text, iso(utcnow()),
             1 if finalized else 0))

    # ---------- состав карт ----------

    def set_map_lineup(self, match_id: int, names: List[str]) -> None:
        """Порядок карт со страницы матча: по нему живой фид узнаёт НОМЕР
        карты в серии. Сам фид присылает только её название."""
        self.set_meta(f"maps:{match_id}", json.dumps(names, ensure_ascii=False))

    def map_lineup(self, match_id: int) -> List[str]:
        raw = self.get_meta(f"maps:{match_id}")
        if not raw:
            return []
        try:
            return list(json.loads(raw))
        except (ValueError, TypeError):
            return []

    # ---------- meta ----------

    def adopt_legacy_event_keys(self, chat_id: str) -> int:
        """Приписать адресата ключам, записанным до появления подписчиков.

        Ключ журнала стал включать чат (`<chat>|<ключ>`). Старые записи без
        префикса перестали бы совпадать, и при первом же запуске после
        обновления сервис счёл бы новым всё, что уже отправлял, — то есть
        разослал бы историю заново. Делается один раз, под флагом в meta.
        """
        if not chat_id or self.get_meta("legacy_keys_adopted"):
            return 0
        cur = self.conn.execute(
            "UPDATE sent_events SET idempotency_key = ? || '|' || idempotency_key "
            "WHERE instr(idempotency_key, '|') = 0", (str(chat_id),))
        self.conn.execute(
            "UPDATE outbox SET idempotency_key = ? || '|' || idempotency_key, "
            "chat_id = ? WHERE instr(idempotency_key, '|') = 0 AND chat_id = ''",
            (str(chat_id), str(chat_id)))
        self.set_meta("legacy_keys_adopted", iso(utcnow()))
        return cur.rowcount

    def prune(self, *, sent_days: int = 90, events_days: int = 365) -> None:
        """Убрать то, что уже никому не нужно.

        Отправленные строки очереди — просто мусор. Журнал событий трогаем
        осторожнее и только по давним матчам: он и есть защита от повторной
        рассылки, и удалить его слишком рано значит разослать всё заново.
        """
        self.conn.execute(
            "DELETE FROM outbox WHERE status = 'sent' AND sent_utc < ?",
            (iso(utcnow() - timedelta(days=sent_days)),))
        self.conn.execute(
            "DELETE FROM sent_events WHERE created_utc < ? AND (match_id IS NULL OR "
            "match_id IN (SELECT match_id FROM matches WHERE start_utc < ?))",
            (iso(utcnow() - timedelta(days=events_days)),
             iso(utcnow() - timedelta(days=events_days))))
        self.conn.execute(
            "DELETE FROM live_messages WHERE match_id IN "
            "(SELECT match_id FROM matches WHERE start_utc < ?)",
            (iso(utcnow() - timedelta(days=sent_days)),))

    def get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
