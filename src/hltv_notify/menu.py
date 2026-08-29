"""Инлайн-меню бота.

Кнопки нужны там, где иначе пришлось бы набирать руками то, что бот и так
знает: id команды, список типов событий, набор интервалов. Текстовые команды
никуда не делись — они короче, когда точно знаешь, чего хочешь.

Данные кнопки (`callback_data`) ограничены 64 байтами, поэтому схема короткая:

    m:main | m:status | m:live | m:next | m:teams | m:rem   разделы
    t:<id>            меню команды
    t:<id>:x:<TYPE>   переключить глушение типа
    t:<id>:rm         перестать отслеживать
    r:<minutes>       переключить напоминание
    p:on | p:off      пауза
"""

from __future__ import annotations

from typing import Dict, List, Optional

# Типы событий, которые имеет смысл глушить поштучно. Служебные тревоги (E8)
# сюда не входят намеренно: их глушить — значит не узнать, что сервис ослеп.
MUTABLE = (
    ("E10", "напоминание"),
    ("E1", "новый матч"),
    ("E2", "перенос"),
    ("E3", "отмена"),
    ("E4", "начало матча"),
    ("E5", "начало карты"),
    ("E6", "конец карты"),
    ("E7", "конец матча"),
    ("E9", "мультикилл"),
)

REMINDER_PRESETS = (10, 15, 30, 60, 120)


def keyboard(rows: List[List[tuple]]) -> Dict:
    """Строки кнопок вида (подпись, данные)."""
    return {"inline_keyboard": [
        [{"text": text, "callback_data": data} for text, data in row]
        for row in rows if row
    ]}


def main(paused: bool) -> Dict:
    return keyboard([
        [("📊 Состояние", "m:status"), ("🔴 Сейчас", "m:live")],
        [("📅 Ближайшие", "m:next"), ("⭐ Команды", "m:teams")],
        [("⏰ Напоминания", "m:rem")],
        [("🔔 Включить уведомления", "p:off")] if paused
        else [("🔕 Тишина", "p:on")],
    ])


def back(target: str = "m:main") -> List[tuple]:
    return [("← Назад", target)]


def teams(rows) -> Dict:
    """Список команд: каждая — кнопка, ведущая в её меню."""
    buttons = [[(row["name"] if row["enabled"] else f"{row['name']} (выкл)",
                 f"t:{row['team_id']}")] for row in rows]
    return keyboard(buttons + [back()])


def team(team_id: int, name: str, muted: List[str], enabled: bool) -> Dict:
    """Меню одной команды: галочки по типам событий и удаление."""
    toggles = []
    row: List[tuple] = []
    for code, label in MUTABLE:
        mark = "🔕" if code in muted else "🔔"
        row.append((f"{mark} {label}", f"t:{team_id}:x:{code}"))
        if len(row) == 2:
            toggles.append(row)
            row = []
    if row:
        toggles.append(row)

    tail = [("▶️ Включить", f"t:{team_id}:on")] if not enabled else \
           [("✖️ Перестать отслеживать", f"t:{team_id}:rm")]
    return keyboard(toggles + [tail, back("m:teams")])


def reminders(active: List[int]) -> Dict:
    """Пресеты интервалов: нажатие добавляет или убирает."""
    row: List[tuple] = []
    rows: List[List[tuple]] = []
    for minutes in REMINDER_PRESETS:
        mark = "✅" if minutes in active else "➕"
        label = f"{minutes} мин" if minutes < 60 else f"{minutes // 60} ч"
        row.append((f"{mark} {label}", f"r:{minutes}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return keyboard(rows + [back()])


def parse(data: str) -> Optional[tuple]:
    """`callback_data` → (действие, аргументы). None — не наш формат."""
    if not data:
        return None
    parts = data.split(":")
    return (parts[0], parts[1:]) if parts else None
