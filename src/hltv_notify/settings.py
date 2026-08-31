"""Per-subscriber settings: the taste knobs, editable from the chat.

The dividing line is not "could this be per person" — almost anything could.
It is **who pays for it**:

* a setting that only changes what ONE person's messages look like is theirs,
  and asking the owner to edit `.env` and restart the service for it is absurd
  when the whole interface is a chat;
* a setting that spends a SHARED budget — the request ceiling, Telegram's
  calls per second, the size of the database — stays in `.env`. There is one
  HLTV and one bot token; letting a subscriber widen a limit everyone draws
  from is not a preference, it is a way to break the service for the others.

So the thresholds live here and the polling intervals do not.

This is the third list of its kind, after `bot.COMMANDS` and `menu.MUTABLE`,
and it exists for the same reason: the command, the buttons, the `/help` text
and the defaults are all generated FROM it. Two hand-kept copies drift, and
that has already happened here once — `/help` never mentioned `/live`.

The environment variable does not disappear when a setting moves here. It
becomes the DEFAULT handed to a subscriber who has never touched it, exactly
like `REMINDERS` does for `/remind`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Setting:
    """One knob.

    Everything is stored as an integer, booleans included: one type in the
    database means one accessor, one parser and one migration story. `maximum`
    of 1 with `boolean` set is what makes it render as on/off.
    """

    name: str
    label: str            # on a button
    summary: str          # in /settings and /help
    default: Callable     # taken from the Config
    minimum: int = 0
    maximum: int = 99
    # The smallest value that is not "off" and that the service will really
    # honour. It is not always `minimum + 1`: the multikill tracker floors its
    # threshold at 2, so accepting 1 would store a number the alerts ignore and
    # report it back as if it were in force. 0 keeps the meaning "off".
    smallest_on: int = 1
    presets: Tuple[int, ...] = ()
    unit: str = ""
    boolean: bool = False

    def clamp(self, value: int) -> int:
        if value <= 0:
            return 0
        return max(self.smallest_on, min(self.maximum, value))

    def describe(self, value: int) -> str:
        """The value as a person reads it."""
        if self.boolean:
            return "on" if value else "off"
        if value <= 0:
            return "off"
        return f"{value} {self.unit}".strip()

    def describe_short(self, value: int) -> str:
        """The same, for a button, where the unit does not fit."""
        if self.boolean or value <= 0:
            return self.describe(value)
        return str(value)


# Ordered as they appear in /settings and on the buttons.
SETTINGS: Tuple[Setting, ...] = (
    Setting(
        name="multikill",
        label="Multikill",
        summary="Alert from this many kills in a round; 0 turns it off",
        # One knob rather than two. MULTIKILL_ALERTS=false and a threshold
        # nobody can reach are the same thing to a reader, and a switch that
        # silently overrides a number is exactly the kind of pair that leaves
        # someone staring at a threshold of 4 wondering why nothing arrives.
        default=lambda c: c.multikill_threshold if c.multikill_alerts else 0,
        # smallest_on is 2 and not 1 because MultikillTracker raises its own
        # floor to 2: a "1" here would be a threshold the alerts never use.
        minimum=0, maximum=5, smallest_on=2, presets=(0, 3, 4, 5), unit="kills",
    ),
    Setting(
        name="comeback",
        label="Comeback",
        summary="Swing in the score difference worth a line on a finished map; 0 off",
        default=lambda c: c.comeback_rounds,
        minimum=0, maximum=16, presets=(0, 6, 9, 12), unit="round swing",
    ),
    # Two knobs and not one: a half happens on every map and is routine, an
    # overtime usually does not happen at all and is the reason someone wants
    # to be pulled back to the screen. Wanting the second without the first is
    # the normal case.
    Setting(
        name="half",
        label="Half time",
        summary="A message when the sides swap",
        default=lambda c: 1 if c.half_alerts else 0,
        minimum=0, maximum=1, presets=(0, 1), boolean=True,
    ),
    Setting(
        name="overtime",
        label="Overtime",
        summary="A message at the start of every overtime",
        default=lambda c: 1 if c.overtime_alerts else 0,
        minimum=0, maximum=1, presets=(0, 1), boolean=True,
    ),
    Setting(
        name="card",
        label="Live score card",
        summary="The message kept up to date round by round during a map",
        default=lambda c: 1 if c.live_message else 0,
        minimum=0, maximum=1, presets=(0, 1), boolean=True,
    ),
)

BY_NAME: Dict[str, Setting] = {item.name: item for item in SETTINGS}


def get(name: str) -> Optional[Setting]:
    return BY_NAME.get(name)


def default_for(config, name: str) -> int:
    """The value a subscriber who has never touched this one gets."""
    item = BY_NAME.get(name)
    return item.clamp(int(item.default(config))) if item else 0


def defaults(config) -> Dict[str, int]:
    return {item.name: default_for(config, item.name) for item in SETTINGS}


def parse_value(item: Setting, raw: str) -> Optional[int]:
    """`"5"`, `"on"`, `"off"` -> a number. None means it could not be read.

    Words are accepted for every setting, not only the boolean ones: "off" is
    how a person says zero, and refusing it for `multikill` while accepting it
    for `half` would be a distinction only the code can see.
    """
    text = (raw or "").strip().lower()
    if not text:
        return None
    if text in {"on", "true", "yes", "1"} and item.boolean:
        return 1
    if text in {"off", "false", "no", "none"}:
        return 0
    if text in {"on", "true", "yes"}:
        # A non-boolean turned on means "back to the default", which the caller
        # supplies; -1 is the signal for it.
        return -1
    if text.isdigit():
        value = int(text)
        if value == 0:
            return 0
        if item.smallest_on <= value <= item.maximum:
            return value
        # Out of range, or between "off" and the smallest value that works.
        return None
    return None


def range_hint(item: Setting) -> str:
    """What a person may type, in the words the refusal uses."""
    if item.boolean:
        return "on or off"
    return f"off, or {item.smallest_on}-{item.maximum}"


def summary_lines(values: Dict[str, int]) -> List[str]:
    """`/settings` with no arguments."""
    return [f"<code>{item.name}</code> — {item.describe(values.get(item.name, 0))}"
            f"  ·  {item.summary}" for item in SETTINGS]
