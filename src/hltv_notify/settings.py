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
from typing import Callable, Dict, List, Optional, Tuple, Union

from .streams import parse_languages

# What a setting's value IS. One tag rather than a flag per type: with a
# separate `boolean` and `languages` both could be true at once, and every
# consumer would have to decide which wins. Each of these is switched on in
# `describe`, `parse_value`, `range_hint` and `menu.settings_screen`.
NUMBER = "number"
BOOLEAN = "boolean"
LANGUAGES = "languages"

Value = Union[int, str]

# The on/off vocabulary, in one place because three consumers read it:
# `parse_value`, `is_reset` and the refusals they produce. Written out twice it
# drifts, and the way it drifts is silent — a word that stops being recognised
# as a word is not rejected, it is taken as DATA. That is exactly how
# `/settings streams_langs on` came to store the primary language "on", which
# no flag can match, so the block stopped filtering instead of turning on.
ON_WORDS = frozenset({"on", "true", "yes"})
OFF_WORDS = frozenset({"off", "false", "no", "none"})
# Only for a list: "any" is not "off", it is a value — every language is
# welcome. It reads better than the empty string a person cannot type.
ANY_WORDS = frozenset({"any", "all"})
RESET_WORDS = frozenset({"default", "reset"})


@dataclass(frozen=True)
class Setting:
    """One knob.

    Numbers and booleans are stored as INTEGER, a language list as TEXT — see
    `Storage.set_setting` / `set_text_setting`. The kind is what says which,
    and nothing else may guess.
    """

    name: str
    label: str            # on a button
    summary: str          # in /settings and /help
    default: Callable     # taken from the Config
    kind: str = NUMBER
    maximum: int = 99
    # The smallest value that is not "off" and that the service will really
    # honour. Usually 1, but the multikill tracker floors its threshold at 2,
    # so accepting 1 there would store a number the alerts ignore and report it
    # back as if it were in force. Zero is always allowed, which is why there
    # is no separate `minimum`: a field nothing reads is a trap, and this
    # registry had one for exactly one commit.
    smallest_on: int = 1
    presets: Tuple[int, ...] = ()
    unit: str = ""
    # How zero reads, and the word a person types for it. Almost always "off",
    # but `streams_count` uses zero for "every one of them" — and a setting
    # that reports "off" while showing all the streams would be describing
    # something the code does not do.
    zero_word: str = "off"

    # ------------------------------------------------------------------

    @property
    def textual(self) -> bool:
        return self.kind == LANGUAGES

    def clamp(self, value: int) -> int:
        if value <= 0:
            return 0
        return max(self.smallest_on, min(self.maximum, value))

    def describe(self, value: Value) -> str:
        """The value as a person reads it."""
        if self.kind == LANGUAGES:
            codes = parse_languages(str(value or ""))
            return ", ".join(codes) if codes else "any language"
        number = int(value or 0)
        if self.kind == BOOLEAN:
            return "on" if number else "off"
        if number <= 0:
            return self.zero_word
        return f"{number} {self.unit}".strip()

    def describe_short(self, value: Value) -> str:
        """The same, for a button, where the unit does not fit."""
        if self.kind == LANGUAGES or self.kind == BOOLEAN or int(value or 0) <= 0:
            return self.describe(value)
        return str(int(value))


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
        maximum=5, smallest_on=2, presets=(0, 3, 4, 5), unit="kills",
    ),
    Setting(
        name="comeback",
        label="Comeback",
        summary="Swing in the score difference worth a line on a finished map; 0 off",
        default=lambda c: c.comeback_rounds,
        maximum=16, presets=(0, 6, 9, 12), unit="round swing",
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
        kind=BOOLEAN, maximum=1, presets=(0, 1),
    ),
    Setting(
        name="overtime",
        label="Overtime",
        summary="A message at the start of every overtime",
        default=lambda c: 1 if c.overtime_alerts else 0,
        kind=BOOLEAN, maximum=1, presets=(0, 1),
    ),
    Setting(
        name="card",
        label="Live score card",
        summary="The message kept up to date round by round during a map",
        default=lambda c: 1 if c.live_message else 0,
        kind=BOOLEAN, maximum=1, presets=(0, 1),
    ),
    # Three knobs for the stream block, because they answer three different
    # questions: whether it appears at all, how long it is, and which
    # broadcasts are worth a tap.
    Setting(
        name="streams",
        label="Stream links",
        summary="Broadcast links under a multikill, so it can be clipped by hand",
        default=lambda c: 1 if c.stream_links else 0,
        kind=BOOLEAN, maximum=1, presets=(0, 1),
    ),
    Setting(
        name="streams_count",
        label="How many streams",
        summary="How many broadcasts to list; 0 lists every one of them",
        default=lambda c: c.stream_links_max,
        maximum=6, presets=(0, 2, 3, 4), unit="links", zero_word="all",
    ),
    Setting(
        name="streams_langs",
        label="Stream languages",
        summary="Languages worth a tap; others appear only when there are none",
        default=lambda c: ",".join(c.stream_language_list()),
        kind=LANGUAGES,
    ),
)

