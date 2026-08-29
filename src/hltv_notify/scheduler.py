"""Опрос расписания и выбор режима частоты.

Матч не должен опрашиваться в активном режиме круглосуточно: команда может не
играть неделями, и это норма, а не ошибка.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from .config import HLTV_BASE, Config
from .http import HltvHttp, SourceRejected, SourceUnavailable, jittered
from .models import Event
from .notify.outbox import Notifier
from .sources import team_page
from .sources.team_page import ParseError
from .state.db import Storage, iso, parse_iso, utcnow
from .state.machine import ScheduleMachine
from .watchdog import Watchdog

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
        self.watchdog = Watchdog(storage, config)
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
            # Флаг сбрасывается ПЕРЕД опросом, а не после. Иначе /check,
            # отданный пока опрос уже идёт, стирался этим сбросом: бот успевал
            # ответить «Проверяю расписание», а внеочередной проверки не было —
            # следующая случалась только через штатный интервал, до получаса.
            self._force.clear()
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001 - опрос не имеет права уронить процесс
                log.exception("непредвиденный сбой опроса расписания")

            self.mode = self.current_mode()
            delay = jittered(self.config.interval_for(self.mode))
            log.info("режим %s, следующий опрос через %.0fs", self.mode, delay)

            if self._force.is_set():
                # Просьба поступила во время опроса — обслуживаем её сразу.
                log.info("внеочередной опрос по команде /check")
                continue

            waiters = [asyncio.create_task(stop.wait()), asyncio.create_task(self._force.wait())]
            done, pending = await asyncio.wait(
                waiters, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if stop.is_set():
                return

    # ------------------------------------------------------------------

    async def poll_once(self) -> List[Event]:
        """Опрос расписания по КАЖДОЙ отслеживаемой команде.

        Страницы разных команд — независимые источники: неудача по одной не
        должна мешать остальным, поэтому ошибки собираются, а цикл идёт дальше.
        """
        teams = self.storage.tracked_teams()
        if not teams:
            log.warning("не задано ни одной отслеживаемой команды")
            return []

        produced: List[Event] = []
        failures: List[str] = []
        for team in teams:
            try:
                produced.extend(await self._poll_team(team))
            except (SourceRejected, SourceUnavailable) as exc:
                failures.append(f"{team['name']}: {exc}")
            except ParseError as exc:
                failures.append(f"{team['name']}: разбор страницы не удался: {exc}")

        if failures and len(failures) == len(teams):
            # Ни одна команда не прочиталась — это отказ источника, а не
            # частная неудача.
            produced.extend(self._handle_failure("; ".join(failures)))
        elif failures:
            log.error("часть команд не прочиталась: %s", "; ".join(failures))
        else:
            self.storage.set_meta(LAST_POLL_KEY, iso(utcnow()))
            self.storage.set_meta(LAST_ERROR_KEY, "")
            self.http.consecutive_failures = 0
            for event in self.watchdog.report_success("schedule"):
                self.notifier.enqueue(event)
                produced.append(event)
        return produced

    async def _poll_team(self, team) -> List[Event]:
        url = f"{HLTV_BASE}/team/{team['team_id']}/{team['slug']}"
        html = await self.http.get_text(url)
        try:
            entries = team_page.parse(html, team["team_id"])
        except ParseError:
            # Ноль матчей при HTTP 200 — это почти наверняка смена вёрстки.
            # Трактуем как отказ источника, а не как «матчей нет».
            self.storage.log_raw("team_page", url, "200/parse-error", html[:20000],
                                 self.config.raw_log_days)
            raise

        events = self.machine.apply(entries, team["team_id"])
        for event in events:
            self.notifier.enqueue(event)
        log.info("расписание %s: %d матчей, %d предстоящих, событий %d",
                 team["name"], len(entries), len(team_page.upcoming(entries)), len(events))
        return events

    # ------------------------------------------------------------------

    def _handle_failure(self, detail: str) -> List[Event]:
        """Решение о тревоге принимает сторож: она зависит не от числа попыток,
        а от того, сколько мы уже слепы и чем рискуем прямо сейчас."""
        self.storage.set_meta(LAST_ERROR_KEY, detail)
        log.error("опрос расписания не удался: %s", detail)
        events = self.watchdog.report_failure("schedule", detail)
        for event in events:
            self.notifier.enqueue(event)
        return events
