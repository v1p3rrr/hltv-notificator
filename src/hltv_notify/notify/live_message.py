"""Живое сообщение со счётом: одно на карту, обновляется по ходу игры.

Это НЕ событие. У него нет ключа идемпотентности и его не надо досылать после
рестарта — его надо перерисовать текущим состоянием. Поэтому оно идёт мимо
outbox: очередь существует, чтобы не терять вехи, а устаревший кадр счёта
терять как раз можно и нужно.

Id сообщения хранится в базе, иначе после перезапуска сервис завёл бы на ту же
карту второе живое сообщение.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional, Tuple

from ..config import Config
from ..state.db import Storage
from . import audience
from . import format as fmt
from .telegram import Telegram, TelegramError

log = logging.getLogger(__name__)

# Telegram не любит частых правок. Даже если конфиг просит чаще — не даём.
HARD_MIN_EDIT_SECONDS = 5.0


class LiveMessenger:
    def __init__(self, storage: Storage, config: Config, telegram: Optional[Telegram]):
        self.storage = storage
        self.config = config
        self.telegram = telegram
        # Момент последней правки держим в памяти: смысл в ограничении частоты
        # обращений к Telegram, а не в переживании рестарта.
        self._last_edit: Dict[Tuple[int, int], float] = {}

    @property
    def _interval(self) -> float:
        return max(float(self.config.live_edit_seconds), HARD_MIN_EDIT_SECONDS)

    async def update(self, match_id: int, snapshot: dict, *, force: bool = False,
                     finalize: bool = False) -> None:
        """Живое сообщение — у каждого подписчика своё.

        Оно редактируется, а id сообщения свой в каждом чате, поэтому общего
        сообщения на всех быть не может.
        """
        if not self.config.live_message or not snapshot:
            return
        for chat_id, for_team_id in self._recipients(match_id):
            await self._update_one(chat_id, for_team_id, match_id, snapshot,
                                   force=force, finalize=finalize)

    def _recipients(self, match_id: int):
        """Тот же расчёт, что и у очереди событий, — и та же проверка паузы.

        Своего расчёта здесь когда-то и не хватало: живое сообщение уходило
        человеку, попросившему тишины через `/pause`.
        """
        return [(chat, teams[0] if teams else None)
                for chat, teams in audience.match_audience(
                    self.storage, self.config, match_id)]

    async def _update_one(self, chat_id: str, for_team_id, match_id: int, snapshot: dict,
                          *, force: bool = False, finalize: bool = False) -> None:
        map_number = int(snapshot.get("map_number") or 0)
        if map_number <= 0:
            return

        row = self.storage.live_message(chat_id, match_id, map_number)
        if row is not None and row["finalized"]:
            return

        key = (chat_id, match_id, map_number)
        if not force:
            elapsed = time.monotonic() - self._last_edit.get(key, 0.0)
            if elapsed < self._interval:
                return

        text = fmt.render_live(fmt.orient(snapshot, for_team_id),
                               team_name=self.config.team_name)
        if row is not None and row["last_text"] == text and not finalize:
            # Счёт не изменился — правка тем же текстом только тратит лимит.
            self._last_edit[key] = time.monotonic()
            return

        message_id = row["telegram_message_id"] if row is not None else None
        if self.config.dry_run or self.telegram is None:
            reason = "DRY_RUN" if self.config.dry_run else "Telegram не настроен"
            log.debug("[%s] живое сообщение матча %s карта %d:\n%s",
                      reason, match_id, map_number, text)
        else:
            try:
                if message_id is None:
                    message_id = await self.telegram.send_message(chat_id, text)
                    log.info("живое сообщение матча %s карта %d создано для %s (id %s)",
                             match_id, map_number, chat_id, message_id)
                else:
                    await self.telegram.edit_message_text(chat_id, message_id, text)
            except TelegramError as exc:
                # Живое сообщение — вспомогательное. Если оно не обновилось,
                # ронять из-за этого воркер и терять вехи нельзя.
                log.warning("живое сообщение матча %s карта %d не обновилось: %s",
                            match_id, map_number, exc)
                self._last_edit[key] = time.monotonic()
                return

        self._last_edit[key] = time.monotonic()
        self.storage.save_live_message(
            chat_id, match_id, map_number, telegram_message_id=message_id,
            text=text, finalized=finalize)

    async def finalize(self, match_id: int, snapshot: dict) -> None:
        """Последняя правка по окончании карты: замораживаем финальный счёт."""
        await self.update(match_id, snapshot, force=True, finalize=True)