BY_NAME: Dict[str, Setting] = {item.name: item for item in SETTINGS}


def get(name: str) -> Optional[Setting]:
    return BY_NAME.get(name)


def default_for(config, name: str) -> Value:
    """The value a subscriber who has never touched this one gets."""
    item = BY_NAME.get(name)
    if item is None:
        return 0
    if item.textual:
        return ",".join(parse_languages(str(item.default(config) or "")))
    return item.clamp(int(item.default(config)))


def defaults(config) -> Dict[str, Value]:
    return {item.name: default_for(config, item.name) for item in SETTINGS}


def is_reset(item: Setting, raw: str) -> bool:
    """Does this word mean "back to whatever the service default is"?

    `default` and `reset` always do. For a LIST so does "on", and that is not
    a courtesy: a number takes "on" to mean the same thing (the -1 below), a
    boolean takes it as 1, and a list has nothing else it could mean — the
    block's own off switch is a different setting. Left to `parse_value` it
    would become the language "on".
    """
    text = (raw or "").strip().lower()
    return text in RESET_WORDS or (item.textual and text in ON_WORDS)


def parse_value(item: Setting, raw: str) -> Optional[Value]:
    """`"5"`, `"on"`, `"off"` -> a value. None means it could not be read.

    Words are accepted for every setting, not only the boolean ones: "off" is
    how a person says zero, and refusing it for `multikill` while accepting it
    for `half` would be a distinction only the code can see.
    """
    text = (raw or "").strip().lower()
    if not text:
        return None

    if item.textual:
        # "any" is the empty list: no language is privileged, so the block is
        # simply the most watched broadcasts whatever they speak. The off
        # words land here too — with no language preferred, the filter is off.
        if text in ANY_WORDS or text in OFF_WORDS:
            return ""
        # An on word is NOT a language, and it is not rejected here for
        # tidiness: two letters of the alphabet is all a language code has to
        # be, so "on" and "no" would pass the check below and be stored as
        # codes no flag can ever match — the filter would fall back to "every
        # broadcast in every language" while the reply said "on". `is_reset`
        # takes them as "back to the service default" before we get here; this
        # is the guard for any other caller.
        if text in ON_WORDS:
            return None
        codes = parse_languages(text)
        if not codes or not all(code.isalpha() and 2 <= len(code) <= 8 for code in codes):
            return None
        return ",".join(codes)

    if (text in ON_WORDS or text == "1") and item.kind == BOOLEAN:
        return 1
    # The synonyms belong to the WORD, not to the number. For `streams_count`
    # zero means "all", and letting "off" through would store the value that
    # lists every stream under the name of the one that lists none — while a
    # real off switch (`streams`) exists next to it.
    zero_words = {item.zero_word}
    if item.zero_word == "off":
        zero_words |= OFF_WORDS
    if text in zero_words:
        return 0
    if text in ON_WORDS:
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
    """What a person may type, in the words the refusal uses.

    Plain prose, never angle brackets: replies go out with parse_mode=HTML,
    and Telegram answers 400 to a tag it does not know — so `<off, or 2-5>`
    is a message that never arrives.
    """
    if item.textual:
        return "language codes such as en,ru — or the word any"
    if item.kind == BOOLEAN:
        return "on or off"
    return f"{item.zero_word}, or {item.smallest_on}-{item.maximum}"


def example(item: Setting) -> str:
    """A value worth showing in "change it like this"."""
    if item.textual:
        return "en,ru"
    return "on" if item.kind == BOOLEAN else str(item.smallest_on)


def summary_lines(values: Dict[str, Value]) -> List[str]:
    """`/settings` with no arguments."""
    return [f"<code>{item.name}</code> — {item.describe(values.get(item.name, 0))}"
            f"  ·  {item.summary}" for item in SETTINGS]
