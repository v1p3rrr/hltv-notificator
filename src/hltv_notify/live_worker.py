"""Live Worker: держит соединение с фидом на время матча.

Живёт только пока матч идёт. Обрыв соединения — норма, а не авария: на записи
реального матча за час случилось 15 подключений и 14 обрывов. Поэтому
переподключение с backoff, а вся защита от повторов — в машине состояний.

Отдельно обрабатывается 403: это не сетевой сбой, а «отойди». Реконнекты с
обычным backoff в такой ситуации только долбят источник, поэтому пауза
минутами, а опрос страницы матча остаётся работать как был.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Dict, Optional

from .config import Config
from .models import Event
from .notify.live_message import LiveMessenger
from .notify.outbox import Notifier
from .sources.scorebot import (SCOREBOT_BASE, FeedIdle, FeedRejected,
                               FeedUnavailable, ScorebotClient,
                               frames_from_packets)
from .state.db import Storage, utcnow
from .state.live_machine import LiveMachine

log = logging.getLogger(__name__)

# Нижняя граница паузы после 403. Источник явно попросил отойти, и опускать
# её конфигом ниже разумного нельзя.
MIN_REJECTED_COOLDOWN_SECONDS = 60.0
MAX_BACKOFF_SECONDS = 60.0


class LiveWorker:
    """Одно соединение на один матч."""

    def __init__(self, storage: Storage, config: Config, notifier: Notifier,
                 match_id: int, url: str, messenger: Optional[LiveMessenger] = None):
        self.storage = storage
        self.config = config
        self.notifier = notifier
        self.messenger = messenger
        self.match_id = match_id
        self.url = url
        self.machine = LiveMachine(storage, config)
        self.connected = False
        self.rejected_until: float = 0.0

    async def run(self, stop: asyncio.Event) -> None:
        attempt = 0
        while not stop.is_set():
            client = ScorebotClient(
                self.match_id, referer=self.url,
                impersonate=self.config.impersonate,
                proxies=self.config.proxies_for(SCOREBOT_BASE))
            try:
                await client.connect()
                await client.subscribe()
                self.connected = True
                attempt = 0
                log.info("живой фид матча %s подключён (sid %s)", self.match_id, client.sid)
                await self._consume(client, stop)
            except FeedRejected as exc:
                self.connected = False
                cooldown = max(float(self.config.live_feed_cooldown),
                               MIN_REJECTED_COOLDOWN_SECONDS)
                log.error("живой фид матча %s отклонён (%s) — пауза %.0f мин, "
                          "работаем по странице матча",
                          self.match_id, exc, cooldown / 60)
                await self._sleep(cooldown, stop)
            except FeedUnavailable as exc:
                self.connected = False
                attempt += 1
                delay = min(2 ** attempt, MAX_BACKOFF_SECONDS) * random.uniform(0.8, 1.2)
                log.warning("живой фид матча %s оборвался (%s), переподключение через %.0fs",
                            self.match_id, exc, delay)
                await self._sleep(delay, stop)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - воркер не имеет права уронить процесс
                self.connected = False
                log.exception("непредвиденный сбой живого фида матча %s", self.match_id)
                await self._sleep(30, stop)
            finally:
                self.connected = False
                await client.close()

    async def _consume(self, client: ScorebotClient, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                packets = await client.poll()
            except FeedIdle:
                # Фид молчит — на карте пауза или идёт перерыв между картами.
                # Соединение живо, переподключаться не нужно.
                log.debug("живой фид матча %s молчит, опрашиваем снова", self.match_id)
                continue
            for frame in frames_from_packets(packets):
                events = self.machine.apply(self.match_id, frame)
                for event in events:
                    self.notifier.enqueue(event)
                await self._refresh_live_message(frame, events)

    async def _refresh_live_message(self, frame, events) -> None:
        """Живое сообщение перерисовывается после apply, чтобы в финальной
        правке уже стоял счёт серии с учётом только что взятой карты."""
        if self.messenger is None:
            return
        snapshot = self.machine.snapshot(self.match_id, frame)
        if not snapshot:
            return
        if any(event.type == "E6" for event in events):
            await self.messenger.finalize(self.match_id, snapshot)
        else:
            await self.messenger.update(self.match_id, snapshot)

    @staticmethod
    async def _sleep(seconds: float, stop: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


class LiveSupervisor:
    """Поднимает и гасит воркеры под идущие матчи.

    Матчей может идти несколько одновременно — у этой команды такое бывает,
    поэтому один воркер на матч, а не один на сервис.
    """

    def __init__(self, storage: Storage, config: Config, notifier: Notifier,
                 messenger: Optional[LiveMessenger] = None):
        self.storage = storage
        self.config = config
        self.notifier = notifier
        self.messenger = messenger
        self._workers: Dict[int, LiveWorker] = {}
        self._tasks: Dict[int, asyncio.Task] = {}
        self._stops: Dict[int, asyncio.Event] = {}

    @property
    def any_connected(self) -> bool:
        return any(worker.connected for worker in self._workers.values())

    def connected_matches(self) -> Dict[int, bool]:
        return {match_id: worker.connected for match_id, worker in self._workers.items()}

    def ensure(self, match_id: int, url: str) -> None:
        if match_id in self._tasks and not self._tasks[match_id].done():
            return
        stop = asyncio.Event()
        worker = LiveWorker(self.storage, self.config, self.notifier, match_id, url,
                            messenger=self.messenger)
        self._workers[match_id] = worker
        self._stops[match_id] = stop
        self._tasks[match_id] = asyncio.create_task(
            worker.run(stop), name=f"live-{match_id}")
        log.info("поднят живой фид для матча %s", match_id)

    def release(self, match_id: int) -> None:
        stop = self._stops.pop(match_id, None)
        if stop is not None:
            stop.set()
        task = self._tasks.pop(match_id, None)
        if task is not None:
            task.cancel()
        self._workers.pop(match_id, None)
        log.info("живой фид для матча %s остановлен", match_id)

    def reconcile(self, live_match_ids: Dict[int, str]) -> None:
        """Привести набор воркеров в соответствие с идущими матчами."""
        for match_id, url in live_match_ids.items():
            self.ensure(match_id, url)
        for match_id in list(self._tasks):
            if match_id not in live_match_ids:
                self.release(match_id)

    async def shutdown(self) -> None:
        # Список собирается ДО release(): release удаляет задачу из _tasks,
        # поэтому раньше здесь всегда оказывался пустой список, gather не
        # вызывался и завершения воркеров никто не ждал. Их finally с закрытием
        # сессии не успевал отработать до закрытия цикла событий.
        tasks = list(self._tasks.values())
        for match_id in list(self._tasks):
            self.release(match_id)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
