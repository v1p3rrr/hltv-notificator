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
from .notify.telegram import API_BASE as TELEGRAM_API_BASE, Telegram
from .reminders import ReminderScheduler
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


# Сколько ждём, пока очередь допишет уже решённое к отправке. Меньше, чем
# stop_grace_period в compose (15s): за ним докер присылает SIGKILL, и не
# успеть дописаться лучше, чем быть убитым посреди записи.
SHUTDOWN_DRAIN_SECONDS = 8.0


def _warn_about_retired_variables(config) -> None:
    """Убранные переменные обязаны сообщать о себе.

    `TELEGRAM_ALLOWED_CHATS` больше не читается — id перечисляются в
    `TELEGRAM_CHAT_ID` через запятую. Промолчать здесь нельзя: если весь список
    был в старой переменной, белый список окажется пустым, и бот перестанет
    отвечать вообще кому-либо. Снаружи это выглядит как «бот умер».
    """
    if not os.environ.get("TELEGRAM_ALLOWED_CHATS", "").strip():
        return
    known = ", ".join(config.allowed_chat_ids()) or "СПИСОК ПУСТ"
    log.warning("TELEGRAM_ALLOWED_CHATS больше не читается: перенесите id в "
                "TELEGRAM_CHAT_ID через запятую. Сейчас разрешены: %s", known)


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

    _warn_about_retired_variables(config)

    storage = Storage(config.db_path)
    # Первый посев: команда из .env становится первой отслеживаемой. Дальше
    # список живёт в базе и правится через бота, а переменные окружения
    # остаются только запасным значением.
    # Разово: ключи журнала, записанные до появления подписчиков, получают
    # адресата. Иначе первое же обновление разослало бы историю заново.
    adopted = storage.adopt_legacy_event_keys(config.main_chat_id)
    if adopted:
        log.info("журнал событий приведён к новому формату: %d записей", adopted)

    for chat in config.allowed_chat_ids():
        if storage.add_subscriber(chat, note="из TELEGRAM_CHAT_ID"):
            # Новому подписчику раскладываем напоминания по умолчанию, дальше
            # он правит их сам.
            for minutes in config.reminder_minutes():
                storage.add_reminder(chat, minutes)
    main_chat = config.main_chat_id
    if not storage.teams(enabled_only=False) and config.team_id and main_chat:
        storage.add_team(main_chat, config.team_id, config.team_slug, config.team_name)
        log.info("первая отслеживаемая команда взята из конфига: %s (id %s) для чата %s",
                 config.team_name, config.team_id, main_chat)
    http = HltvHttp(config)
    telegram: Optional[Telegram] = (
        Telegram(config.bot_token, config.proxies_for(TELEGRAM_API_BASE))
        if config.telegram_enabled() else None)
    notifier = Notifier(storage, config, telegram)
    poller = SchedulePoller(storage, config, http, notifier)
    messenger = LiveMessenger(storage, config, telegram)
    supervisor = LiveSupervisor(storage, config, notifier, messenger)
    matches = MatchPoller(storage, config, http, notifier, supervisor)

    if config.dry_run:
        log.warning("DRY_RUN включён: уведомления пишутся в лог, в Telegram не уходят")
    if config.proxy.configured:
        # Печатаем всегда, когда прокси есть: молчаливый прокси — это полчаса
        # разглядывания таймаутов на ровном месте.
        log.info("%s", config.proxy.describe())
    if telegram is None:
        missing = []
        if not config.bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not config.allowed_chat_ids():
            missing.append("TELEGRAM_CHAT_ID")
        log.warning("работаем без Telegram: не задано %s", ", ".join(missing))

    stop = asyncio.Event()
    _install_signal_handlers(stop)

    watchdog = Watchdog(storage, config)
    reminders = ReminderScheduler(storage, config)
    # Очередь держим отдельно: при остановке её нельзя рвать наравне с
    # остальными, см. ниже.
    outbox = asyncio.create_task(notifier.run(stop), name="outbox")
    tasks: List[asyncio.Task] = [
        asyncio.create_task(reminders.run(stop, notifier), name="reminders"),
        asyncio.create_task(poller.run(stop), name="schedule-poller"),
        asyncio.create_task(watchdog.run(stop, notifier), name="watchdog"),
        asyncio.create_task(matches.run(stop), name="match-poller"),
        outbox,
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
    # Всех, кроме очереди, гасим сразу: они только порождают новое, а нового
    # нам уже не надо. Бот при этом висит в getUpdates до 25 секунд — ждать его
    # мы не можем, у докера свой таймер.
    others = [task for task in tasks if task is not outbox]
    for task in others:
        task.cancel()
    await asyncio.gather(*others, return_exceptions=True)
    await supervisor.shutdown()

    # А очередь дописывает начатое сама и по своей воле выходит: stop уже
    # взведён. Рвать её отменой нельзя — отмена посреди send_message оставила бы
    # сообщение ОТПРАВЛЕННЫМ, но не отмеченным в базе, и при следующем запуске
    # оно ушло бы человеку второй раз.
    try:
        await asyncio.wait_for(outbox, timeout=SHUTDOWN_DRAIN_SECONDS)
    except asyncio.TimeoutError:
        log.warning("очередь не успела дописаться за %.0f с; остаток (%d) уйдёт "
                    "при следующем запуске", SHUTDOWN_DRAIN_SECONDS,
                    storage.pending_count())
    except asyncio.CancelledError:
        pass

    await http.close()
    if telegram is not None:
        await telegram.close()
    storage.prune(sent_days=config.outbox_keep_days, events_days=config.events_keep_days)
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


def health(config_module_=None) -> int:
    """Проверка живости для Docker HEALTHCHECK.

    Смотрит не «процесс запущен», а «сервис делает свою работу»: база
    открывается и расписание опрашивалось не слишком давно. Зависший процесс
    выглядит для докера живым, и без этой проверки restart-policy его не
    перезапустит.
    """
    load_dotenv(Path(".env"))
    config = config_module.load()
    setup_logging(config.log_level)
    try:
        storage = Storage(config.db_path)
    except Exception as exc:  # noqa: BLE001 - причину надо показать
        print(f"нездоров: база не открывается: {exc}")
        return 1

    try:
        last_poll = storage.get_meta("last_schedule_poll_utc")
        if not last_poll:
            # Сервис только что стартовал и ещё не успел опросить — это не
            # повод его убивать.
            print("здоров: опросов ещё не было")
            return 0
        from .state.db import parse_iso, utcnow
        age = (utcnow() - parse_iso(last_poll)).total_seconds()
        limit = config.interval_for("idle") * 2 + 300
        if age > limit:
            print(f"нездоров: расписание не опрашивалось {int(age)} с (порог {int(limit)})")
            return 1
        print(f"здоров: последний опрос {int(age)} с назад")
        return 0
    finally:
        storage.close()


def main() -> int:
    if "--health" in sys.argv:
        return health()
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
