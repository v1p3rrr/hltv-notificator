"""Конфигурация: только переменные окружения, никаких секретов в коде."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List
from pathlib import Path

from .proxy import ProxySettings

# Потолок частоты, зашитый в код. Конфигом не поднимается — см. docs/operations.md.
HARD_MIN_REQUEST_INTERVAL_SECONDS = 30.0

HLTV_BASE = "https://www.hltv.org"

log = logging.getLogger(__name__)

# id чата в Telegram — число; у групп и каналов оно отрицательное.
_CHAT_ID_RE = re.compile(r"^-?\d+$")


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

    # Кому разрешено пользоваться ботом. ОДНА переменная, id через запятую:
    #     TELEGRAM_CHAT_ID=123456789,987654321
    # Первый в списке — основной чат: в него садится команда из TEAM_ID при
    # первом запуске и уходят сообщения, если подписчиков в базе ещё нет.
    # По умолчанию список закрытый: у бота публичный адрес, и без него команду
    # ему сможет отдать кто угодно, кто его найдёт.
    chat_id: str = field(default_factory=lambda: _str("TELEGRAM_CHAT_ID", ""))
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
    # Прокси берётся из стандартных HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY —
    # своих переменных для этого не заводим, см. hltv_notify.proxy.
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
        """Прокси для конкретного адреса — в том виде, в каком его ждёт curl_cffi."""
        return self.proxy.for_url(url)

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
        return bool(self.bot_token and self.allowed_chat_ids())

    def reminder_minutes(self) -> List[int]:
        values = []
        for part in self.default_reminders.replace(";", ",").split(","):
            part = part.strip()
            if part.isdigit():
                values.append(int(part))
        return sorted(set(values), reverse=True)

    def allowed_chat_ids(self) -> List[str]:
        """Разрешённые чаты в порядке объявления, без повторов.

        Источник один — `TELEGRAM_CHAT_ID`, где id перечисляются через запятую
        (точка с запятой и пробелы тоже принимаются).
        """
        ids: List[str] = []
        for part in self.chat_id.replace(";", ",").split(","):
            part = part.strip()
            if not part or part in ids:
                continue
            if not _CHAT_ID_RE.match(part):
                # Не роняем запуск: остальные id рабочие, а этот всё равно
                # ничего не получит — Telegram адресуется числом.
                log.warning("в списке чатов пропущено значение %r: "
                            "нужен числовой id, его подскажет /whoami", part)
                continue
            ids.append(part)
        return ids

    @property
    def main_chat_id(self) -> str:
        """Основной чат: первый в списке.

        Он же адресат первого посева команды из TEAM_ID и запасной получатель,
        пока подписчиков в базе нет.
        """
        ids = self.allowed_chat_ids()
        return ids[0] if ids else ""

    def chat_allowed(self, chat_id: str) -> bool:
        if not self.whitelist_only:
            return True
        return str(chat_id) in self.allowed_chat_ids()


def load() -> Config:
    return Config()
