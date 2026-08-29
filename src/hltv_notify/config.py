"""Конфигурация: только переменные окружения, никаких секретов в коде."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List
from pathlib import Path

# Потолок частоты, зашитый в код. Конфигом не поднимается — см. docs/operations.md.
HARD_MIN_REQUEST_INTERVAL_SECONDS = 30.0

HLTV_BASE = "https://www.hltv.org"


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
    # что отслеживаем
    team_id: int = field(default_factory=lambda: _int("TEAM_ID", 12857))
    team_slug: str = field(default_factory=lambda: _str("TEAM_SLUG", "forze-reload"))
    team_name: str = field(default_factory=lambda: _str("TEAM_NAME", "FORZE Reload"))

    # Telegram
    bot_token: str = field(default_factory=lambda: _str("TELEGRAM_BOT_TOKEN", ""))
    chat_id: str = field(default_factory=lambda: _str("TELEGRAM_CHAT_ID", ""))

    # Кому разрешено пользоваться ботом. По умолчанию — только перечисленные:
    # у бота публичный адрес, и без белого списка команду ему сможет отдать
    # кто угодно, кто его найдёт.
    allowed_chats: str = field(default_factory=lambda: _str("TELEGRAM_ALLOWED_CHATS", ""))
    whitelist_only: bool = field(default_factory=lambda: _bool("TELEGRAM_WHITELIST_ONLY", True))

    # режим
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", True))
    timezone: str = field(default_factory=lambda: _str("TZ_DISPLAY", "Europe/Moscow"))
    log_level: str = field(default_factory=lambda: _str("LOG_LEVEL", "INFO"))
    db_path: Path = field(default_factory=lambda: Path(_str("DB_PATH", "data/hltv.db")))

    # частоты (секунды)
    poll_idle: int = field(default_factory=lambda: _int("POLL_IDLE_SECONDS", 1800))
    poll_prematch: int = field(default_factory=lambda: _int("POLL_PREMATCH_SECONDS", 180))
    poll_live: int = field(default_factory=lambda: _int("POLL_LIVE_SECONDS", 60))
    poll_live_with_feed: int = field(
        default_factory=lambda: _int("POLL_LIVE_WITH_FEED_SECONDS", 300))
    prematch_window_minutes: int = field(
        default_factory=lambda: _int("PREMATCH_WINDOW_MINUTES", 30))

    # живое сообщение со счётом по ходу карты
    live_message: bool = field(default_factory=lambda: _bool("LIVE_MESSAGE", True))
    live_edit_seconds: int = field(default_factory=lambda: _int("LIVE_EDIT_SECONDS", 10))

    # через сколько сообщать, что сервис ослеп. В срочных ситуациях (до старта
    # меньше минуты, три раунда до конца карты, овертайм) порог всё равно
    # минута — см. hltv_notify.watchdog.
    degraded_alert_seconds: int = field(
        default_factory=lambda: _int("DEGRADED_ALERT_SECONDS", 300))

    # пауза после 403 на живом фиде: источник просит отойти, и секунды тут
    # не помогают. На это время сервис живёт опросом страницы матча.
    live_feed_cooldown: int = field(
        default_factory=lambda: _int("LIVE_FEED_COOLDOWN_SECONDS", 600))

    # алерт о мультикилле игрока НАШЕЙ команды, чтобы успеть клипануть
    multikill_alerts: bool = field(default_factory=lambda: _bool("MULTIKILL_ALERTS", True))
    multikill_threshold: int = field(default_factory=lambda: _int("MULTIKILL_THRESHOLD", 4))

    # Напоминания перед матчем: значения по умолчанию для нового подписчика,
    # дальше он правит их сам через /remind.
    default_reminders: str = field(default_factory=lambda: _str("REMINDERS", "15"))

    # Сколько хранить историю. Журнал событий — это защита от повторной
    # рассылки, поэтому чистится куда осторожнее очереди.
    outbox_keep_days: int = field(default_factory=lambda: _int("OUTBOX_KEEP_DAYS", 90))
    events_keep_days: int = field(default_factory=lambda: _int("EVENTS_KEEP_DAYS", 365))

    # пороги событий
    e2_min_shift_minutes: int = field(default_factory=lambda: _int("E2_MIN_SHIFT_MINUTES", 5))
    e2_debounce_minutes: int = field(default_factory=lambda: _int("E2_DEBOUNCE_MINUTES", 10))
    stale_minutes: int = field(default_factory=lambda: _int("STALE_MINUTES", 15))

    # HTTP
    impersonate: str = field(default_factory=lambda: _str("HTTP_IMPERSONATE", "chrome"))
    http_retries: int = field(default_factory=lambda: _int("HTTP_RETRIES", 3))
    failures_before_alert: int = field(
        default_factory=lambda: _int("FAILURES_BEFORE_ALERT", 3))
    raw_log_days: int = field(default_factory=lambda: _int("RAW_LOG_DAYS", 7))

    @property
    def team_url(self) -> str:
        return f"{HLTV_BASE}/team/{self.team_id}/{self.team_slug}"

    def interval_for(self, mode: str) -> int:
        """Интервал опроса с учётом потолка: конфиг не может его пробить."""
        base = {
            "idle": self.poll_idle,
            "prematch": self.poll_prematch,
            "live": self.poll_live,
            "live_with_feed": self.poll_live_with_feed,
        }[mode]
        return max(base, int(HARD_MIN_REQUEST_INTERVAL_SECONDS))

    def telegram_enabled(self) -> bool:
        return bool(self.bot_token and (self.chat_id or self.allowed_chat_ids()))

    def reminder_minutes(self) -> List[int]:
        values = []
        for part in self.default_reminders.replace(";", ",").split(","):
            part = part.strip()
            if part.isdigit():
                values.append(int(part))
        return sorted(set(values), reverse=True)

    def allowed_chat_ids(self) -> List[str]:
        """Белый список из .env плюс основной чат: он разрешён всегда."""
        ids = [part.strip() for part in self.allowed_chats.replace(";", ",").split(",")]
        ids = [part for part in ids if part]
        if self.chat_id and self.chat_id not in ids:
            ids.insert(0, self.chat_id)
        return ids

    def chat_allowed(self, chat_id: str) -> bool:
        if not self.whitelist_only:
            return True
        return str(chat_id) in self.allowed_chat_ids()


def load() -> Config:
    return Config()
