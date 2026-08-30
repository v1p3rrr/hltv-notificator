"""Очередь исходящих: отказ Telegram не должен терять уведомления.

Событие попадает сюда уже дедуплицированным (см. Storage.record_event),
поэтому задача воркера простая: доставить и не превысить лимиты Telegram.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from ..config import Config
from ..models import Event
from ..state.db import Storage
from . import audience
from . import format as fmt
from .telegram import Telegram, TelegramError

log = logging.getLogger(__name__)

# Telegram разрешает примерно одно сообщение в секунду в один чат.
SEND_INTERVAL_SECONDS = 1.2
MAX_ATTEMPTS = 8


class Notifier:
    """Приём событий и их доставка. Единственный, кто пишет в Telegram."""

    def __init__(self, storage: Storage, config: Config, telegram: Optional[Telegram]):
        self.storage = storage
        self.config = config
        self.telegram = telegram

    def enqueue(self, event: Event) -> bool:
        """Поставить событие в очередь КАЖДОМУ, кого оно касается.

        False — никому не поставлено: либо всем уже отправлялось, либо событие
        заглушено у всех подходящих подписчиков.
        """
        created = 0
        for chat_id, for_team_id in self._recipients(event):
            body = fmt.render(
                event, team_name=self.config.team_name,
                # Пояс у каждого свой: подписчики могут жить в разных.
                tz_name=self.storage.subscriber_timezone(chat_id, self.config.timezone),
                for_team_id=for_team_id)
            if self.storage.record_event(
                    idempotency_key=event.idempotency_key,
                    event_type=event.type,
                    match_id=event.match_id,
                    body=body,
                    chat_id=chat_id):
                created += 1

        if created:
            log.info("событие %s поставлено в очередь %d адресату(ам): %s",
                     event.type, created, event.idempotency_key)
        else:
            log.debug("событие никому не ушло (дубль или заглушено): %s",
                      event.idempotency_key)
        return bool(created)

    def _recipients(self, event: Event):
        """Кому это событие адресовано и от лица какой команды показывать.

        Кто вообще на связи, решает `audience` — там же и проверка паузы.
        Здесь остаётся то, что знает только очередь: адресные события и
        глушение по типам.

        Правило для матча двух отслеживаемых команд: событие уходит
        подписчику, если ХОТЯ БЫ ОДНА из его команд в этом матче не заглушила
        такой тип. Иначе одна команда молча глушила бы уведомления про другую.
        """
        if event.match_id is None:
            rows = audience.service_audience(self.storage, self.config)
        else:
            teams = self.storage.match_team_ids(event.match_id)
            player_team = event.payload.get("team_id")
            if event.type == "E9" and player_team:
                # Мультикилл адресован тем, кто следит за командой ЭТОГО игрока.
                teams = [player_team]
            rows = audience.match_audience(self.storage, self.config,
                                           event.match_id, teams=teams)

        only_chat = event.payload.get("only_chat")
        if only_chat is not None:
            # Адресное событие (напоминание): интервалы у подписчиков разные,
            # и рассылать его всем участникам матча нельзя.
            if only_chat not in audience.active_subscribers(self.storage):
                return []
            mine = [(chat, their) for chat, their in rows if chat == only_chat]
            # Матч может быть ещё не связан с командами — напоминание всё
            # равно адресное, показываем его от лица матча.
            rows = mine or [(only_chat, [])]

        recipients = []
        for chat, their_teams in rows:
            if not their_teams:
                recipients.append((chat, None))
                continue
            wanted = [team_id for team_id in their_teams
                      if event.type not in self.storage.team_mutes(chat, team_id)]
            if not wanted:
                log.debug("событие %s заглушено у %s", event.type, chat)
                continue
            recipients.append((chat, wanted[0]))
        return recipients

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self._drain()
            except Exception:  # noqa: BLE001 - воркер не имеет права умирать
                log.exception("сбой воркера очереди")
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    async def _drain(self) -> None:
        for row in self.storage.due_outbox():
            await self._deliver(row)
            await asyncio.sleep(SEND_INTERVAL_SECONDS)

    async def _deliver(self, row) -> None:
        if self.config.dry_run or self.telegram is None:
            reason = "DRY_RUN" if self.config.dry_run else "Telegram не настроен"
            log.info("[%s] сообщение не отправлено, содержимое:\n%s", reason, row["body"])
            self.storage.mark_sent(row["id"], None)
            return

        attempts = row["attempts"] + 1
        chat_id = row["chat_id"] or self.config.main_chat_id
        try:
            message_id = await self.telegram.send_message(chat_id, row["body"])
        except TelegramError as exc:
            if exc.fatal:
                log.error("сообщение %s отброшено, повтор не поможет: %s", row["id"], exc)
                self.storage.mark_sent(row["id"], None)
                return
            if attempts >= MAX_ATTEMPTS:
                log.error("сообщение %s не доставлено за %d попыток: %s",
                          row["id"], attempts, exc)
                self.storage.mark_retry(row["id"], attempts, 3600)
                return
            delay = exc.retry_after if exc.retry_after else min(2 ** attempts, 300)
            log.warning("Telegram не принял (%s), повтор через %.0fs", exc, delay)
            self.storage.mark_retry(row["id"], attempts, delay)
            return

        self.storage.mark_sent(row["id"], message_id)
        log.info("отправлено сообщение %s (telegram id %s)", row["id"], message_id)
