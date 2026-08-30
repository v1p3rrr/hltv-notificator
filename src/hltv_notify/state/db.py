"""SQLite: state, the journal of sent events and the outgoing queue.

The core of the anti-duplicate protection is the unique index on
sent_events.idempotency_key. Checking "is it already in the database" with a
separate query would be a race; an insert is not. Recording the event and
queueing the message happen in one transaction, otherwise a crash between them
would lose the notification.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = """
-- Who we send to at all. Different Telegram accounts keep their own team lists.
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id   TEXT PRIMARY KEY,
    added_utc TEXT NOT NULL,
    enabled   INTEGER NOT NULL DEFAULT 1,
    note      TEXT,
    -- The display timezone is per person: subscribers may live in different ones.
    timezone  TEXT,
    -- The global "be quiet" switch: it overrides per-type muting.
    paused    INTEGER NOT NULL DEFAULT 0
);

-- How long before a match to remind. The list is per subscriber.
CREATE TABLE IF NOT EXISTS reminders (
    chat_id        TEXT NOT NULL,
    minutes_before INTEGER NOT NULL,
    PRIMARY KEY (chat_id, minutes_before)
);

-- Teams are PER SUBSCRIBER: the same match can interest two people, and muting
-- it for one must not mute it for the other.
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

-- Which TRACKED teams take part in a match. There can be two: if tracked teams
-- play each other the match is still one match, and notifications about it
-- must arrive once each.
CREATE TABLE IF NOT EXISTS match_teams (
    match_id INTEGER NOT NULL,
    team_id  INTEGER NOT NULL,
    PRIMARY KEY (match_id, team_id)
);

