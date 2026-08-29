"""Опрос страниц матчей: фактический старт, ход серии, завершение.

Живёт отдельно от опроса расписания и с другой частотой. Активен только
вокруг матчей: команда может не играть неделями, и круглосуточный активный
опрос был бы неуважением к источнику без всякой пользы.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import List, Optional

from .config import Config
from .http import HltvHttp, SourceRejected, SourceUnavailable, jittered
from .models import Event, MatchState
from .notify.outbox import Notifier
from .sources import match_page
from .sources.match_page import ParseError
from .state.db import Storage, iso, utcnow
from .state.match_machine import MatchMachine

log = logging.getLogger(__name__)

IDLE_RECHECK_SECONDS = 60.0
LAST_MATCH_POLL_KEY = "last_match_poll_utc"


class MatchPoller:
    def __init__(self, storage: Storage, config: Config, http: HltvHttp, notifier: Notifier,
                 supervisor=None):
        self.storage = storage
        self.config = config
        self.http = http
        self.notifier = notifier
        self.supervisor = supervisor
        self.machine = MatchMachine(storage, config)
        self.mode = "idle"
        self.live_feed_active = False

    # ------------------------------------------------------------------

    def active(self, now: Optional[datetime] = None):
        return self.storage.active_matches(
            now, lookahead_minutes=self.config.prematch_window_minutes)

    def _mode_for(self, rows) -> str:
        if any(row["state"] == MatchState.LIVE for row in rows):
            return "live_with_feed" if self.live_feed_active else "live"
        return "prematch" if rows else "idle"

    # ------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            rows = self.active()
            self._reconcile_live_feed(rows)
            self.mode = self._mode_for(rows)

            if rows:
                try:
                    await self.poll_once(rows)
                except Exception:  # noqa: BLE001 - опрос не имеет права уронить процесс
                    log.exception("непредвиденный сбой опроса матчей")
                # Ещё раз, уже по свежим состояниям: матч мог только что стать
                # LIVE, и ждать целый круг, чтобы поднять фид, значит потерять
                # минуту там, где вся затея ради скорости.
                self._reconcile_live_feed(self.active())
                self.mode = self._mode_for(self.active())
                delay = jittered(self.config.interval_for(self.mode))
            else:
                # Матчей рядом нет — ни одного запроса, только дешёвая проверка базы.
                delay = IDLE_RECHECK_SECONDS

            log.debug("опрос матчей: режим %s, активных %d, следующий цикл через %.0fs",
                      self.mode, len(rows), delay)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                continue

    # ------------------------------------------------------------------

    def _reconcile_live_feed(self, rows) -> None:
        """Живой фид поднимается только под идущие матчи.

        Он же определяет режим опроса страницы: пока фид на связи, страница
        нужна лишь для сверки и опрашивается заметно реже.
        """
        if self.supervisor is None:
            return
        live = {row["match_id"]: row["url"]
                for row in rows if row["state"] == MatchState.LIVE}
        self.supervisor.reconcile(live)
        self.live_feed_active = self.supervisor.any_connected

    async def poll_once(self, rows=None) -> List[Event]:
        rows = self.active() if rows is None else rows
        produced: List[Event] = []
        for row in rows:
            produced.extend(await self._poll_match(row))
        if rows:
            self.storage.set_meta(LAST_MATCH_POLL_KEY, iso(utcnow()))
        return produced

    async def _poll_match(self, row) -> List[Event]:
        match_id = row["match_id"]
        url = row["url"]
        try:
            html = await self.http.get_text(url)
        except (SourceRejected, SourceUnavailable) as exc:
            log.error("страница матча %s не читается: %s", match_id, exc)
            return self._degraded(f"Страница матча {match_id} не читается: {exc}")

        try:
            observation = match_page.parse(html, match_id)
        except ParseError as exc:
            self.storage.log_raw("match_page", url, "200/parse-error", html[:20000],
                                 self.config.raw_log_days)
            log.error("разбор страницы матча %s не удался: %s", match_id, exc)
            return self._degraded(f"Страница матча {match_id} не разобралась: {exc}")

        events = self.machine.apply(observation)
        for event in events:
            self.notifier.enqueue(event)
        if events:
            log.info("матч %s: события %s", match_id, [e.type for e in events])
        else:
            log.debug("матч %s: %s, серия %s", match_id, observation.status,
                      observation.series_score(self.config.team_id))
        return events

    # ------------------------------------------------------------------

    def _degraded(self, detail: str) -> List[Event]:
        if self.http.consecutive_failures < self.config.failures_before_alert:
            return []
        bucket = utcnow().strftime("%Y-%m-%dT%H")
        event = Event(
            type="E8",
            idempotency_key=f"E8:match_page:unavailable:{bucket}",
            match_id=None,
            payload={
                "reason": "Страница матча не читается",
                "detail": f"{self.http.consecutive_failures} неудачных попыток подряд. {detail}",
            },
        )
        self.notifier.enqueue(event)
        return [event]
