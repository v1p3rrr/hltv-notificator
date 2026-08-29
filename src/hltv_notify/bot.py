"""Команды боту — интерфейс сервиса.

Пользователь должен понимать, почему уведомление не пришло, не подключаясь
по SSH. Ответы на команды идут напрямую, минуя outbox: это не уведомления,
их не надо ни дедуплицировать, ни досылать после рестарта.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .config import Config
from .notify import format as fmt
from .notify.telegram import Telegram, TelegramError
from .scheduler import LAST_ERROR_KEY, LAST_POLL_KEY, SchedulePoller
from .state.db import Storage, parse_iso, utcnow

log = logging.getLogger(__name__)

HELP = (
    "Команды:\n"
    "/status — состояние сервиса и источников\n"
    "/next — ближайшие матчи по данным сервиса\n"
    "/check — проверить расписание прямо сейчас\n"
    "/verbose on|off — подробный режим логов"
)


class CommandBot:
    def __init__(self, storage: Storage, config: Config, telegram: Telegram,
                 poller: SchedulePoller, matches=None):
        self.storage = storage
        self.config = config
        self.telegram = telegram
        self.poller = poller
        self.matches = matches
        self._offset: Optional[int] = None

    async def run(self, stop: asyncio.Event) -> None:
        # Пропускаем накопившееся за время простоя: отвечать на команды
        # недельной давности бессмысленно.
        try:
            backlog = await self.telegram.get_updates(None, timeout=0)
            if backlog:
                self._offset = backlog[-1]["update_id"] + 1
        except TelegramError as exc:
            log.warning("не удалось прочитать очередь команд: %s", exc)

        while not stop.is_set():
            try:
                updates = await self.telegram.get_updates(self._offset, timeout=25)
            except TelegramError as exc:
                log.warning("getUpdates не удался: %s", exc)
                await asyncio.sleep(5)
                continue
            except Exception:  # noqa: BLE001 - бот не имеет права умирать
                log.exception("сбой опроса команд")
                await asyncio.sleep(5)
                continue

            for update in updates:
                self._offset = update["update_id"] + 1
                await self._handle(update)

    async def _handle(self, update: dict) -> None:
        message = update.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return
        if chat_id != str(self.config.chat_id):
            log.warning("команда из чужого чата %s проигнорирована", chat_id)
            return

        command, _, argument = text.partition(" ")
        command = command.split("@")[0].lower()
        argument = argument.strip().lower()

        handlers = {
            "/status": self._status,
            "/next": self._next,
            "/check": self._check,
            "/start": lambda: HELP,
            "/help": lambda: HELP,
        }
        try:
            if command == "/verbose":
                reply = self._verbose(argument)
            elif command in handlers:
                result = handlers[command]()
                reply = await result if asyncio.iscoroutine(result) else result
            else:
                reply = f"Не знаю такой команды.\n\n{HELP}"
        except Exception:  # noqa: BLE001 - ответить надо в любом случае
            log.exception("ошибка обработки команды %s", command)
            reply = "Команда упала, подробности в логах."

        try:
            await self.telegram.send_message(chat_id, reply)
        except TelegramError as exc:
            log.error("не удалось ответить на %s: %s", command, exc)

    # ------------------------------------------------------------------

    def _status(self) -> str:
        tz = self.config.timezone
        last_poll = self.storage.get_meta(LAST_POLL_KEY)
        last_error = self.storage.get_meta(LAST_ERROR_KEY)
        matches = len(self.storage.all_matches())
        upcoming = len(self.storage.upcoming_matches())

        lines = [
            "<b>Состояние сервиса</b>",
            f"Команда: {self.config.team_name} (id {self.config.team_id})",
            f"Режим опроса расписания: {self.poller.mode}",
            f"Режим опроса матчей: {self.matches.mode if self.matches else '—'}",
            f"Активных матчей: {len(self.matches.active()) if self.matches else 0}",
            f"Отправка: {'ВЫКЛЮЧЕНА (DRY_RUN)' if self.config.dry_run else 'включена'}",
            f"Последний опрос: {fmt.human_time(last_poll, tz) if last_poll else 'ещё не было'}",
            f"Неудач подряд: {self.poller.http.consecutive_failures}",
            f"Матчей в базе: {matches}, предстоящих: {upcoming}",
            f"В очереди на отправку: {self.storage.pending_count()}",
            f"Всего событий отправлено: {self.storage.sent_event_count()}",
        ]
        if last_error:
            lines.append(f"Последняя ошибка: <i>{fmt.escape(last_error)}</i>")
        return "\n".join(lines)

    def _next(self) -> str:
        rows = self.storage.upcoming_matches()
        if not rows:
            return "Предстоящих матчей нет. Для этой команды это нормально — она может не играть неделями."
        lines = ["<b>Ближайшие матчи</b>"]
        for row in rows[:10]:
            when = fmt.human_time(row["start_utc"], self.config.timezone)
            lines.append(
                f"{when} — {fmt.escape(row['opponent_name'])}\n"
                f"    {fmt.escape(row['event_name'])}\n"
                f"    {row['url']}")
        return "\n".join(lines)

    async def _check(self) -> str:
        self.poller.request_poll()
        return "Проверяю расписание. Если что-то изменилось, уведомление придёт отдельно."

    def _verbose(self, argument: str) -> str:
        if argument not in {"on", "off"}:
            return "Использование: /verbose on | /verbose off"
        level = logging.DEBUG if argument == "on" else getattr(
            logging, self.config.log_level.upper(), logging.INFO)
        logging.getLogger("hltv_notify").setLevel(level)
        return f"Подробный режим {'включён' if argument == 'on' else 'выключен'}."
