"""The bot's inline menu.

Buttons are there for the things you would otherwise have to type out even
though the bot already knows them: team ids, the list of event types, the set
of intervals. The text commands have not gone anywhere — they are shorter when
you know exactly what you want.

Button payloads (`callback_data`) are capped at 64 bytes, hence the terse
scheme:

    m:main | m:status | m:live | m:next | m:teams | m:rem   sections
    t:<id>            one team's menu
    t:<id>:x:<TYPE>   toggle muting of a type
    t:<id>:rm         stop tracking
    r:<minutes>       toggle a reminder
    p:on | p:off      pause
"""

from __future__ import annotations

from typing import Dict, List, Optional

from . import settings as prefs

# Event types worth muting one by one. Service alerts (E8) are deliberately not
# here: muting those means not finding out that the service has gone blind.
MUTABLE = (
    ("E10", "reminder"),
    ("E1", "new match"),
    ("E2", "reschedule"),
    ("E3", "cancellation"),
    ("E4", "match start"),
    ("E5", "map start"),
    ("E12", "half"),
    ("E13", "overtime"),
    ("E11", "map point"),
    ("E6", "map end"),
    ("E7", "match end"),
    ("E9", "multikill"),
)

REMINDER_PRESETS = (10, 15, 30, 60, 120)


def keyboard(rows: List[List[tuple]]) -> Dict:
    """Rows of buttons given as (label, payload)."""
    return {"inline_keyboard": [
        [{"text": text, "callback_data": data} for text, data in row]
        for row in rows if row
    ]}


def main(paused: bool) -> Dict:
    return keyboard([
        [("📊 Status", "m:status"), ("🔴 Live now", "m:live")],
        [("📅 Upcoming", "m:next"), ("⭐ Teams", "m:teams")],
        [("⏰ Reminders", "m:rem"), ("🎚 Settings", "m:set")],
        [("🔔 Turn notifications on", "p:off")] if paused
        else [("🔕 Quiet", "p:on")],
    ])


def back(target: str = "m:main") -> List[tuple]:
    return [("← Back", target)]


def teams(rows) -> Dict:
    """The team list: each one is a button leading into its own menu."""
    buttons = [[(row["name"] if row["enabled"] else f"{row['name']} (off)",
                 f"t:{row['team_id']}")] for row in rows]
    return keyboard(buttons + [back()])


def team(team_id: int, name: str, muted: List[str], enabled: bool) -> Dict:
    """One team's menu: per-event-type toggles and removal."""
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

    tail = [("▶️ Turn on", f"t:{team_id}:on")] if not enabled else \
           [("✖️ Stop tracking", f"t:{team_id}:rm")]
    return keyboard(toggles + [tail, back("m:teams")])


def reminders(active: List[int]) -> Dict:
    """Interval presets: tapping one adds or removes it."""
    row: List[tuple] = []
    rows: List[List[tuple]] = []
    for minutes in REMINDER_PRESETS:
        mark = "✅" if minutes in active else "➕"
        label = f"{minutes} min" if minutes < 60 else f"{minutes // 60} h"
        row.append((f"{mark} {label}", f"r:{minutes}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return keyboard(rows + [back()])


def settings_screen(values: Dict[str, int]) -> Dict:
    """One row per setting: its presets, with the active one ticked.

    The rows are generated from `settings.SETTINGS` rather than written out,
    for the same reason MUTABLE is: a knob added to the registry has to appear
    on the buttons by itself, or the buttons and the command drift apart.
    """
    rows: List[List[tuple]] = []
    for item in prefs.SETTINGS:
        current = values.get(item.name, 0)
        # A caption row, then the choices. One wide row would fit, but four
        # presets beside a label is unreadable on a phone.
        rows.append([(f"{item.label}: {item.describe(current)}", f"s:{item.name}")])
        rows.append([(("🔘 " if value == current else "○ ") + item.describe_short(value),
                      f"s:{item.name}:{value}") for value in item.presets])
    return keyboard(rows + [back()])


def parse(data: str) -> Optional[tuple]:
    """`callback_data` -> (action, args). None means it is not our format."""
    if not data:
        return None
    parts = data.split(":")
    return (parts[0], parts[1:]) if parts else None
