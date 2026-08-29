"""Мультикиллы игроков отслеживаемой команды — алерт, чтобы успеть клипануть.

Считается по кадрам scoreboard, а НЕ по событиям Kill из лога. Причина та же,
по которой лог не используется нигде: при каждом подключении фид проигрывает
бэклог заново, и алерты посыпались бы за давно сыгранные раунды. В кадре у
каждого игрока лежат накопленные за карту фраги — значит достаточно запомнить
их на старте раунда и следить за приростом. Заодно это даёт алерт в момент
четвёртого фрага, а не в конце раунда.

Направление ошибок безопасное: после переподключения посреди раунда база
берётся заново, поэтому мультикилл может быть ПРОПУЩЕН, но не выдуман.
"""

from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Optional, Set, Tuple

from ..sources.scorebot import PlayerLine

log = logging.getLogger(__name__)

ACE = 5
WARMUP = "warmup"


class MultikillTracker:
    """Состояние на один матч. Живёт в памяти воркера.

    В базу не пишется намеренно: это данные одного раунда, они бессмысленны
    после рестарта, а защита от повторов и так стоит на ключе события.
    """

    def __init__(self, threshold: int = 4):
        self.threshold = max(2, threshold)
        self._key: Optional[Tuple[str, int]] = None
        self._baseline: Dict[str, int] = {}
        self._alerted: Set[Tuple[str, int]] = set()

    @property
    def levels(self) -> List[int]:
        """Пороги, на которых стоит дёрнуть: сам порог и эйс.

        Между ними не сообщаем: два сообщения на раунд — это уже шум, а вот
        «стало эйсом» посмотреть стоит.
        """
        return sorted({self.threshold, ACE})

    def observe(self, map_name: str, round_number: int, round_state: str,
                players: Iterable[PlayerLine]) -> List[Tuple[PlayerLine, int]]:
        """Игроки, которые ТОЛЬКО ЧТО взяли мультикилл, и число фрагов в раунде."""
        players = list(players)
        key = (map_name, round_number)

        if key != self._key:
            # Новый раунд: фиксируем точку отсчёта и забываем прошлые алерты.
            self._key = key
            self._baseline = {p.steam_id: p.kills for p in players}
            self._alerted = set()
            return []

        # В разминке фраги идут из дезматча и к раунду отношения не имеют.
        if round_state == WARMUP:
            self._baseline = {p.steam_id: p.kills for p in players}
            return []

        found: List[Tuple[PlayerLine, int]] = []
        for player in players:
            base = self._baseline.get(player.steam_id)
            if base is None:
                # Игрок появился в кадре посреди раунда (замена, реконнект).
                # Считать все его фраги за раунд нельзя — берём точку отсчёта.
                self._baseline[player.steam_id] = player.kills
                continue
            in_round = player.kills - base
            if in_round < self.threshold:
                continue
            crossed = [level for level in self.levels
                       if in_round >= level and (player.steam_id, level) not in self._alerted]
            if not crossed:
                continue
            # Отмечаем ВСЕ взятые пороги, а сообщаем один раз: игрок мог
            # прыгнуть с трёх сразу до эйса между двумя кадрами.
            for level in crossed:
                self._alerted.add((player.steam_id, level))
            found.append((player, in_round))
        return found
