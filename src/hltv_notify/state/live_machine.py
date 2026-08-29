"""Переходы по кадрам живого фида: E5 и мгновенный E6.

Ради чего всё: секция карт на странице матча обновляется с задержкой, поэтому
E6 по странице приходит не в момент победного раунда. Фид знает счёт сразу,
и решение принимается по счёту — правило в hltv_notify.scoring, пороги
считаются от формата, который присылает сам фид.

Два свойства фида, из-за которых события рождаются ТОЛЬКО на переходах:
  * кадр scoreboard приходит по нескольку раз в секунду и всегда целиком;
  * при каждом подключении прилетает полное состояние заново, а в логе — ещё
    и бэклог уже случившегося.
Поэтому решения строятся на сравнении с сохранённым состоянием, а не на факте
получения кадра. И поэтому же log не используется вовсе.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from ..config import Config
from ..models import Event, MatchState
from ..scoring import map_completed
from ..sources.scorebot import LiveFrame, PlayerLine
from .db import Storage
from .multikill import MultikillTracker

log = logging.getLogger(__name__)

SOURCE = "scorebot"


def normalize_map_name(name: str) -> str:
    """`de_mirage` → `Mirage`.

    Фид даёт внутренние имена карт, страница матча — человеческие. Хранить и
    сравнивать надо в одном виде, иначе смена источника выглядела бы как смена
    карты и порождала ложный E5.
    """
    cleaned = (name or "").strip()
    for prefix in ("de_", "cs_"):
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    return cleaned[:1].upper() + cleaned[1:] if cleaned else ""


class LiveMachine:
    def __init__(self, storage: Storage, config: Config):
        self.storage = storage
        self.config = config
        # Трекер на КАЖДУЮ отслеживаемую команду матча: если отслеживаемые
        # команды играют друг против друга, четвёрка игрока каждой из них —
        # свой самостоятельный хайлайт, и глушить одну ради другой нельзя.
        # Живут в памяти воркера и переживают реконнекты внутри него.
        self._multikill: Dict[int, MultikillTracker] = {}

    def _tracker(self, team_id: int) -> MultikillTracker:
        if team_id not in self._multikill:
            self._multikill[team_id] = MultikillTracker(self.config.multikill_threshold)
        return self._multikill[team_id]

    # ------------------------------------------------------------------

    def apply(self, match_id: int, frame: LiveFrame) -> List[Event]:
        team_id = self.storage.canonical_team(match_id) or self.config.team_id
        ours, theirs = frame.our_score(team_id)
        if ours is None or theirs is None:
            # Записывать чужой счёт опаснее, чем промолчать. Но шуметь стоит
            # только когда id команд проставлены и просто не наши: пока фид их
            # не заполнил, это обычные переходные кадры между картами.
            if frame.ct_team_id or frame.t_team_id:
                log.warning("в кадре матча %s нет команды %s — кадр отброшен",
                            match_id, team_id)
            else:
                log.debug("переходный кадр матча %s без команд — пропущен", match_id)
            return []

        map_name = normalize_map_name(frame.map_name)
        if not map_name:
            return []

        recorded = {row["map_name"]: row["map_number"]
                    for row in self.storage.map_results(match_id)}
        if map_name in recorded:
            # Карта уже записана как сыгранная: фид ещё какое-то время
            # присылает её финальный счёт, реагировать не на что.
            return []

        state_row = self.storage.get_state(match_id)
        # Читаем СВОЮ памятку, а не current_map_name: то поле пишут обе машины,
        # и страница матча кладёт туда первую несыгранную, то есть ПРЕДСТОЯЩУЮ
        # карту. Читая его, живая машина видела «карта не менялась» ровно в тот
        # момент, когда карта начиналась, и E5 не рождался никогда.
        previous_map = state_row["live_map_name"] if state_row else None
        map_number = self._map_number(match_id, map_name, len(recorded))

        events: List[Event] = []
        events.extend(self._multikill_events(match_id, frame, map_number, map_name,
                                             ours, theirs))
        if self._is_new_map(previous_map, map_name, frame):
            events.append(self._event_e5(match_id, frame, map_number, map_name, len(recorded)))

        self.storage.set_map_format(match_id, frame.regulation, frame.overtime)
        verdict = map_completed(ours, theirs,
                                regulation=frame.regulation, overtime=frame.overtime)
        if verdict.completed:
            events.append(self._event_e6(match_id, frame, map_number, map_name,
                                         ours, theirs, verdict.overtime_number > 0))
            self.storage.record_map_result(
                match_id=match_id, map_number=map_number, map_name=map_name,
                score_team=ours, score_opponent=theirs,
                overtime=verdict.overtime_number > 0)
            log.info("матч %s: карта %d (%s) взята по счёту %d:%d, овертайм №%d",
                     match_id, map_number, map_name, ours, theirs, verdict.overtime_number)

        series = self._series(match_id)
        self.storage.set_state(
            match_id, MatchState.LIVE, source=SOURCE,
            current_map_number=map_number, current_map_name=map_name,
            current_map_score=f"{ours}-{theirs}",
            series_score=f"{series[0]}-{series[1]}")
        self.storage.set_live_map(match_id, map_name)
        return events

    # ------------------------------------------------------------------

    def snapshot(self, match_id: int, frame: LiveFrame) -> Optional[dict]:
        """Данные для живого сообщения со счётом.

        Отдельно от apply(): живое сообщение — это не событие. У него нет
        ключа идемпотентности и его не надо досылать после рестарта, его надо
        просто перерисовать текущим состоянием.
        """
        team_id = self.storage.canonical_team(match_id) or self.config.team_id
        ours, theirs = frame.our_score(team_id)
        if ours is None or theirs is None:
            return None
        map_name = normalize_map_name(frame.map_name)
        if not map_name:
            return None
        recorded = self.storage.map_results(match_id)
        series = self._series(match_id)
        row = self.storage.get_match(match_id)
        return {
            "map_number": self._map_number(match_id, map_name, len(recorded)),
            "map_name": map_name,
            "score_team": ours,
            "score_opponent": theirs,
            "round": frame.current_round,
            "round_state": frame.round_state,
            "in_play": frame.in_play,
            "series_team": series[0],
            "series_opponent": series[1],
            "opponent": frame.opponent_name(team_id)
                        or (row["opponent_name"] if row else ""),
            "team_name": self.storage.team_name(team_id, self.config.team_name),
            "event_name": row["event_name"] if row else "",
            "url": row["url"] if row else "",
        }

    def _multikill_events(self, match_id: int, frame: LiveFrame, map_number: int,
                          map_name: str, ours: int, theirs: int) -> List[Event]:
        """Мультикилл игрока НАШЕЙ команды — чтобы успеть клипануть хайлайт."""
        if not self.config.multikill_alerts:
            return []
        # Все отслеживаемые участники матча, а не только каноническая команда:
        # если отслеживаемые команды играют друг против друга, четвёрка игрока
        # каждой из них — самостоятельный хайлайт.
        canonical = self.storage.canonical_team(match_id) or self.config.team_id
        tracked = self.storage.match_team_ids(match_id) or [canonical]
        events: List[Event] = []
        for tracked_team in tracked:
            events.extend(self._multikill_for_team(
                match_id, frame, map_number, map_name, ours, theirs, tracked_team))
        return events

    def _multikill_for_team(self, match_id: int, frame: LiveFrame, map_number: int,
                            map_name: str, ours: int, theirs: int,
                            tracked_team: int) -> List[Event]:
        taken = self._tracker(tracked_team).observe(
            map_name, frame.current_round, frame.round_state,
            frame.our_players(tracked_team))
        events: List[Event] = []
        for player, kills in taken:
            log.info("матч %s: %s взял %d фрагов в раунде %d на карте %s",
                     match_id, player.nick, kills, frame.current_round, map_name)
            events.append(Event(
                type="E9",
                idempotency_key=(f"E9:{match_id}:map:{map_number}"
                                 f":round:{frame.current_round}:{player.steam_id}:{kills}"),
                match_id=match_id,
                payload={
                    **self._context(match_id, frame),
                    "team_name": self.storage.team_name(tracked_team, self.config.team_name),
                    "nick": player.nick,
                    "kills": kills,
                    "map_number": map_number,
                    "map_name": map_name,
                    "round": frame.current_round,
                    "score_team": ours,
                    "score_opponent": theirs,
                },
            ))
        return events

    def _map_number(self, match_id: int, map_name: str, recorded_count: int) -> int:
        """Номер карты в серии.

        Фид присылает только название, номера у него нет. Берём его из состава
        карт, вычитанного со страницы матча. Считать «сколько карт уже
        записано плюс один» ненадёжно: страница обновляется с задержкой, и
        если сервис подключился к фиду посреди серии, предыдущая карта могла
        быть ещё не записана — вторая карта получила бы номер первой.
        """
        lineup = self.storage.map_lineup(match_id)
        for index, name in enumerate(lineup, start=1):
            if name and name.lower() == map_name.lower():
                return index
        return recorded_count + 1

    def _is_new_map(self, previous_map: Optional[str], map_name: str,
                    frame: LiveFrame) -> bool:
        """Карта началась.

        Если предыдущей карты в состоянии нет, значит матч мы только что взяли
        под наблюдение. Объявлять «карта началась» про карту, которая идёт уже
        двадцать раундов, поздно — поэтому в этом случае событие рождается
        только если карта действительно в самом начале.
        """
        if previous_map is None:
            return frame.current_round <= 1
        return previous_map != map_name

    def _series(self, match_id: int) -> Tuple[int, int]:
        ours = theirs = 0
        for row in self.storage.map_results(match_id):
            if row["score_team"] > row["score_opponent"]:
                ours += 1
            elif row["score_opponent"] > row["score_team"]:
                theirs += 1
        return ours, theirs

    def _url(self, match_id: int) -> str:
        row = self.storage.get_match(match_id)
        return row["url"] if row else ""

    def _context(self, match_id: int, frame: LiveFrame) -> dict:
        row = self.storage.get_match(match_id)
        team_id = self.storage.canonical_team(match_id) or self.config.team_id
        return {
            "team_name": self.storage.team_name(team_id, self.config.team_name),
            "opponent": frame.opponent_name(team_id)
                        or (row["opponent_name"] if row else ""),
            "event_name": row["event_name"] if row else "",
            "url": row["url"] if row else "",
        }

    # ------------------------------------------------------------------

    def _event_e5(self, match_id: int, frame: LiveFrame, map_number: int,
                  map_name: str, decided_before: int) -> Event:
        series = self._series(match_id)
        return Event(
            type="E5",
            idempotency_key=f"E5:{match_id}:map:{map_number}:started:{map_name}",
            match_id=match_id,
            payload={
                **self._context(match_id, frame),
                "map_number": map_number,
                "map_name": map_name,
                "series_team": series[0],
                "series_opponent": series[1],
            },
        )

    def _event_e6(self, match_id: int, frame: LiveFrame, map_number: int, map_name: str,
                  ours: int, theirs: int, overtime: bool) -> Event:
        series = self._series(match_id)
        # Счёт серии с учётом только что взятой карты.
        if ours > theirs:
            series = (series[0] + 1, series[1])
        elif theirs > ours:
            series = (series[0], series[1] + 1)
        return Event(
            type="E6",
            idempotency_key=f"E6:{match_id}:map:{map_number}:result:{ours}-{theirs}",
            match_id=match_id,
            payload={
                **self._context(match_id, frame),
                "map_number": map_number,
                "map_name": map_name,
                "score_team": ours,
                "score_opponent": theirs,
                "overtime": overtime,
                "series_team": series[0],
                "series_opponent": series[1],
            },
        )
