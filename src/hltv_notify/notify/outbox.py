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
        """False — событие с таким ключом уже отправлялось, тихо пропускаем."""
        body = fmt.render(event, team_name=self.config.team_name, tz_name=self.config.timezone)
        created = self.storage.record_event(
            idempotency_key=event.idempotency_key,
            event_type=event.type,
            match_id=event.match_id,
            body=body,
        )
        if created:
            log.info("событие %s поставлено в очередь: %s", event.type, event.idempotency_key)
        else:
            log.debug("событие уже отправлялось, пропуск: %s", event.idempotency_key)
        return created

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
        try:
            message_id = await self.telegram.send_message(self.config.chat_id, row["body"])
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
