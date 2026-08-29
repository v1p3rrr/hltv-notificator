"""Определение конца карты по счёту.

Зачем это нужно отдельно от страницы матча. Секция карт на HLTV обновляется с
задержкой — наблюдение на матче 2397091: при реальном счёте 12:11 в секции
стояло 5:7, то есть результат предыдущей половины. Уведомление «карта
закончилась» по странице приходит не в момент победного раунда, а когда HLTV
обновит статусы. Живой фид знает счёт по раундам сразу, поэтому решение о
конце карты принимается по счёту, а страница остаётся подтверждением.

Никаких «13 раундов» в коде. Формат приходит из самого источника:
`regulationHalfLength` и `overtimeHalfLength` в кадре scoreboard (наблюдалось
12 и 3), они же — в атрибутах `data-max-rounds-regulation` и
`data-max-rounds-overtime` на странице матча. Поэтому правило одинаково
работает и для MR12, и для устаревшего MR15, и для коротких форматов.

Как считается. Половина длится `regulation` раундов, значит регламент — это
2*regulation раундов, и победа в нём наступает на `regulation + 1`, если
соперник не добрал до `regulation`. Если оба дошли до `regulation` (12:12),
начинается овертайм: каждый овертайм — это две половины по `overtime`
раундов, и выиграть его значит взять `overtime + 1` из них. Отсюда пороги
13, затем 16, затем 19 и так далее.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

DEFAULT_REGULATION = 12
DEFAULT_OVERTIME = 3


@dataclass(frozen=True)
class MapVerdict:
    completed: bool
    overtime_number: int = 0

    def __bool__(self) -> bool:
        return self.completed


def _overtime_number(low: int, regulation: int, overtime: int) -> int:
    """Какой овертайм идёт сейчас, судя по счёту отстающей стороны.

    Считаем по меньшему счёту, а не по большему: победитель овертайма
    опережает соперника, и по большему номер определился бы неверно.

    Отстающий за один овертайм может набрать не больше `overtime` раундов,
    поэтому его счёт и говорит, сколько овертаймов позади. Граничный случай —
    ничья на потолке овертайма (15:15 при MR12/MR3): овертайм закончен
    вничью, идёт следующий, и цель уже 19, а не 16.
    """
    if low < regulation:
        return 0
    return (low - regulation) // overtime + 1


def map_completed(score_a: Optional[int], score_b: Optional[int], *,
                  regulation: int = DEFAULT_REGULATION,
                  overtime: int = DEFAULT_OVERTIME) -> MapVerdict:
    """Закончена ли карта при таком счёте.

    Возвращает вердикт, а не голый bool, чтобы вызывающий мог отличить победу
    в регламенте от победы в овертайме, не пересчитывая то же самое заново.
    """
    if score_a is None or score_b is None:
        return MapVerdict(False)
    if regulation < 1 or overtime < 1:
        return MapVerdict(False)

    high, low = max(score_a, score_b), min(score_a, score_b)
    if low < 0 or high < 0:
        return MapVerdict(False)

    # Никто ещё не взял решающий раунд регламента.
    if high < regulation + 1:
        return MapVerdict(False)

    # Победа в регламенте: соперник не дотянул до regulation.
    if low < regulation:
        return MapVerdict(True, 0)

    # Оба дошли до regulation — идут овертаймы.
    played = _overtime_number(low, regulation, overtime)
    threshold = regulation + played * overtime + 1
    if high >= threshold and low <= threshold - 2:
        return MapVerdict(True, played)
    return MapVerdict(False, played)


def rounds_to_win(score_a: int, score_b: int, *,
                  regulation: int = DEFAULT_REGULATION,
                  overtime: int = DEFAULT_OVERTIME) -> int:
    """Сколько раундов лидеру осталось до победы на карте.

    Нужно для сообщений вида «матчпоинт»: величина считается по тем же
    порогам, что и сам конец карты, поэтому расходиться они не могут.
    """
    # Та же защита, что и в map_completed: без неё формат с overtime=0 (поле
    # пропало в кадре и подставился ноль) уронил бы вызов делением на ноль.
    if regulation < 1 or overtime < 1:
        return 0
    high, low = max(score_a, score_b), min(score_a, score_b)
    if low < regulation:
        return max(0, regulation + 1 - high)
    played = _overtime_number(low, regulation, overtime)
    return max(0, regulation + played * overtime + 1 - high)
