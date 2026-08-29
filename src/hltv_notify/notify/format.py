"""Рендер сообщений.

Каждое сообщение самодостаточно: из него понятно, о какой команде, каком
сопернике, каком турнире и какой карте речь, без листания истории чата.
Время — в часовом поясе пользователя; хранится всё в UTC, конвертация только
здесь. Названия месяцев свои, чтобы не зависеть от локали в контейнере.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from ..models import Event

MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн",
          "июл", "авг", "сен", "окт", "ноя", "дек"]
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def to_local(value, tz_name: str) -> datetime:
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(tz_name))


def human_time(value, tz_name: str, *, with_date: bool = True) -> str:
    dt = to_local(value, tz_name)
    clock = f"{dt.hour:02d}:{dt.minute:02d}"
    if not with_date:
        return clock
    return f"{WEEKDAYS[dt.weekday()]} {dt.day} {MONTHS[dt.month - 1]}, {clock}"


def escape(text: Optional[str]) -> str:
    return html.escape(text or "", quote=False)


_esc = escape


def _link(url: str, title: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{_esc(title)}</a>'


def render(event: Event, *, team_name: str, tz_name: str) -> str:
    """Событие → готовый текст в HTML-разметке Telegram."""
    payload = event.payload
    opponent = _esc(payload.get("opponent") or "TBD")
    event_name = _esc(payload.get("event_name"))
    url = payload.get("url") or ""
    # Имя команды берётся из события: отслеживаемых команд может быть
    # несколько, и у каждого матча своя. Значение из конфига — запасное.
    team = _esc(payload.get("team_name") or team_name)

    if event.type == "E1":
        when = human_time(payload["start_utc"], tz_name)
        lines = ["🆕 <b>Новый матч</b>", f"{team} — {opponent}"]
        if payload.get("placeholder"):
            lines.append("<i>соперник ещё не определён</i>")
        lines += [event_name, f"🕒 {when}", _link(url, "Страница матча")]
        return "\n".join(lines)

    if event.type == "E2":
        was = human_time(payload["old_start_utc"], tz_name)
        now = human_time(payload["start_utc"], tz_name)
        return "\n".join([
            "🕐 <b>Время матча изменилось</b>",
            f"{team} — {opponent}",
            event_name,
            f"Было: {was}",
            f"Стало: {now}",
            _link(url, "Страница матча"),
        ])

    if event.type == "E3":
        when = human_time(payload["start_utc"], tz_name)
        return "\n".join([
            "❌ <b>Матч отменён или отложен</b>",
            f"{team} — {opponent}",
            event_name,
            f"Планировался: {when}",
            _link(url, "Страница матча"),
        ])

    if event.type == "E4":
        best_of = payload.get("best_of")
        suffix = f" · BO{best_of}" if best_of else ""
        return "\n".join([
            "🔴 <b>Матч начался</b>",
            f"{team} — {opponent}",
            f"{event_name}{suffix}",
            _link(url, "Страница матча"),
        ])

    if event.type == "E5":
        return "\n".join([
            "🗺 <b>Карта началась</b>",
            f"{team} — {opponent}",
            f"Карта {payload.get('map_number')}: <b>{_esc(payload.get('map_name'))}</b>",
            f"Счёт по картам: {payload.get('series_team')}:{payload.get('series_opponent')}",
            event_name,
            _link(url, "Страница матча"),
        ])

    if event.type == "E6":
        ours = payload.get("score_team")
        theirs = payload.get("score_opponent")
        icon = "✅" if (ours or 0) > (theirs or 0) else "❌"
        overtime = " (овертайм)" if payload.get("overtime") else ""
        return "\n".join([
            f"{icon} <b>Карта {payload.get('map_number')} сыграна</b>",
            f"{_esc(payload.get('map_name'))} — <b>{ours}:{theirs}</b>{overtime}",
            f"{team} — {opponent}",
            f"Счёт по картам: <b>{payload.get('series_team')}:{payload.get('series_opponent')}</b>",
            event_name,
            _link(url, "Страница матча"),
        ])

    if event.type == "E7":
        ours = payload.get("series_team", 0)
        theirs = payload.get("series_opponent", 0)
        icon = "🏆" if payload.get("won") else "💀"
        lines = [
            f"{icon} <b>Матч завершён</b>",
            f"<b>{team} {ours}:{theirs} {opponent}</b>",
            event_name,
        ]
        for item in payload.get("maps", []):
            overtime = " (OT)" if item.get("overtime") else ""
            lines.append(f"   {_esc(item['name'])} — "
                         f"{item['score_team']}:{item['score_opponent']}{overtime}")
        lines.append(_link(url, "Страница матча"))
        return "\n".join(lines)

    if event.type == "E9":
        kills = payload.get("kills", 0)
        icon = "🔥" if kills < 5 else "💥"
        headline = "ЭЙС" if kills >= 5 else f"{kills} фрага в раунде"
        return "\n".join([
            f"{icon} <b>{_esc(payload.get('nick'))} — {headline}</b>",
            f"{_esc(payload.get('map_name'))}, раунд {payload.get('round')} · "
            f"счёт {payload.get('score_team')}:{payload.get('score_opponent')}",
            f"{team} — {opponent}",
            _link(url, "Смотреть матч"),
        ])

    if event.type == "E8R":
        return "\n".join([
            "✅ <b>Восстановилось</b>",
            _esc(payload.get("reason")),
            f"<i>{_esc(payload.get('detail'))}</i>",
        ])

    if event.type == "E8":
        lines = [
            "⚠️ <b>Сервис деградировал</b>",
            _esc(payload.get("reason")),
            f"<i>{_esc(payload.get('detail'))}</i>",
        ]
        # Ссылка на матч, если тревога о конкретном матче: чтобы можно было
        # сразу пойти и посмотреть глазами, а не искать его руками.
        if url:
            lines.append(_link(url, "Страница матча"))
        return "\n".join(lines)

    return f"{_esc(event.type)}: {_esc(str(payload))}"


ROUND_STATE_LABELS = {
    "warmup": "разминка",
    "freezePeriod": "закупка",
    "started": "идёт раунд",
    "ended": "раунд закончен",
}


def render_live(snapshot: dict, *, team_name: str) -> str:
    """Живое сообщение на карту: одно на карту, обновляется по ходу игры.

    Оно намеренно короткое: его перерисовывают каждые несколько секунд, и
    длинный текст в истории чата превращается в стену.
    """
    team = escape(snapshot.get("team_name") or team_name)
    opponent = escape(snapshot.get("opponent") or "TBD")
    map_name = escape(snapshot.get("map_name"))
    state = ROUND_STATE_LABELS.get(snapshot.get("round_state"), "")
    tail = f" · {state}" if state else ""
    return "\n".join([
        f"🎯 <b>{team} {snapshot['score_team']}:{snapshot['score_opponent']} {opponent}</b>",
        f"Карта {snapshot.get('map_number')}: {map_name} · раунд {snapshot.get('round')}{tail}",
        f"Счёт по картам: {snapshot.get('series_team')}:{snapshot.get('series_opponent')}",
        _link(snapshot.get("url") or "", "Страница матча"),
    ])
