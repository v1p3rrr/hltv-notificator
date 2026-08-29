"""Команды боту — интерфейс сервиса.

Пользователь должен понимать, почему уведомление не пришло, не подключаясь
по SSH. Ответы на команды идут напрямую, минуя outbox: это не уведомления,
их не надо ни дедуплицировать, ни досылать после рестарта.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

from zoneinfo import ZoneInfo

from .config import HLTV_BASE, Config
from .notify import format as fmt
from .sources import team_page
from .notify.telegram import Telegram, TelegramError
from .models import MatchState
from .scheduler import LAST_ERROR_KEY, LAST_POLL_KEY, SchedulePoller
from .state.db import Storage, parse_iso, utcnow
from .watchdog import Watchdog

log = logging.getLogger(__name__)

TEAM_URL_RE = re.compile(r"/team/(\d+)(?:/([^/?#\s]+))?")


DURATION_RE = re.compile(r"^(\d+)\s*([мmчh]?)", re.IGNORECASE)


def _parse_minutes(argument: str):
    """«15», «15m», «90», «2h» → минуты. None — не разобрали."""
    found = DURATION_RE.match((argument or "").strip())
    if not found:
        return None
    value = int(found.group(1))
    if found.group(2).lower() in ("ч", "h"):
        value *= 60
    return value if 1 <= value <= 24 * 60 else None


def _human(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} мин"
    hours, rest = divmod(minutes, 60)
    return f"{hours} ч" if rest == 0 else f"{hours} ч {rest} мин"


def _parse_team_ref(argument: str):
    """id и slug из ссылки на команду или из голого id."""
    argument = (argument or "").strip()
    found = TEAM_URL_RE.search(argument)
    if found:
        return int(found.group(1)), found.group(2)
    if argument.isdigit():
        return int(argument), None
    return None, None

MUTABLE_EVENTS = ("E1", "E2", "E3", "E4", "E5", "E6", "E7", "E9")

HELP = (
    "Команды:\n"
    "/teams — какие команды вы отслеживаете\n"
    "/track &lt;ссылка на команду&gt; — добавить команду\n"
    "/untrack &lt;id&gt; — перестать отслеживать\n"
    "/mute &lt;id&gt; &lt;E5,E9&gt; — заглушить типы событий по команде\n"
    "/unmute &lt;id&gt; — снять все глушения по команде\n"
    "/remind [15m|1h] — напоминания перед матчем, /remind rm 15m — убрать\n"
    "/tz &lt;Europe/Berlin&gt; — ваш часовой пояс\n"
    "/pause — молчать, /resume — снова слать\n"
    "/whoami — ваш chat_id\n"
    "/status — состояние сервиса и источников\n"
    "/next — ближайшие матчи по данным сервиса\n"
    "/check — проверить расписание прямо сейчас\n"
    "/verbose on|off — подробный режим логов"
)


class CommandBot:
    def __init__(self, storage: Storage, config: Config, telegram: Telegram,
                 poller: SchedulePoller, matches=None, http=None):
        self.storage = storage
        self.config = config
        self.telegram = telegram
        self.poller = poller
        self.matches = matches
        self.http = http
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
        command, _, argument = text.partition(" ")
        command = command.split("@")[0].lower()
        argument = argument.strip()

        if command == "/whoami":
            # Отвечаем всем: человек должен узнать свой id, чтобы его внесли
            # в белый список. Ничего секретного в этом числе нет.
            await self._reply(chat_id, f"Ваш chat_id: <code>{fmt.escape(chat_id)}</code>")
            return

        if not self.config.chat_allowed(chat_id):
            # Молча: отвечать незнакомцу отказом значит подтверждать
            # существование бота кому попало. Свой chat_id человек узнаёт
            # командой /whoami, она обрабатывается выше и отвечает всем.
            # Сам id пишем в лог — чтобы владелец мог внести его в белый список.
            log.warning("команда %s из чата %s отклонена: его нет в белом списке",
                        command, chat_id)
            return

        # Разрешённый чат становится подписчиком при первом же обращении:
        # иначе пришлось бы заводить его руками в базе.
        if self.storage.get_subscriber(chat_id) is None:
            self.storage.add_subscriber(chat_id)
            log.info("новый подписчик: %s", chat_id)

        handlers = {
            "/teams": lambda: self._teams(chat_id),
            "/status": self._status,
            "/live": self._live,
            "/next": self._next,
            "/check": self._check,
            "/start": lambda: HELP,
            "/help": lambda: HELP,
        }
        try:
            if command == "/verbose":
                reply = self._verbose(argument.lower())
            elif command == "/track":
                reply = await self._track(chat_id, argument)
            elif command == "/untrack":
                reply = self._untrack(chat_id, argument)
            elif command == "/mute":
                reply = self._mute(chat_id, argument)
            elif command == "/unmute":
                reply = self._unmute(chat_id, argument)
            elif command == "/remind":
                reply = self._remind(chat_id, argument)
            elif command == "/tz":
                reply = self._timezone(chat_id, argument)
            elif command == "/pause":
                reply = self._pause(chat_id, True)
            elif command == "/resume":
                reply = self._pause(chat_id, False)
            elif command in handlers:
                result = handlers[command]()
                reply = await result if asyncio.iscoroutine(result) else result
            else:
                reply = f"Не знаю такой команды.\n\n{HELP}"
        except Exception:  # noqa: BLE001 - ответить надо в любом случае
            log.exception("ошибка обработки команды %s", command)
            reply = "Команда упала, подробности в логах."

        await self._reply(chat_id, reply)

    async def _reply(self, chat_id: str, text: str) -> None:
        try:
            await self.telegram.send_message(chat_id, text)
        except TelegramError as exc:
            log.error("не удалось ответить в чат %s: %s", chat_id, exc)

    # ------------------------------------------------------------------

    def _status(self) -> str:
        tz = self.config.timezone
        last_poll = self.storage.get_meta(LAST_POLL_KEY)
        last_error = self.storage.get_meta(LAST_ERROR_KEY)
        matches = len(self.storage.all_matches())
        upcoming = len(self.storage.upcoming_matches())

        lines = [
            "<b>Состояние сервиса</b>",
            f"Подписчиков: {len(self.storage.subscribers())}, "
            f"команд под наблюдением: {len(self.storage.tracked_teams())}",
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
        lines.append(self._feed_line())
        degraded = Watchdog(self.storage, self.config).degraded_subsystems()
        if degraded:
            lines.append("⚠️ Не работает: " + ", ".join(degraded))
        if last_error:
            lines.append(f"Последняя ошибка: <i>{fmt.escape(last_error)}</i>")
        return "\n".join(lines)

    def _teams(self, chat_id: str) -> str:
        rows = self.storage.teams(chat_id, enabled_only=False)
        if not rows:
            return "Вы не отслеживаете ни одной команды. Добавить: /track &lt;ссылка&gt;"
        lines = ["<b>Ваши команды</b>"]
        for row in rows:
            marks = []
            if not row["enabled"]:
                marks.append("выключена")
            if row["muted_events"]:
                marks.append("заглушено: " + row["muted_events"].replace(",", ", "))
            tail = ("  (" + "; ".join(marks) + ")") if marks else ""
            lines.append(f"{fmt.escape(row['name'])} — id {row['team_id']}{tail}")
        return "\n".join(lines)

    async def _track(self, chat_id: str, argument: str) -> str:
        """Добавить команду. Принимает ссылку на страницу команды или её id.

        Ссылка предпочтительнее: из неё берутся и id, и slug. У команд бывают
        тёзки и приставки ex-, поэтому по названию искать нельзя — только по id.
        """
        team_id, slug = _parse_team_ref(argument)
        if team_id is None:
            return ("Не понял команду. Пришлите ссылку вида\n"
                    "https://www.hltv.org/team/12857/forze-reload")
        if self.http is None:
            return "Добавление недоступно: сервис запущен без HTTP-слоя."

        url = f"{HLTV_BASE}/team/{team_id}/{slug or '-'}"
        try:
            html = await self.http.get_text(url)
        except Exception as exc:  # noqa: BLE001 - показать причину пользователю
            return f"Страница команды не открылась: {type(exc).__name__}: {exc}"

        name = team_page.parse_team_name(html)
        if not name:
            return f"На странице {url} не нашлось имени команды — проверьте ссылку."

        added = self.storage.add_team(chat_id, team_id, slug or str(team_id), name)
        self.poller.request_poll()
        if added:
            return (f"Отслеживаю <b>{fmt.escape(name)}</b> (id {team_id}).\n"
                    "Текущее расписание занесено молча — уведомления начнутся "
                    "со следующих изменений.")
        return f"<b>{fmt.escape(name)}</b> (id {team_id}) уже отслеживается, включил обратно."

    def _untrack(self, chat_id: str, argument: str) -> str:
        team_id, _ = _parse_team_ref(argument)
        if team_id is None:
            return "Использование: /untrack &lt;id команды&gt;"
        row = self.storage.get_team(chat_id, team_id)
        if row is None:
            return f"Вы и так не отслеживаете команду {team_id}."
        self.storage.set_team_enabled(chat_id, team_id, False)
        # История матчей не удаляется: если команду вернут, журнал уже
        # отправленного не даст разослать всё заново.
        return (f"Больше не отслеживаю <b>{fmt.escape(row['name'])}</b> (id {team_id}). "
                "История сохранена.")

    def _mute(self, chat_id: str, argument: str) -> str:
        """Заглушить типы событий по одной команде.

        Если отслеживаемые команды играют друг против друга, событие всё равно
        придёт, когда его хочет ХОТЯ БЫ ОДНА из ваших команд в этом матче:
        иначе одна команда молча глушила бы уведомления про другую.
        """
        parts = argument.split()
        team_id, _ = _parse_team_ref(parts[0]) if parts else (None, None)
        if team_id is None or len(parts) < 2:
            return ("Использование: /mute &lt;id команды&gt; &lt;типы через запятую&gt;\n"
                    f"Типы: {', '.join(MUTABLE_EVENTS)}")
        row = self.storage.get_team(chat_id, team_id)
        if row is None:
            return f"Вы не отслеживаете команду {team_id}."

        requested = [part.strip().upper() for part in parts[1].replace(";", ",").split(",")]
        requested = [part for part in requested if part]
        unknown = [part for part in requested if part not in MUTABLE_EVENTS]
        if unknown:
            return (f"Не знаю тип(ы): {', '.join(unknown)}.\n"
                    f"Доступные: {', '.join(MUTABLE_EVENTS)}")

        self.storage.set_team_mutes(chat_id, team_id, requested)
        return (f"По команде <b>{fmt.escape(row['name'])}</b> заглушено: "
                f"{', '.join(sorted(set(requested)))}")

    def _unmute(self, chat_id: str, argument: str) -> str:
        team_id, _ = _parse_team_ref(argument)
        if team_id is None:
            return "Использование: /unmute &lt;id команды&gt;"
        row = self.storage.get_team(chat_id, team_id)
        if row is None:
            return f"Вы не отслеживаете команду {team_id}."
        self.storage.set_team_mutes(chat_id, team_id, [])
        return f"По команде <b>{fmt.escape(row['name'])}</b> глушения сняты."

    def _remind(self, chat_id: str, argument: str) -> str:
        """Список интервалов, за сколько до матча напоминать."""
        parts = argument.split()
        if not parts:
            return self._remind_list(chat_id)

        removing = parts[0].lower() in ("rm", "del", "-", "убрать", "удалить")
        value = _parse_minutes(parts[1] if removing and len(parts) > 1 else parts[0])
        if value is None:
            return ("Использование: /remind 15m — добавить, /remind rm 15m — убрать.\n"
                    "Принимаю минуты и часы: 15, 30m, 1h, 2h.")

        if removing:
            if not self.storage.remove_reminder(chat_id, value):
                return f"Напоминания за {fmt.escape(_human(value))} и не было."
            return f"Убрал напоминание за {fmt.escape(_human(value))}.\n\n" + \
                   self._remind_list(chat_id)
        if not self.storage.add_reminder(chat_id, value):
            return f"Напоминание за {fmt.escape(_human(value))} уже стоит."
        return f"Буду напоминать за {fmt.escape(_human(value))}.\n\n" + \
               self._remind_list(chat_id)

    def _remind_list(self, chat_id: str) -> str:
        values = self.storage.reminders(chat_id)
        if not values:
            return "Напоминаний нет. Добавить: /remind 15m"
        listed = ", ".join(_human(value) for value in values)
        return f"<b>Напоминаю за:</b> {fmt.escape(listed)}"

    def _timezone(self, chat_id: str, argument: str) -> str:
        """Свой пояс у каждого: подписчики могут жить в разных."""
        current = self.storage.subscriber_timezone(chat_id, self.config.timezone)
        if not argument:
            return (f"Ваш пояс: <b>{fmt.escape(current)}</b>\n"
                    "Сменить: /tz Europe/Berlin")
        try:
            ZoneInfo(argument)
        except Exception:  # noqa: BLE001 - имя пояса приходит от человека
            return (f"Не знаю пояс «{fmt.escape(argument)}».\n"
                    "Нужно имя из базы IANA, например Europe/Moscow или Asia/Tbilisi.")
        self.storage.set_subscriber_timezone(chat_id, argument)
        return f"Время буду показывать в <b>{fmt.escape(argument)}</b>."

    def _pause(self, chat_id: str, paused: bool) -> str:
        self.storage.set_subscriber_paused(chat_id, paused)
        if paused:
            return ("Молчу. Уведомления не будут ни приходить, ни копиться — "
                    "пропущенное потом не досылается.\nВключить обратно: /resume")
        return "Снова на связи."

    def _feed_line(self) -> str:
        """Состояние живого фида: от него зависят E5, мультикиллы и скорость E6."""
        supervisor = getattr(self.matches, "supervisor", None) if self.matches else None
        if supervisor is None:
            return "Живой фид: не включён"
        feeds = supervisor.connected_matches()
        if not feeds:
            return "Живой фид: матчей нет"
        connected = [str(mid) for mid, ok in feeds.items() if ok]
        pending = [str(mid) for mid, ok in feeds.items() if not ok]
        parts = []
        if connected:
            parts.append("на связи: " + ", ".join(connected))
        if pending:
            parts.append("подключается: " + ", ".join(pending))
        return "Живой фид: " + "; ".join(parts)

    def _live(self) -> str:
        """Что прямо сейчас на идущем матче.

        Полезно, когда уведомление не пришло: видно, дошёл ли счёт до сервиса
        вообще и от какого источника он последний раз обновлялся.
        """
        rows = [row for row in (self.matches.active() if self.matches else [])
                if row["state"] == MatchState.LIVE]
        if not rows:
            return "Сейчас матчей нет."

        supervisor = getattr(self.matches, "supervisor", None)
        feeds = supervisor.connected_matches() if supervisor else {}
        blocks = []
        for row in rows:
            state = self.storage.get_state(row["match_id"])
            score = state["current_map_score"] if state else None
            series = state["series_score"] if state else None
            map_name = state["current_map_name"] if state else None
            source = state["last_source"] if state else "?"
            feed = "на связи" if feeds.get(row["match_id"]) else "нет"
            block = [
                f"<b>{fmt.escape(self.config.team_name)} — {fmt.escape(row['opponent_name'])}</b>",
                fmt.escape(row["event_name"]),
                f"Карта: {fmt.escape(map_name) if map_name else '—'}"
                f"   счёт: {score or '—'}   по картам: {series or '—'}",
                f"Живой фид: {feed} · последнее обновление от источника «{source}»",
            ]
            for result in self.storage.map_results(row["match_id"]):
                block.append(f"   {fmt.escape(result['map_name'])} — "
                             f"{result['score_team']}:{result['score_opponent']}")
            block.append(row["url"])
            blocks.append("\n".join(block))
        return "\n\n".join(blocks)

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
