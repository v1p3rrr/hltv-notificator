"""Опрос расписания и выбор режима частоты.

Матч не должен опрашиваться в активном режиме круглосуточно: команда может не
играть неделями, и это норма, а не ошибка.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from .config import Config
from .http import HltvHttp, SourceRejected, SourceUnavailable, jittered
from .models import Event
from .notify.outbox import Notifier
from .sources import team_page
from .sources.team_page import ParseError
from .state.db import Storage, iso, parse_iso, utcnow
from .state.machine import ScheduleMachine

log = logging.getLogger(__name__)

LAST_POLL_KEY = "last_schedule_poll_utc"
LAST_ERROR_KEY = "last_schedule_error"


class SchedulePoller:
    def __init__(self, storage: Storage, config: Config, http: HltvHttp, notifier: Notifier):
        self.storage = storage
        self.config = config
        self.http = http
        self.notifier = notifier
        self.machine = ScheduleMachine(storage, config)
        self.mode = "idle"
        self._force = asyncio.Event()

    # ------------------------------------------------------------------

    def request_poll(self) -> None:
        """Внеочередной опрос по команде /check."""
        self._force.set()

    def current_mode(self, now: Optional[datetime] = None) -> str:
        now = now or utcnow()
        upcoming = self.storage.upcoming_matches(now)
        if not upcoming:
            return "idle"
        nearest = parse_iso(upcoming[0]["start_utc"])
        window = timedelta(minutes=self.config.prematch_window_minutes)
        return "prematch" if nearest - now <= window else "idle"

    # ------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001 - опрос не имеет права уронить процесс
                log.exception("непредвиденный сбой опроса расписания")

            self.mode = self.current_mode()
            delay = jittered(self.config.interval_for(self.mode))
            log.info("режим %s, следующий опрос через %.0fs", self.mode, delay)

            self._force.clear()
            waiters = [asyncio.create_task(stop.wait()), asyncio.create_task(self._force.wait())]
            done, pending = await asyncio.wait(
                waiters, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if stop.is_set():
                return

    # ------------------------------------------------------------------

    async def poll_once(self) -> List[Event]:
        url = self.config.team_url
        try:
            html = await self.http.get_text(url)
        except (SourceRejected, SourceUnavailable) as exc:
            return self._handle_failure(str(exc))

        try:
            entries = team_page.parse(html, self.config.team_id)
        except ParseError as exc:
            # Ноль матчей при HTTP 200 — это почти наверняка смена вёрстки.
            # Трактуем как отказ источника, а не как «матчей нет».
            self.storage.log_raw("team_page", url, "200/parse-error", html[:20000],
                                 self.config.raw_log_days)
            return self._handle_failure(f"разбор страницы команды не удался: {exc}")

        self.storage.set_meta(LAST_POLL_KEY, iso(utcnow()))
        self.storage.set_meta(LAST_ERROR_KEY, "")
        self.http.consecutive_failures = 0

        events = self.machine.apply(entries)
        for event in events:
            self.notifier.enqueue(event)
        log.info("расписание: %d матчей, %d предстоящих, событий %d",
                 len(entries), len(team_page.upcoming(entries)), len(events))
        return events

    # ------------------------------------------------------------------

    def _handle_failure(self, detail: str) -> List[Event]:
        self.storage.set_meta(LAST_ERROR_KEY, detail)
        log.error("опрос расписания не удался: %s", detail)

        if self.http.consecutive_failures < self.config.failures_before_alert:
            return []

        # Час в ключе не даёт слать «я ослеп» на каждой неудачной попытке,
        # но и не глушит проблему навсегда.
        bucket = utcnow().strftime("%Y-%m-%dT%H")
        event = Event(
            type="E8",
            idempotency_key=f"E8:schedule:unavailable:{bucket}",
            match_id=None,
            payload={
                "reason": "Расписание не читается",
                "detail": f"{self.http.consecutive_failures} неудачных попыток подряд. {detail}",
            },
        )
        self.notifier.enqueue(event)
        return [event]
