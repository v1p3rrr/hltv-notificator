"""Кому адресовано то, что мы собрались отправить.

Единственное место, где решается этот вопрос. Раньше его решали двое —
очередь событий и живое сообщение, — и они разошлись: очередь про паузу знала,
живое сообщение нет, поэтому поставивший `/pause` продолжал получать счёт по
ходу карты. Правило «пауза проверяется в двух местах» неисполнимо; правильный
вывод — чтобы место было одно.

Отсюда же берётся вторая половина того же дефекта: ветка «у матча нет связей с
командами» раньше отдавала чат из конфига напрямую, минуя и список
подписчиков, и паузу.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

# (чат, команды этого чата в матче). Пустой список команд — «показать от лица
# самого матча»: либо матч ещё не связан с командами, либо это одиночный режим.
Audience = List[Tuple[str, List[int]]]


def active_subscribers(storage) -> List[str]:
    """Подписчики, которым сейчас можно писать: включённые и не на паузе."""
    return [chat for chat in storage.subscriber_ids()
            if not storage.subscriber_paused(chat)]


def _fallback(storage, config) -> Audience:
    """Одиночный режим: подписчиков в базе нет вовсе — пишем в чат из конфига.

    Если подписчики есть, но все молчат, возвращаем пустоту: это и есть смысл
    паузы. Раньше здесь стоял безусловный `config.chat_id`, и владелец получал
    уведомления, поставив себе `/pause`.
    """
    if storage.subscribers():
        return []
    return [(config.main_chat_id, [])]


def service_audience(storage, config) -> Audience:
    """Служебные тревоги (деградация, восстановление) — всем, кто на связи.

    Они не про команды, а про то, что сервис ослеп, поэтому глушению по типам
    не подлежат. Паузе — подлежат: человек попросил тишины.
    """
    subscribers = active_subscribers(storage)
    if not subscribers:
        return _fallback(storage, config)
    return [(chat, []) for chat in subscribers]


def match_audience(storage, config, match_id: int, *,
                   teams: Optional[Sequence[int]] = None) -> Audience:
    """Кому интересен этот матч и от лица каких его команд показывать.

    `teams` переопределяет участников — так адресуется мультикилл: он касается
    только тех, кто следит за командой самого игрока.
    """
    subscribers = active_subscribers(storage)
    if not subscribers:
        return _fallback(storage, config)

    if teams is None:
        teams = storage.match_team_ids(match_id)
    if not teams:
        # Матч ещё не связан ни с одной командой — так выглядит база сразу
        # после обновления, пока страница команды не опрошена заново. Показываем
        # всем, кто на связи: это честнее, чем отдать его владельцу из конфига
        # мимо и списка подписчиков, и паузы.
        return [(chat, []) for chat in subscribers]

    allowed = set(subscribers)
    by_chat: dict = {}
    for team_id in teams:
        for chat in storage.subscribers_tracking(team_id):
            if chat in allowed:
                by_chat.setdefault(chat, []).append(team_id)
    return list(by_chat.items())