CREATE TABLE IF NOT EXISTS matches (
    match_id       INTEGER PRIMARY KEY,
    -- The canonical perspective: which team the score is counted from and in
    -- whose name the message is written. Chosen deterministically (the team
    -- that saw the match first), otherwise a match between two tracked teams
    -- would produce two mirrored idempotency keys and therefore two
    -- notifications instead of one.
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
    -- current_map_name is a DISPLAY field, written by both machines, and "who
    -- wrote last" is undefined there. Decisions must not be made from it.
    -- Below are private memos: each is written and read by exactly one machine.
    live_map_name      TEXT,   -- the last map the LIVE FEED saw
    live_round_state   TEXT,   -- the phase the LIVE FEED reports (warmup/started/...)
    live_frame_utc     TEXT,   -- when that phase was last seen: it goes stale
    page_seen_utc      TEXT,   -- when the MATCH PAGE first saw this match
    regulation_rounds  INTEGER,-- map format: half of regulation (usually 12)
    overtime_rounds    INTEGER,-- half of an overtime (usually 3)
    best_of            INTEGER -- series format, so the feed can tell the match is over
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
        # The connection is in autocommit, so every INSERT is its own
        # transaction. With synchronous=FULL that is an fsync per write: in a
        # container the initial fill of the database took 21 seconds instead of
        # half a second. In WAL mode the value NORMAL is safe against a process
        # crash and only loses data on a power cut — an acceptable trade for a
        # notification journal.
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Columns added later: CREATE TABLE IF NOT EXISTS will not reach them
        on an existing database, and the database must not be lost — it holds
        the journal of sent events."""
        added = {
            "match_state": {
                "progress_hash": "TEXT",
                "progress_since_utc": "TEXT",
                "live_map_name": "TEXT",
                "page_seen_utc": "TEXT",
                "regulation_rounds": "INTEGER",
                "overtime_rounds": "INTEGER",
                "best_of": "INTEGER",
                "live_round_state": "TEXT",
                "live_frame_utc": "TEXT",
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

        # Tables whose primary key changed. ALTER TABLE cannot do that, so we
        # recreate them. Live messages are one map's state; losing them is not
        # a problem, a new one will be created.
        if "chat_id" not in {row["name"] for row in
                             self.conn.execute("PRAGMA table_info(live_messages)")}:
            self.conn.execute("DROP TABLE IF EXISTS live_messages")
            self.conn.executescript(SCHEMA)

        # Before subscribers existed, `teams` had no chat_id. There is nowhere
        # to migrate it to: the owner is decided by the first seed from config.
        team_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(teams)")}
        if team_columns and "chat_id" not in team_columns:
            self.conn.execute("ALTER TABLE teams RENAME TO teams_without_owner")
            self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ---------- subscribers ----------

    def add_subscriber(self, chat_id: str, note: str = "") -> bool:
        """True means the subscriber appeared for the first time."""
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
        """The global "be quiet" switch.

        While it is on, notifications are NOT ACCUMULATED, they are simply not
        created: the point of the pause is silence, not dumping everything at
        once when it is lifted.
        """
        self.conn.execute("UPDATE subscribers SET paused = ? WHERE chat_id = ?",
                          (1 if paused else 0, str(chat_id)))

    def subscriber_paused(self, chat_id: str) -> bool:
        row = self.get_subscriber(chat_id)
        return bool(row and row["paused"])

    # ---------- pre-match reminders ----------

    def add_reminder(self, chat_id: str, minutes_before: int) -> bool:
        """False means that reminder already existed."""
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

    # ---------- tracked teams ----------

    def add_team(self, chat_id: str, team_id: int, slug: str, name: str) -> bool:
        """True means the team was added for this subscriber for the first time."""
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
        """A subscriber's teams, or without chat_id every row of everyone."""
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
        """Unique teams across all subscribers — those are the ones to poll.

        Several people may follow the same team, and it has one page: polling
        it once per subscriber would mean pestering the source for nothing.
        """
        return list(self.conn.execute(
            "SELECT team_id, MIN(slug) AS slug, MIN(name) AS name FROM teams "
            "WHERE enabled = 1 GROUP BY team_id ORDER BY team_id"))

    def team_ids(self) -> List[int]:
        return [row["team_id"] for row in self.tracked_teams()]

    def subscribers_tracking(self, team_id: int) -> List[str]:
        """Who cares about this team. Enabled subscribers only."""
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
        """The team name is shared; it does not depend on the subscriber."""
        if team_id is None:
            return fallback
        row = self.conn.execute(
            "SELECT name FROM teams WHERE team_id = ? LIMIT 1", (team_id,)).fetchone()
        return row["name"] if row else fallback

    # ---------- linking a match to tracked teams ----------

    def link_match_team(self, match_id: int, team_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO match_teams (match_id, team_id) VALUES (?, ?)",
            (match_id, team_id))

    def match_team_ids(self, match_id: int) -> List[int]:
        return [row["team_id"] for row in self.conn.execute(
            "SELECT team_id FROM match_teams WHERE match_id = ? ORDER BY team_id",
            (match_id,))]

    def canonical_team(self, match_id: int) -> Optional[int]:
        """The team the match is described from.

        Taken from what is REMEMBERED in `matches.team_id` — the team that saw
        the match first. The choice is arbitrary, but it has to be not only
        deterministic but also unchanging: the orientation of the score, and
        therefore the idempotency key, depends on it.

        This used to return simply the lower id among the participants, and it
        flipped out of nowhere: add a team with a lower id through the bot
        mid-match, and the scores of already played maps swapped over while the
        next messages about the same match contradicted the previous ones. The
        `COALESCE` guard in `upsert_match` was protecting a column nobody read;
        now it is read.

        The lower id remains the fallback answer: for matches created before
        the column existed, and for those that entered the database outside the
        schedule path.
        """
        ids = self.match_team_ids(match_id)
        row = self.conn.execute(
            "SELECT team_id FROM matches WHERE match_id = ?", (match_id,)).fetchone()
        chosen = row["team_id"] if row else None
        if chosen is not None and (not ids or chosen in ids):
            return chosen
        return ids[0] if ids else None

    # ---------- matches ----------

    def get_match(self, match_id: int) -> Optional[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM matches WHERE match_id = ?", (match_id,))
        return cur.fetchone()

    def all_matches(self) -> List[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM matches ORDER BY start_utc"))

    def tracked_match_ids(self, team_id: Optional[int] = None) -> List[int]:
        """Matches the service still considers current: not yet vanished.

        With team_id, only that team's matches. This is essential: team A's
        match must not be counted as vanished merely because it is not on team
        B's page.
        """
        if team_id is None:
            return [row["match_id"] for row in self.conn.execute(
                "SELECT match_id FROM matches WHERE missing_since_utc IS NULL")]
        return [row["match_id"] for row in self.conn.execute(
            "SELECT m.match_id FROM matches m "
            "JOIN match_teams t ON t.match_id = m.match_id "
            "WHERE m.missing_since_utc IS NULL AND t.team_id = ?", (team_id,))]

    def visible_match_ids(self, chat_id: str) -> set:
        """Matches this subscriber is entitled to see.

        Needed for /next and /live: accounts keep their own team lists, and
        other people's teams' matches are noise in someone else's chat. A match
        not yet linked to any team is visible to everyone: hiding it would mean
        losing it entirely.
        """
        mine = {row["match_id"] for row in self.conn.execute(
            "SELECT DISTINCT mt.match_id FROM match_teams mt "
            "JOIN teams t ON t.team_id = mt.team_id "
            "WHERE t.chat_id = ? AND t.enabled = 1", (str(chat_id),))}
        unlinked = {row["match_id"] for row in self.conn.execute(
            "SELECT m.match_id FROM matches m "
            "LEFT JOIN match_teams mt ON mt.match_id = m.match_id "
            "WHERE mt.team_id IS NULL")}
        return mine | unlinked

    def upcoming_matches(self, now: Optional[datetime] = None) -> List[sqlite3.Row]:
        """Matches still ahead of us, by the NEWEST time known.

        While a reschedule is being debounced the confirmed time is stale by
        definition — the page already says otherwise. Judging by the confirmed
        one, a match moved forward drops out of "upcoming" at its old start,
        and with it goes everything that hangs off this query: the polling
        cadence falls to idle, the reminders stop and /next hides the match.
        That is exactly how a move from 18:00 to 18:20 was missed. So the
        pending time wins where there is one, and `confirmed_start_utc` stays
        available for whoever needs to tell the two apart.

        The alias comes FIRST in the select on purpose: `m.*` brings its own
        `start_utc` along, and when a name repeats sqlite3.Row answers with the
        first column that carries it.
        """
        now = now or utcnow()
        return list(self.conn.execute(
            "SELECT COALESCE(s.pending_start_utc, m.start_utc) AS start_utc, "
            "       m.*, s.state AS state, m.start_utc AS confirmed_start_utc "
            "FROM matches m "
            "LEFT JOIN match_state s ON s.match_id = m.match_id "
            "WHERE COALESCE(s.pending_start_utc, m.start_utc) >= ? "
            "  AND m.missing_since_utc IS NULL "
            "ORDER BY COALESCE(s.pending_start_utc, m.start_utc)",
            (iso(now),),
        ))

    def matches_awaiting_start(self, now: Optional[datetime] = None, *,
                               grace_minutes: int = 60) -> List[sqlite3.Row]:
        """Matches whose time has come and gone without them starting.

        HLTV moves a match after its own slot has arrived as a matter of
        routine: 18:00 passes, nothing happens, and at 18:03 the page says
        18:15, then 18:30. Judged only by "is the start still ahead", such a
        match is nobody's business any more — the schedule falls back to idle
        and looks at the page again half an hour later, by which time the move
        no longer matters.

        A match that really started drops out on its own: the page sets the
        state to LIVE (even when the "match started" message itself is being
        held back for the warmup). The window is bounded so a match that never
        happens does not keep the polling up forever.
        """
        now = now or utcnow()
        return list(self.conn.execute(
            "SELECT COALESCE(s.pending_start_utc, m.start_utc) AS start_utc, "
            "       m.*, s.state AS state, m.start_utc AS confirmed_start_utc "
            "FROM matches m "
            "LEFT JOIN match_state s ON s.match_id = m.match_id "
            "WHERE COALESCE(s.pending_start_utc, m.start_utc) <= ? "
            "  AND COALESCE(s.pending_start_utc, m.start_utc) >= ? "
            "  AND m.missing_since_utc IS NULL "
            "  AND (s.state IS NULL OR s.state NOT IN "
            "       ('LIVE', 'MAP_LIVE', 'MAP_BREAK', 'FINISHED', 'CANCELLED')) "
            "ORDER BY COALESCE(s.pending_start_utc, m.start_utc)",
            (iso(now), iso(now - timedelta(minutes=grace_minutes))),
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
                -- The order is exactly this: an already chosen perspective is
                -- NOT changed. If a new one overwrote the old, adding a second
                -- tracked team would flip the score of a running match, the
                -- idempotency keys would become mirrored and everything
                -- already sent would go out again.
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

    # ---------- state ----------

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
        """A candidate for a new start time.

        E2 is not sent immediately: a burst of edits within a short window
        collapses, and a return to the original time inside the window is not
        an event at all.
        """
        self.conn.execute(
            "UPDATE match_state SET pending_start_utc = ?, pending_since_utc = ? "
            "WHERE match_id = ?",
            (iso(start) if start else None, iso(since) if since else None, match_id),
        )

    def set_map_format(self, match_id: int, regulation: int, overtime: int) -> None:
        """The map format from the source. Needed to work out "how many rounds
        to a win" — the urgency of degradation alerts depends on it."""
        self.conn.execute(
            "UPDATE match_state SET regulation_rounds = ?, overtime_rounds = ? "
            "WHERE match_id = ?", (regulation, overtime, match_id))

    def set_best_of(self, match_id: int, best_of: Optional[int]) -> None:
        """The series format from the match page.

        Without it the live feed cannot tell that the match is over: it only
        ever knows the current map. With it, the last map's result and the
        "match finished" message arrive at the same moment instead of four
        minutes apart.
        """
        if not best_of:
            return
        self.conn.execute("UPDATE match_state SET best_of = ? WHERE match_id = ?",
                          (int(best_of), match_id))

    def best_of(self, match_id: int) -> Optional[int]:
        row = self.get_state(match_id)
        return row["best_of"] if row else None

    def finished_event_keys(self, match_id: int) -> List[str]:
        """The E7 keys already sent for this match, whoever they went to.

        Used to tell a first "match finished" from a correction: if the page
        later disagrees with the feed about the series score, the key differs,
        the message goes out again, and it should say it is a correction rather
        than look like a duplicate.
        """
        return [row["idempotency_key"] for row in self.conn.execute(
            "SELECT idempotency_key FROM sent_events "
            "WHERE event_type = 'E7' AND match_id = ?", (match_id,))]

    def set_progress(self, match_id: int, signature: str, since: datetime) -> None:
        """A fingerprint of the match moving forward and the moment it last
        changed. It is how "the match is on a technical pause" is told apart
        from "the service has gone blind"."""
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
        """Matches worth polling page by page.

        The window backwards is needed because a match easily starts later than
        scheduled; the window forwards is there to catch the actual start.
        Active polling is not done around the clock: a team can go weeks
        without playing.
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

    # ---------- events and the queue ----------

    def record_event(self, *, idempotency_key: str, event_type: str,
                     match_id: Optional[int], body: str, chat_id: str = "") -> bool:
        """Journal plus queue in one transaction. False means the event has
        already been sent.

        The journal key includes the recipient: one and the same event can
        concern several subscribers, and each of them must get it exactly once.
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
            # The key already exists — the event was sent. A normal outcome.
            self.conn.execute("ROLLBACK")
            return False
        except Exception:
            # Any other failure (disk, lock) is obliged to close the
            # transaction. The connection is in autocommit and lives for the
            # whole run of the service: a transaction left open would break
            # EVERY subsequent send until a restart.
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
        """When the oldest unsent message was created.

        If it has been hanging for a long time, Telegram is not accepting, and
        that has to be said: silent delivery looks exactly like an absence of
        events.
        """
        row = self.conn.execute(
            "SELECT MIN(created_utc) AS oldest FROM outbox WHERE status = 'pending'"
        ).fetchone()
        return row["oldest"] if row and row["oldest"] else None

    def is_queued(self, idempotency_key: str) -> bool:
        """Is this message still waiting in the queue.

        Used where the ORDER between two messages matters: the live card must
        not overtake the "match has started" it continues.
        """
        row = self.conn.execute(
            "SELECT 1 FROM outbox WHERE status = 'pending' AND idempotency_key LIKE ? "
            "LIMIT 1", (f"%{idempotency_key}",)).fetchone()
        return row is not None

    def pending_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE status = 'pending'").fetchone()[0]

    def sent_event_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM sent_events").fetchone()[0]

    # ---------- raw responses ----------

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
        """The map the live feed sees. Written only by the live machine.

        Deliberately separate from current_map_name: that field is written by
        both machines, and the match page puts the UPCOMING map (the first
        undecided one) there. Reading it as "the previous map", the live
        machine never saw a transition and never produced E5 at all.
        """
        self.conn.execute(
            "UPDATE match_state SET live_map_name = ? WHERE match_id = ?",
            (map_name, match_id))

    def set_live_phase(self, match_id: int, round_state: str,
                       when: Optional[datetime] = None) -> None:
        """What phase the feed reports, and when it said so. Live machine only.

        The match page cannot tell a warmup from a game — it raises its LIVE
        flag the moment the teams connect to the server, and the warmup before
        the first map can run twenty minutes. The feed says it outright, so the
        page machine reads this to decide whether "the match has started" is
        true yet.

        The timestamp is half the answer: a phase nobody has confirmed for
        minutes means the feed is not talking, and then the page must go back
        to deciding on its own rather than waiting for a warmup to end that
        nobody is watching.
        """
        self.conn.execute(
            "UPDATE match_state SET live_round_state = ?, live_frame_utc = ? "
            "WHERE match_id = ?",
            (round_state, iso(when or utcnow()), match_id))

    def start_event_sent(self, match_id: int) -> bool:
        """Has "the match has started" already gone out for this match.

        Both machines can produce E4 — the page from its LIVE flag, the feed
        from the first round actually played — with the same idempotency key,
        so the journal is the one place that knows. Asking it is what lets the
        page retry on every poll instead of relying on a state transition that
        happens exactly once.
        """
        row = self.conn.execute(
            "SELECT 1 FROM sent_events WHERE event_type = 'E4' AND match_id = ? LIMIT 1",
            (match_id,)).fetchone()
        return row is not None

    def set_pending_start_event(self, match_id: int,
                                payload: Optional[Dict[str, Any]]) -> None:
        """The "match started" message, written and waiting for the warmup to end.

        The payload is stored rather than rebuilt later on purpose: the map
        picks and the opponent's real name come from the page observation, and
        a second builder somewhere else would drift away from this one.
        """
        key = f"start_event:{match_id}"
        if payload is None:
            self.set_meta(key, "")
        else:
            self.set_meta(key, json.dumps(payload, ensure_ascii=False))

    def pending_start_event(self, match_id: int) -> Optional[Dict[str, Any]]:
        raw = self.get_meta(f"start_event:{match_id}")
        if not raw:
            return None
        try:
            return dict(json.loads(raw))
        except (ValueError, TypeError):
            return None

    def mark_page_seen(self, match_id: int) -> None:
        """A marker: "the match page has already observed this match". Set once.

        This used to be derived from last_source, but the live feed rewrites it
        on every frame, so the marker was permanently true: page-side E6 went
        down the silent branch the whole time the feed was up.
        """
        self.conn.execute(
            "UPDATE match_state SET page_seen_utc = ? "
            "WHERE match_id = ? AND page_seen_utc IS NULL",
            (iso(utcnow()), match_id))

    # ---------- the live message for one map ----------

    def live_message(self, chat_id: str, match_id: int,
                     map_number: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM live_messages WHERE chat_id = ? AND match_id = ? "
            "AND map_number = ?", (str(chat_id), match_id, map_number)).fetchone()

    def save_live_message(self, chat_id: str, match_id: int, map_number: int, *,
                          telegram_message_id: Optional[int], text: str,
                          finalized: bool = False) -> None:
        """The message id survives a restart: otherwise, after a restart, the
        service would start a second live message for the same map."""
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

    # ---------- the map lineup ----------

    def set_map_lineup(self, match_id: int, names: List[str]) -> None:
        """The order of maps from the match page: it is how the live feed
        learns a map's NUMBER in the series. The feed itself only sends the
        name."""
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
        """Prefix the recipient onto keys written before subscribers existed.

        The journal key came to include the chat (`<chat>|<key>`). Old records
        without the prefix would stop matching, and on the first run after an
        upgrade the service would consider everything it had already sent to be
        new — that is, it would send the whole history again. Done once, under
        a flag in meta.
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
        """Remove what nobody needs any more.

        Sent queue rows are simply rubbish. The event journal is treated far
        more carefully and only for long-past matches: it is the protection
        against re-sending, and deleting it too early means sending everything
        again.
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
