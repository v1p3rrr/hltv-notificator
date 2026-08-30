"""Configuration: environment variables only, never secrets in code."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List
from pathlib import Path
from urllib.parse import urlparse

from .proxy import ProxySettings

# The request-rate ceiling, hardcoded. Not raisable by config — see
# docs/operations.md.
HARD_MIN_REQUEST_INTERVAL_SECONDS = 30.0

HLTV_BASE = "https://www.hltv.org"

# Where the service is allowed to go at all. The list is closed and lives in
# code rather than config: match addresses come FROM THE HLTV PAGE, and without
# this check a poisoned record would send the request to a foreign host — the
# local network the service runs in included. The check sits at the network
# egress itself so it also catches records written to the database before the
# parser was fixed.
ALLOWED_HOSTS = frozenset({"www.hltv.org", "hltv.org", "scorebot-lb.hltv.org"})

log = logging.getLogger(__name__)

# A Telegram chat id is a number; for groups and channels it is negative.
_CHAT_ID_RE = re.compile(r"^-?\d+$")


def url_allowed(url: str) -> bool:
    """Whether this address may be requested.

    The scheme must be https and the host must be in ALLOWED_HOSTS. What is
    compared is the `hostname` from the parsed URL, not the start of the
    string: `https://www.hltv.org@evil/` starts out "correctly" yet leads to
    evil — that part is userinfo, not the host.
    """
    try:
        parts = urlparse(url or "")
    except ValueError:
        return False
    return parts.scheme == "https" and (parts.hostname or "").lower() in ALLOWED_HOSTS


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return raw if raw not in (None, "") else default


@dataclass(frozen=True)
class Config:
    # what we follow
    team_id: int = field(default_factory=lambda: _int("TEAM_ID", 12857))
    team_slug: str = field(default_factory=lambda: _str("TEAM_SLUG", "forze-reload"))
    team_name: str = field(default_factory=lambda: _str("TEAM_NAME", "FORZE Reload"))

    # Telegram
    bot_token: str = field(default_factory=lambda: _str("TELEGRAM_BOT_TOKEN", ""))

    # Who may use the bot. ONE variable, ids separated by commas:
    #     TELEGRAM_CHAT_ID=123456789,987654321
    # The first one is the main chat: the team from TEAM_ID is seeded there on
    # the first run, and messages go there while there are no subscribers yet.
    # The list is closed by default: the bot has a public address, and without
    # it anyone who finds the bot could command it.
    chat_id: str = field(default_factory=lambda: _str("TELEGRAM_CHAT_ID", ""))
    whitelist_only: bool = field(default_factory=lambda: _bool("TELEGRAM_WHITELIST_ONLY", True))

    # mode
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", True))
    timezone: str = field(default_factory=lambda: _str("TZ_DISPLAY", "Europe/Moscow"))
    log_level: str = field(default_factory=lambda: _str("LOG_LEVEL", "INFO"))
    db_path: Path = field(default_factory=lambda: Path(_str("DB_PATH", "data/hltv.db")))

    # polling intervals (seconds)
    poll_idle: int = field(default_factory=lambda: _int("POLL_IDLE_SECONDS", 1800))
    poll_prematch: int = field(default_factory=lambda: _int("POLL_PREMATCH_SECONDS", 180))
    poll_live: int = field(default_factory=lambda: _int("POLL_LIVE_SECONDS", 60))
    poll_live_with_feed: int = field(
        default_factory=lambda: _int("POLL_LIVE_WITH_FEED_SECONDS", 300))
    prematch_window_minutes: int = field(
        default_factory=lambda: _int("PREMATCH_WINDOW_MINUTES", 30))
    # How long a match that should have started keeps the schedule on the
    # frequent cadence. That is the window in which HLTV moves it — by five
    # minutes, then ten — and the only way to catch such a move is to keep
    # looking. Deliberately not the same thing as watchdog.START_GRACE_MINUTES,
    # which answers a different question: for how long being blind around this
    # match is still urgent.
    late_start_grace_minutes: int = field(
        default_factory=lambda: _int("LATE_START_GRACE_MINUTES", 60))

    # the live score message kept up to date during a map
    live_message: bool = field(default_factory=lambda: _bool("LIVE_MESSAGE", True))
    live_edit_seconds: int = field(default_factory=lambda: _int("LIVE_EDIT_SECONDS", 10))

    # how long before we report that the service has gone blind. In urgent
    # situations (under a minute to the start, three rounds left on a map,
    # overtime) the threshold is a minute regardless — see hltv_notify.watchdog.
    degraded_alert_seconds: int = field(
        default_factory=lambda: _int("DEGRADED_ALERT_SECONDS", 300))

    # pause after a 403 on the live feed: the source is asking us to back off,
    # and seconds will not help. The service lives on match-page polling then.
    live_feed_cooldown: int = field(
        default_factory=lambda: _int("LIVE_FEED_COOLDOWN_SECONDS", 600))

    # alert on a multikill by a player of OUR team, so a highlight can be clipped
    multikill_alerts: bool = field(default_factory=lambda: _bool("MULTIKILL_ALERTS", True))
    multikill_threshold: int = field(default_factory=lambda: _int("MULTIKILL_THRESHOLD", 4))

    # alert on the half and on every new overtime. Off by default: it is one
    # more message per map for something the live card already shows.
    phase_alerts: bool = field(default_factory=lambda: _bool("PHASE_ALERTS", False))

    # Pre-match reminders: the defaults handed to a new subscriber, who then
    # edits them via /remind.
    default_reminders: str = field(default_factory=lambda: _str("REMINDERS", "15"))

    # How long history is kept. The event journal is the protection against
    # re-sending, so it is pruned far more cautiously than the queue.
    outbox_keep_days: int = field(default_factory=lambda: _int("OUTBOX_KEEP_DAYS", 90))
    events_keep_days: int = field(default_factory=lambda: _int("EVENTS_KEEP_DAYS", 365))

    # event thresholds
    e2_min_shift_minutes: int = field(default_factory=lambda: _int("E2_MIN_SHIFT_MINUTES", 5))
    e2_debounce_minutes: int = field(default_factory=lambda: _int("E2_DEBOUNCE_MINUTES", 10))
    stale_minutes: int = field(default_factory=lambda: _int("STALE_MINUTES", 15))

    # HTTP
    # The proxy comes from the standard HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/
    # NO_PROXY — we deliberately do not invent our own variables, see
    # hltv_notify.proxy.
    proxy: ProxySettings = field(default_factory=ProxySettings.from_env)
    impersonate: str = field(default_factory=lambda: _str("HTTP_IMPERSONATE", "chrome"))
    http_retries: int = field(default_factory=lambda: _int("HTTP_RETRIES", 3))
    failures_before_alert: int = field(
        default_factory=lambda: _int("FAILURES_BEFORE_ALERT", 3))
    raw_log_days: int = field(default_factory=lambda: _int("RAW_LOG_DAYS", 7))

    @property
    def team_url(self) -> str:
        return f"{HLTV_BASE}/team/{self.team_id}/{self.team_slug}"

    def proxies_for(self, url: str) -> Dict[str, str]:
        """The proxy for one address, in the shape curl_cffi expects."""
        return self.proxy.for_url(url)

    def interval_for(self, mode: str) -> int:
        """Polling interval, ceiling included: config cannot break through it."""
        base = {
            "idle": self.poll_idle,
            "prematch": self.poll_prematch,
            "live": self.poll_live,
            "live_with_feed": self.poll_live_with_feed,
        }[mode]
        return max(base, int(HARD_MIN_REQUEST_INTERVAL_SECONDS))

    def telegram_enabled(self) -> bool:
        return bool(self.bot_token and self.allowed_chat_ids())

    def reminder_minutes(self) -> List[int]:
        values = []
        for part in self.default_reminders.replace(";", ",").split(","):
            part = part.strip()
            if part.isdigit():
                values.append(int(part))
        return sorted(set(values), reverse=True)

    def allowed_chat_ids(self) -> List[str]:
        """Allowed chats in declaration order, without duplicates.

        There is a single source, `TELEGRAM_CHAT_ID`, where ids are listed
        separated by commas (semicolons and surrounding spaces are accepted
        too).
        """
        ids: List[str] = []
        for part in self.chat_id.replace(";", ",").split(","):
            part = part.strip()
            if not part or part in ids:
                continue
            if not _CHAT_ID_RE.match(part):
                # Do not fail the startup: the other ids work, and this one
                # would receive nothing anyway — Telegram is addressed by number.
                log.warning("skipping %r in the chat list: a numeric id is "
                            "required, /whoami will tell you yours", part)
                continue
            ids.append(part)
        return ids

    @property
    def main_chat_id(self) -> str:
        """The main chat: the first one in the list.

        It is also where the first team from TEAM_ID is seeded and the fallback
        recipient while there are no subscribers in the database.
        """
        ids = self.allowed_chat_ids()
        return ids[0] if ids else ""

    def chat_allowed(self, chat_id: str) -> bool:
        if not self.whitelist_only:
            return True
        return str(chat_id) in self.allowed_chat_ids()


def load() -> Config:
    return Config()
