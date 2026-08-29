"""Переходы, порождаемые наблюдениями страницы матча: E4, E7 и детект зависания.

Как и в расписании, событие рождается на переходе состояния. Страница матча
опрашивается раз в минуту и присылает одно и то же — «увидели LIVE, шлём E4»
дало бы уведомление на каждом опросе.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from ..config import Config
from ..models import Event, MatchState
from ..sources import match_page
from ..sources.match_page import MapLine, MatchObservation
from .db import Storage, parse_iso, utcnow

log = logging.getLogger(__name__)

STATUS_TO_STATE = {
    match_page.STATUS_LIVE: MatchState.LIVE,
    match_page.STATUS_OVER: MatchState.FINISHED,
    match_page.STATUS_UPCOMING: MatchState.SCHEDULED,
}

TERMINAL = {MatchState.FINISHED, MatchState.CANCELLED}


def _overtime(line: MapLine) -> bool:
    """Овертайм — по числу половин, а не по арифметике счёта.

    Регламент овертаймов различается между турнирами, и завязываться на
    «больше 13 раундов» значит сломаться на первом же нестандартном формате.
    Половин больше двух — значит был овертайм.
    """
    if not line.halves:
        return False
    return line.halves.count(";") >= 2


class MatchMachine:
    def __init__(self, storage: Storage, config: Config):
        self.storage = storage
        self.config = config

    # ------------------------------------------------------------------

    def apply(self, observation: MatchObservation, now: Optional[datetime] = None) -> List[Event]:
        now = now or utcnow()
        team_id = self.config.team_id
        match_id = observation.match_id

        if observation.our_side(team_id) is None:
            # Страницу отдали не ту, либо разметка поменялась. Молча
            # интерпретировать такое опасно: можно записать чужой счёт.
            log.warning("на странице матча %s нет команды %s — наблюдение отброшено",
                        match_id, team_id)
            return []

        row = self.storage.get_match(match_id)
        state_row = self.storage.get_state(match_id)
        previous = state_row["state"] if state_row else MatchState.SCHEDULED
        # Именно первое наблюдение СО СТРАНИЦЫ МАТЧА, а не первое вообще:
        # строку состояния заводит опрос расписания, поэтому проверка
        # «state_row is None» здесь почти всегда ложна и карты, доигранные до
        # начала наблюдения, получали бы E6 задним числом.
        first_observation = state_row is None or state_row["last_source"] != "match_page"
        target = STATUS_TO_STATE.get(observation.status, previous)

        # Снимок ДО записи результатов: по нему видно, какие карты решились
        # именно сейчас. Иначе сравнивать было бы уже не с чем.
        known_maps = {r["map_number"] for r in self.storage.map_results(match_id)}
        seen_live = previous in (MatchState.LIVE, MatchState.MAP_LIVE, MatchState.MAP_BREAK)

        events: List[Event] = []
        ours, theirs = observation.series_score(team_id)
        current_map = self._current_map(observation)

        self.storage.set_state(
            match_id, target, source="match_page",
            current_map_number=current_map.number if current_map else None,
            current_map_name=current_map.name if current_map else None,
            series_score=f"{ours}-{theirs}",
        )
        self._store_map_results(observation, team_id)
        # Состав карт нужен живому фиду: он знает название карты, но не её
        # номер в серии. Записываем, как только вето сыграно.
        lineup = [line.name for line in observation.maps]
        if any(name and name.upper() != "TBA" for name in lineup):
            self.storage.set_map_lineup(match_id, lineup)

        if target == MatchState.LIVE and previous not in (MatchState.LIVE, *TERMINAL):
            events.append(self._event_e4(observation, row, team_id))

        discovered_finished = target == MatchState.FINISHED and not seen_live
        events.extend(self._map_events(
            observation, row, team_id, known_maps,
            silent=first_observation or discovered_finished))

        if target == MatchState.FINISHED and previous not in TERMINAL:
            if not seen_live:
                # Матч уже доигран, а «идёт» мы не застали. Слать E4 и E6
                # задним числом бессмысленно — сразу итог.
                log.info("матч %s обнаружен уже завершённым, E4 и E6 пропущены", match_id)
            events.append(self._event_e7(observation, row, team_id, ours, theirs))

        events.extend(self._check_stall(observation, team_id, target, now))
        return events

    # ------------------------------------------------------------------

    def _current_map(self, observation: MatchObservation) -> Optional[MapLine]:
        """Текущая карта — первая нерешённая с известным названием."""
        live = observation.live_map()
        if live is not None:
            return live
        for line in observation.maps:
            if not observation.is_final(line) and line.name and line.name.upper() != "TBA":
                return line
        final = observation.final_maps()
        return final[-1] if final else None

    def _map_events(self, observation: MatchObservation, row, team_id: int,
                    known_maps: set, *, silent: bool) -> List[Event]:
        """E6 — на переходе карты из нерешённой в решённую.

        Событие рождается один раз на карту: повторные опросы видят ту же
        карту уже в known_maps. Ключ идемпотентности включает счёт, поэтому
        даже исправление счёта на стороне HLTV не приведёт к молчанию.
        """
        events: List[Event] = []
        for line in observation.final_maps():
            if line.number in known_maps:
                continue
            if silent:
                # Карта была сыграна до того, как мы начали смотреть за матчем.
                # Это не переход, а состояние на момент знакомства.
                log.info("матч %s: карта %d уже была сыграна к моменту наблюдения, E6 пропущен",
                         observation.match_id, line.number)
                continue
            events.append(self._event_e6(observation, row, team_id, line))
        return events

    def _store_map_results(self, observation: MatchObservation, team_id: int) -> None:
        for line in observation.final_maps():
            ours, theirs = observation.map_score(line, team_id)
            if ours is None or theirs is None:
                continue
            self.storage.record_map_result(
                match_id=observation.match_id, map_number=line.number, map_name=line.name,
                score_team=ours, score_opponent=theirs, overtime=_overtime(line),
            )

    # ------------------------------------------------------------------

    def _check_stall(self, observation: MatchObservation, team_id: int,
                     state: str, now: datetime) -> List[Event]:
        """«Матч завис»: идёт, но ничего не меняется дольше порога.

        Технические паузы бывают долгими, поэтому порог, а не мгновенная
        тревога. Ключ включает отпечаток застывшего состояния: одно
        уведомление на одно зависание, но повторное зависание в другой точке
        матча сообщится снова.
        """
        if state != MatchState.LIVE:
            return []

        signature = observation.progress_signature(team_id)
        state_row = self.storage.get_state(observation.match_id)
        previous_hash = state_row["progress_hash"] if state_row else None
        since_raw = state_row["progress_since_utc"] if state_row else None

        if previous_hash != signature or since_raw is None:
            self.storage.set_progress(observation.match_id, signature, now)
            return []

        stalled_for = now - parse_iso(since_raw)
        if stalled_for < timedelta(minutes=self.config.stale_minutes):
            return []

        digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12]
        minutes = int(stalled_for.total_seconds() // 60)
        return [Event(
            type="E8",
            idempotency_key=f"E8:match:{observation.match_id}:stale:{digest}",
            match_id=observation.match_id,
            payload={
                "reason": "Матч завис",
                "detail": (f"Счёт и состояние не меняются {minutes} мин, "
                           f"страница матча всё ещё показывает LIVE."),
            },
        )]

    # ------------------------------------------------------------------

    def _event_e4(self, observation: MatchObservation, row, team_id: int) -> Event:
        opponent_id, opponent_name = observation.opponent(team_id)
        return Event(
            type="E4",
            idempotency_key=f"E4:{observation.match_id}:started",
            match_id=observation.match_id,
            payload={
                "opponent": opponent_name or (row["opponent_name"] if row else ""),
                "opponent_id": opponent_id,
                "event_name": observation.event_name or (row["event_name"] if row else ""),
                "best_of": observation.best_of,
                "url": row["url"] if row else "",
            },
        )

    def _event_e6(self, observation: MatchObservation, row, team_id: int,
                  line: MapLine) -> Event:
        our_score, their_score = observation.map_score(line, team_id)
        opponent_id, opponent_name = observation.opponent(team_id)
        # Счёт серии берётся на момент этой карты, а не итоговый: между двумя
        # опросами могут завершиться сразу две карты, и в сообщении о первой
        # итоговый счёт был бы враньём.
        series_ours, series_theirs = observation.series_after(line.number, team_id)
        return Event(
            type="E6",
            idempotency_key=(f"E6:{observation.match_id}:map:{line.number}"
                             f":result:{our_score}-{their_score}"),
            match_id=observation.match_id,
            payload={
                "opponent": opponent_name or (row["opponent_name"] if row else ""),
                "opponent_id": opponent_id,
                "event_name": observation.event_name or (row["event_name"] if row else ""),
                "map_number": line.number,
                "map_name": line.name,
                "score_team": our_score,
                "score_opponent": their_score,
                "overtime": _overtime(line),
                "halves": line.halves,
                "series_team": series_ours,
                "series_opponent": series_theirs,
                "url": row["url"] if row else "",
            },
        )

    def _event_e7(self, observation: MatchObservation, row, team_id: int,
                  ours: int, theirs: int) -> Event:
        opponent_id, opponent_name = observation.opponent(team_id)
        maps = []
        for line in observation.final_maps():
            our_score, their_score = observation.map_score(line, team_id)
            maps.append({
                "number": line.number,
                "name": line.name,
                "score_team": our_score,
                "score_opponent": their_score,
                "overtime": _overtime(line),
            })
        return Event(
            type="E7",
            idempotency_key=f"E7:{observation.match_id}:finished:{ours}-{theirs}",
            match_id=observation.match_id,
            payload={
                "opponent": opponent_name or (row["opponent_name"] if row else ""),
                "opponent_id": opponent_id,
                "event_name": observation.event_name or (row["event_name"] if row else ""),
                "series_team": ours,
                "series_opponent": theirs,
                "won": ours > theirs,
                "maps": maps,
                "url": row["url"] if row else "",
            },
        )
