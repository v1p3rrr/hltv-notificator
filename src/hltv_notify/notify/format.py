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
    team = _esc(team_name)

    if event.type == "E1":
        when = human_time(payload["start_utc"], tz_name)
        head = f"🆕 <b>Новый матч</b>\n{team} — {opponent}"
        if payload.get("placeholder"):
            head += "\n<i>соперник ещё не определён</i>"
        return (f"{head}\n{event_name}\n🕒 {when}\n"
                f"{_link(url, 'Страница матча')}")

    if event.type == "E2":
        was = human_time(payload["old_start_utc"], tz_name)
        now = human_time(payload["start_utc"], tz_name)
        return (f"🕐 <b>Время матча изменилось</b>\n{team} — {opponent}\n{event_name}\n"
                f"Было: {was}\nСтало: {now}\n{_link(url, 'Страница матча')}")

    if event.type == "E3":
        when = human_time(payload["start_utc"], tz_name)
        return (f"❌ <b>Матч отменён или отложен</b>\n{team} — {opponent}\n{event_name}\n"
                f"Планировался: {when}\n{_link(url, 'Страница матча')}")

    if event.type == "E8":
        return (f"⚠️ <b>Сервис деградировал</b>\n{_esc(payload.get('reason'))}\n"
                f"<i>{_esc(payload.get('detail'))}</i>")

    return f"{_esc(event.type)}: {_esc(str(payload))}"
