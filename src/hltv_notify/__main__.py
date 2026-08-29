"""Точка входа: поднимает задачи и корректно их гасит.

Один процесс, один пользователь. Компоненты не вызывают друг друга напрямую:
опрос пишет наблюдения, машина состояний рождает события, нотификатор
отправляет. Дедупликация живёт в одном месте, а не размазана по коду.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import List, Optional

from . import config as config_module
from .bot import CommandBot
from .http import HltvHttp
from .live_worker import LiveSupervisor
from .match_poller import MatchPoller
from .notify.live_message import LiveMessenger
from .notify.outbox import Notifier
from .notify.telegram import Telegram
from .scheduler import SchedulePoller
from .watchdog import Watchdog
from .state.db import Storage

log = logging.getLogger("hltv_notify")


def load_dotenv(path: Path) -> None:
    """Секреты — только из окружения или .env вне репозитория."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def setup_logging(level: str) -> None:
    # Логи и сообщения на русском: под Windows консоль по умолчанию cp1252,
    # и первая же кириллическая строка уронила бы обработчик логов.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("hltv_notify").setLevel(getattr(logging, level.upper(), logging.INFO))


async def run() -> int:
    load_dotenv(Path(".env"))
    config = config_module.load()
    setup_logging(config.log_level)

    storage = Storage(config.db_path)
    # Первый посев: команда из .env становится первой отслеживаемой. Дальше
    # список живёт в базе и правится через бота, а переменные окружения
    # остаются только запасным значением.
    for chat in config.allowed_chat_ids():
        storage.add_subscriber(chat, note="из TELEGRAM_ALLOWED_CHATS")
    if not storage.teams(enabled_only=False) and config.team_id and config.chat_id:
        storage.add_team(config.chat_id, config.team_id, config.team_slug, config.team_name)
        log.info("первая отслеживаемая команда взята из конфига: %s (id %s) для чата %s",
                 config.team_name, config.team_id, config.chat_id)
    http = HltvHttp(config)
    telegram: Optional[Telegram] = Telegram(config.bot_token) if config.telegram_enabled() else None
    notifier = Notifier(storage, config, telegram)
    poller = SchedulePoller(storage, config, http, notifier)
    messenger = LiveMessenger(storage, config, telegram)
    supervisor = LiveSupervisor(storage, config, notifier, messenger)
    matches = MatchPoller(storage, config, http, notifier, supervisor)

    if config.dry_run:
        log.warning("DRY_RUN включён: уведомления пишутся в лог, в Telegram не уходят")
    if telegram is None:
        missing = []
        if not config.bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not config.chat_id and not config.allowed_chat_ids():
            missing.append("TELEGRAM_CHAT_ID или TELEGRAM_ALLOWED_CHATS")
        log.warning("работаем без Telegram: не задано %s", ", ".join(missing))

    stop = asyncio.Event()
    _install_signal_handlers(stop)

    watchdog = Watchdog(storage, config)
    tasks: List[asyncio.Task] = [
        asyncio.create_task(poller.run(stop), name="schedule-poller"),
        asyncio.create_task(watchdog.run(stop, notifier), name="watchdog"),
        asyncio.create_task(matches.run(stop), name="match-poller"),
        asyncio.create_task(notifier.run(stop), name="outbox"),
    ]
    if telegram is not None:
        bot = CommandBot(storage, config, telegram, poller, matches, http)
        tasks.append(asyncio.create_task(bot.run(stop), name="command-bot"))

    log.info("сервис запущен: подписчиков %d, команд под наблюдением %d (%s), "
             "режим отправки %s",
             len(storage.subscribers()), len(storage.tracked_teams()),
             ", ".join(row["name"] for row in storage.tracked_teams()) or "нет",
             "dry-run" if config.dry_run else "боевой")

    try:
        await stop.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        stop.set()

    log.info("останавливаемся")
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await supervisor.shutdown()
    await http.close()
    if telegram is not None:
        await telegram.close()
    storage.close()
    return 0


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows: сигналы через loop не ставятся, останов идёт по Ctrl+C.
            pass


def main() -> int:
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
