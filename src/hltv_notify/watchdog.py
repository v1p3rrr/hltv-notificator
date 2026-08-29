"""Сторож: сообщить, что уведомления перестали работать.

Смысл ровно один — если сервис ослеп в каком угодно месте, пользователь должен
об этом узнать и сходить посмотреть матч руками. Поэтому тревога поднимается
не мгновенно (короткий сбой чинится ретраями сам), но и не «когда-нибудь».

Срочность зависит от того, чем мы рискуем прямо сейчас:

* до старта матча меньше минуты, или на карте кому-то осталось три раунда до
  победы, или идёт овертайм — ждать нельзя, тревога через минуту;
* всё остальное — через `DEGRADED_ALERT_SECONDS` (по умолчанию 5 минут,
  настраивается до 10).

Одна тревога на один сбой: ключ идемпотентности содержит момент начала сбоя,
поэтому повторные проверки того же сбоя ничего не шлют, а новый сбой сообщится
заново. Восстановление сообщается отдельно — чтобы не гадать, прошло ли.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from .config import Config
from .models import Event, MatchState
from .scoring import rounds_to_win
from .state.db import Storage, iso, parse_iso, utcnow

log = logging.getLogger(__name__)

# Нижняя граница: даже в самой срочной ситуации даём минуту на ретраи, иначе
# тревога полетит от любого одиночного таймаута.
URGENT_SECONDS = 60.0
# Верхняя граница настройки: молчать дольше десяти минут бессмысленно, к этому
# времени матч уже успеет пройти мимо.
MAX_ALERT_SECONDS = 600.0

# Сколько раундов до победы считается «вот-вот всё решится».
DECISIVE_ROUNDS = 3
# Сколько после планового старта матч считается «вот-вот начнётся». Дальше
# этого окна молчащий матч перестаёт быть срочным: он мог и не состояться.
START_GRACE_MINUTES = 30

SUBSYSTEMS = {
    "schedule": "Расписание не читается",
    "match_page": "Страница матча не читается",
    "live_feed": "Живой фид не поднимается",
    "outbox": "Уведомления не уходят в Telegram",
}


def _since_key(subsystem: str) -> str:
    return f"degraded_since:{subsystem}"


def _detail_key(subsystem: str) -> str:
    return f"degraded_detail:{subsystem}"


class Watchdog:
    def __init__(self, storage: Storage, config: Config):
        self.storage = storage
        self.config = config

    # ------------------------------------------------------------------

    @property
    def normal_delay(self) -> float:
        return min(max(float(self.config.degraded_alert_seconds), URGENT_SECONDS),
                   MAX_ALERT_SECONDS)

    def urgency(self, now: Optional[datetime] = None) -> Tuple[float, str]:
        """Через сколько поднимать тревогу и почему именно столько."""
        now = now or utcnow()

        for row in self.storage.active_matches(now):
            if row["state"] != MatchState.LIVE:
                continue
            reason = self._match_urgency(row["match_id"])
            if reason:
                return URGENT_SECONDS, reason

        # Окно вокруг старта, а не только «до старта». Самый неприятный случай
        # — матч УЖЕ начался, а мы слепы и ещё не поняли, что он идёт: именно
        # тогда молчать пять минут дороже всего.
        for row in self.storage.active_matches(now):
            # Идущий матч сюда не относится: его мы как раз видим, а его
            # срочность уже оценена выше по счёту.
            if row["state"] in (MatchState.LIVE, MatchState.FINISHED):
                continue
            start = parse_iso(row["start_utc"])
            if start - now > timedelta(seconds=URGENT_SECONDS):
                continue
            if now - start > timedelta(minutes=START_GRACE_MINUTES):
                continue
            if start > now:
                return URGENT_SECONDS, "до старта матча меньше минуты"
            return URGENT_SECONDS, "матч должен был начаться, а мы его не видим"

        return self.normal_delay, "матч не на решающей стадии"

    def _match_urgency(self, match_id: int) -> Optional[str]:
        """Срочность по счёту идущей карты."""
        state = self.storage.get_state(match_id)
        if state is None or not state["current_map_score"]:
            return None
        try:
            ours, theirs = (int(part) for part in state["current_map_score"].split("-"))
        except (ValueError, AttributeError):
            return None

        regulation = state["regulation_rounds"] or 12
        overtime = state["overtime_rounds"] or 3

        # Овертайм: обе стороны добрались до конца регламента.
        if ours >= regulation and theirs >= regulation:
            return f"идёт овертайм при счёте {ours}:{theirs}"

        left = rounds_to_win(ours, theirs, regulation=regulation, overtime=overtime)
        if left <= DECISIVE_ROUNDS:
            return f"до конца карты {left} раунд(ов) при счёте {ours}:{theirs}"
        return None

    # ------------------------------------------------------------------

    def report_failure(self, subsystem: str, detail: str,
                       now: Optional[datetime] = None,
                       since: Optional[datetime] = None) -> List[Event]:
        """Подсистема не работает. Тревога — только когда сбой продержался.

        `since` передаёт тот, кто знает НАСТОЯЩЕЕ начало сбоя. Для очереди это
        момент создания старейшего застрявшего сообщения: заставлять её
        отсчитывать заново значило бы молчать лишний порог сверх того, что она
        уже простояла.
        """
        now = now or utcnow()
        since_raw = self.storage.get_meta(_since_key(subsystem))
        if not since_raw:
            self.storage.set_meta(_since_key(subsystem), iso(since or now))
            self.storage.set_meta(_detail_key(subsystem), detail)
            log.warning("подсистема %s не отвечает, отсчёт пошёл: %s", subsystem, detail)
            since_raw = iso(since or now)
            if since is None:
                return []

        self.storage.set_meta(_detail_key(subsystem), detail)
        since = parse_iso(since_raw)
        delay, reason = self.urgency(now)
        broken_for = now - since
        if broken_for < timedelta(seconds=delay):
            return []

        minutes = max(1, int(broken_for.total_seconds() // 60))
        return [Event(
            type="E8",
            # Ключ включает момент НАЧАЛА сбоя: одна тревога на один сбой,
            # но новый сбой сообщится заново.
            idempotency_key=f"E8:{subsystem}:down:{iso(since)}",
            match_id=None,
            payload={
                "reason": SUBSYSTEMS.get(subsystem, subsystem),
                "detail": (f"Не работает {minutes} мин и не починилось само. "
                           f"Порог {int(delay)} с, потому что {reason}. {detail}"),
                "url": self._match_url(),
            },
        )]

    def report_success(self, subsystem: str,
                       now: Optional[datetime] = None) -> List[Event]:
        """Подсистема ожила. Если о сбое сообщали — сообщаем и о конце."""
        since_raw = self.storage.get_meta(_since_key(subsystem))
        if not since_raw:
            return []
        now = now or utcnow()
        self.storage.set_meta(_since_key(subsystem), "")
        self.storage.set_meta(_detail_key(subsystem), "")

        since = parse_iso(since_raw)
        broken_for = now - since
        # О сбое, который никто не увидел, молчим и на выходе.
        if broken_for < timedelta(seconds=URGENT_SECONDS):
            log.info("подсистема %s ожила за %.0f с, тревоги не было",
                     subsystem, broken_for.total_seconds())
            return []

        minutes = max(1, int(broken_for.total_seconds() // 60))
        return [Event(
            type="E8R",
            idempotency_key=f"E8R:{subsystem}:up:{iso(since)}",
            match_id=None,
            payload={
                "reason": SUBSYSTEMS.get(subsystem, subsystem),
                "detail": f"Снова работает. Простой составил {minutes} мин.",
            },
        )]

    # ------------------------------------------------------------------

    def check_outbox(self, now: Optional[datetime] = None) -> List[Event]:
        """Очередь не разгребается — значит Telegram не принимает.

        Тревога об этом уйдёт в ту же застрявшую очередь, и это нормально: она
        доедет, когда связь вернётся, а до тех пор видна в /status и в логах.
        Молчать нельзя — иначе о немой доставке не узнает никто.
        """
        now = now or utcnow()
        oldest = self.storage.oldest_pending_utc()
        if oldest is None:
            return self.report_success("outbox", now)
        stuck_for = now - parse_iso(oldest)
        delay, _ = self.urgency(now)
        if stuck_for < timedelta(seconds=delay):
            return []
        return self.report_failure(
            "outbox", f"старейшее сообщение ждёт {int(stuck_for.total_seconds() // 60)} мин",
            now, since=parse_iso(oldest))

    def check_live_feed(self, connected: dict, now: Optional[datetime] = None) -> List[Event]:
        """Идёт матч, а фида нет. Опрос страницы при этом работает, поэтому
        это не потеря всего — но E5, мультикиллы и скорость E6 теряются."""
        now = now or utcnow()
        live = [row for row in self.storage.active_matches(now)
                if row["state"] == MatchState.LIVE]
        if not live:
            return self.report_success("live_feed", now)
        if any(connected.get(row["match_id"]) for row in live):
            return self.report_success("live_feed", now)
        return self.report_failure(
            "live_feed", f"матчей идёт {len(live)}, ни к одному фид не подключён", now)

    def _match_url(self) -> str:
        """Ссылка на идущий матч, если он есть: чтобы из тревоги можно было
        сразу пойти и посмотреть счёт глазами."""
        for row in self.storage.active_matches():
            if row["state"] == MatchState.LIVE:
                return row["url"]
        upcoming = self.storage.upcoming_matches()
        return upcoming[0]["url"] if upcoming else ""

    async def run(self, stop, notifier, interval: float = 60.0) -> None:
        """Периодическая проверка того, что никто больше не проверяет.

        Опрос расписания и опрос матчей сообщают о своих сбоях сами. Очередь
        отправки — нет: она может тихо копиться, пока Telegram не принимает.
        """
        import asyncio

        while not stop.is_set():
            try:
                for event in self.check_outbox():
                    notifier.enqueue(event)
            except Exception:  # noqa: BLE001 - сторож не имеет права умирать
                log.exception("сбой сторожа")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    def degraded_subsystems(self) -> List[str]:
        return [name for name in SUBSYSTEMS
                if self.storage.get_meta(_since_key(name))]
