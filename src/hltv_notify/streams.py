"""Choosing which broadcasts to put under a multikill, and how to label them.

The point of the block is narrow: a 4k has just happened, and the owner wants
to be on a stream within seconds to clip it by hand. So the list is short, the
most watched come first, and a language nobody in the chat reads is worse than
nothing — it costs a tap and gives a broadcast that cannot be followed.

Everything here is pure: dictionaries in, dictionaries out. The selection is
run at RENDER time and not in the machine, because the event is born once for
everybody while the language list and the count belong to one reader — the
same division `format.comeback_line` follows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

# Unicode has no Twitch or Kick glyph, and Telegram's custom emoji are limited
# to bots that bought a username on Fragment. The brand colours are the closest
# honest thing, and they read at a glance in a list of three.
PROVIDER_ICON = {"twitch": "🟣", "kick": "🟢"}

# Below this many links the second-language quota does not apply: with two
# slots, spending one on a less watched broadcast to satisfy a rule costs more
# than it gives.
QUOTA_FROM = 3

WORLD = "🌍"


@dataclass(frozen=True)
class StreamPreference:
    """One reader's taste in broadcasts, resolved from their settings.

    Passed to the renderer as a single object rather than three parameters:
    they are only ever used together, and `None` in place of one says "this
    person has the block switched off" without a fourth flag.
    """

    limit: int = 3
    languages: Tuple[str, ...] = ()
    aliases: Mapping[str, str] = field(default_factory=dict)


def parse_languages(raw: str) -> List[str]:
    """`"en, ru"` -> `["en", "ru"]`. Order is kept, duplicates are not."""
    codes: List[str] = []
    for part in (raw or "").replace(";", ",").replace(" ", ",").split(","):
        code = part.strip().lower()
        if code and code not in codes:
            codes.append(code)
    return codes


def language_of(flag: str, aliases: Optional[Dict[str, str]] = None) -> str:
    """The language a flag stands for.

    A flag that is not in the alias table is its own language: `RU` -> `ru`,
    `BR` -> `br`. Only the exceptions need listing, and the big one is English,
    which arrives under `GB`, `US`, `WORLD` and every anglophone country.
    """
    flag = (flag or "").strip().upper()
    if not flag:
        return ""
    return (aliases or {}).get(flag, flag.lower())


def flag_emoji(flag: str) -> str:
    """`"RU"` -> 🇷🇺. `WORLD`, and anything that is not a country code, -> 🌍."""
    flag = (flag or "").strip().upper()
    if len(flag) != 2 or not flag.isalpha():
        return WORLD
    return "".join(chr(0x1F1E6 + ord(letter) - ord("A")) for letter in flag)


def pick(streams: Sequence[dict], *, primary: Sequence[str], limit: int,
         aliases: Optional[Dict[str, str]] = None) -> List[dict]:
    """The broadcasts to show, most watched first.

    Two rules, and they are not the same rule:

    * a language outside `primary` appears ONLY when the match has no broadcast
      in any primary language at all. One English stream beats five Portuguese
      ones even though it leaves the list short — a link that cannot be
      followed is not a fallback, it is noise;
    * from `QUOTA_FROM` links upwards, the list must not be all one language
      while another primary one exists further down. The last slot is given up
      for the most watched broadcast in a different primary language. Below
      that count there is no quota: with two slots the cost outweighs it.

    `limit` of 0 means every stream in the pool.
    """
    ordered = sorted(streams, key=lambda one: int(one.get("viewers") or 0),
                     reverse=True)
    wanted = set(primary)

    def speaks(one: dict) -> str:
        return language_of(one.get("flag"), aliases)

    in_primary = [one for one in ordered if speaks(one) in wanted]
    # No primary broadcast at all is the only case where the rest are of any
    # use; then they are simply the most watched, with no quota among them.
    pool = in_primary or list(ordered)
    if limit <= 0:
        return pool

    chosen = pool[:limit]
    if limit < QUOTA_FROM or not in_primary:
        return chosen

    languages = {speaks(one) for one in chosen}
    if len(languages) > 1:
        return chosen
    # `pool` is primary-only here, so whatever is found is a primary language
    # by construction — and the first one is the most watched of them.
    others = [one for one in pool[limit:] if speaks(one) not in languages]
    if not others:
        # Nobody else is casting in a language this reader wants; the list
        # stays as popularity left it rather than losing a slot for nothing.
        return chosen
    return chosen[:-1] + [others[0]]
