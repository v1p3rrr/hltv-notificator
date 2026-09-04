"""Configuration: environment variables only, never secrets in code."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List
from pathlib import Path
from urllib.parse import urlparse

from .proxy import ProxySettings
from .streams import parse_languages

# The request-rate ceiling, hardcoded. Not raisable by config — see
# docs/operations.md.
HARD_MIN_REQUEST_INTERVAL_SECONDS = 30.0

HLTV_BASE = "https://www.hltv.org"

# Where the service is allowed to go at all. The list is closed and lives in
# code rather than config: match addresses come FROM THE HLTV PAGE, and without
# this check a poisoned record would send the request to a foreign host — the
# local network the service runs in included. The check sits at the network
# egress itself so it also catches records written to the database before the
# parser was fixed.
ALLOWED_HOSTS = frozenset({"www.hltv.org", "hltv.org", "scorebot-lb.hltv.org"})

log = logging.getLogger(__name__)

# A Telegram chat id is a number; for groups and channels it is negative.
_CHAT_ID_RE = re.compile(r"^-?\d+$")


def url_allowed(url: str) -> bool:
    """Whether this address may be requested.

    The scheme must be https and the host must be in ALLOWED_HOSTS. What is
    compared is the `hostname` from the parsed URL, not the start of the
    string: `https://www.hltv.org@evil/` starts out "correctly" yet leads to
    evil — that part is userinfo, not the host.
    """
    try:
        parts = urlparse(url or "")
    except ValueError:
        return False
    return parts.scheme == "https" and (parts.hostname or "").lower() in ALLOWED_HOSTS


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return raw if raw not in (None, "") else default


@dataclass(frozen=True)
class Config:
    # what we follow
    team_id: int = field(default_factory=lambda: _int("TEAM_ID", 12857))
    team_slug: str = field(default_factory=lambda: _str("TEAM_SLUG", "forze-reload"))
    team_name: str = field(default_factory=lambda: _str("TEAM_NAME", "FORZE Reload"))

    # Telegram
    bot_token: str = field(default_factory=lambda: _str("TELEGRAM_BOT_TOKEN", ""))

    # Who may use the bot. ONE variable, ids separated by commas:
    #     TELEGRAM_CHAT_ID=123456789,987654321
    # The first one is the main chat: the team from TEAM_ID is seeded there on
    # the first run, and messages go there while there are no subscribers yet.
    # The list is closed by default: the bot has a public address, and without
    # it anyone who finds the bot could command it.
    chat_id: str = field(default_factory=lambda: _str("TELEGRAM_CHAT_ID", ""))
    whitelist_only: bool = field(default_factory=lambda: _bool("TELEGRAM_WHITELIST_ONLY", True))

    # mode
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", True))
    timezone: str = field(default_factory=lambda: _str("TZ_DISPLAY", "Europe/Moscow"))
    log_level: str = field(default_factory=lambda: _str("LOG_LEVEL", "INFO"))
    db_path: Path = field(default_factory=lambda: Path(_str("DB_PATH", "data/hltv.db")))

    # polling intervals (seconds)
    poll_idle: int = field(default_factory=lambda: _int("POLL_IDLE_SECONDS", 1800))
    poll_prematch: int = field(default_factory=lambda: _int("POLL_PREMATCH_SECONDS", 180))
    poll_live: int = field(default_factory=lambda: _int("POLL_LIVE_SECONDS", 60))
    poll_live_with_feed: int = field(
        default_factory=lambda: _int("POLL_LIVE_WITH_FEED_SECONDS", 300))
    prematch_window_minutes: int = field(
        default_factory=lambda: _int("PREMATCH_WINDOW_MINUTES", 30))
    # How long a match that should have started keeps the schedule on the
    # frequent cadence. That is the window in which HLTV moves it — by five
    # minutes, then ten — and the only way to catch such a move is to keep
    # looking. Deliberately not the same thing as watchdog.START_GRACE_MINUTES,
    # which answers a different question: for how long being blind around this
    # match is still urgent.
    late_start_grace_minutes: int = field(
        default_factory=lambda: _int("LATE_START_GRACE_MINUTES", 60))

    # How many teams one subscriber may follow at once. The cost of a schedule
    # sweep is `distinct teams x 30 s` because of the request ceiling, so this
    # is the one setting that decides whether the service keeps up: at ten
    # teams a sweep already takes five minutes against the three that pre-match
    # mode would like. 0 removes the limit, and the protection with it.
    max_teams_per_subscriber: int = field(
        default_factory=lambda: _int("MAX_TEAMS_PER_SUBSCRIBER", 10))

    # Commands one chat may send per minute. Off by default: with the
    # whitelist on, the people who can reach the bot are people you chose, and
    # a limit that never fires is a limit nobody has tested. Turn it on before
    # opening the bot up.
    command_rate_limit: int = field(
        default_factory=lambda: _int("COMMAND_RATE_LIMIT", 0))

    # the live score message kept up to date during a map
    live_message: bool = field(default_factory=lambda: _bool("LIVE_MESSAGE", True))
    live_edit_seconds: int = field(default_factory=lambda: _int("LIVE_EDIT_SECONDS", 10))
    # How many card edits a second the service allows itself IN TOTAL. The
    # card is per subscriber, so its cost grows with the audience while
    # Telegram's budget does not: holding the interval per person fixed means
    # the total rate rises until it runs into the ceiling. This holds the
    # total instead and lets the interval stretch — a card that updates every
    # thirty seconds is honest, one stuck at a five-minute-old score is not.
    # 0 removes the adjustment.
    live_edit_budget: int = field(
        default_factory=lambda: _int("LIVE_EDIT_BUDGET", 10))

    # how long before we report that the service has gone blind. In urgent
    # situations (under a minute to the start, three rounds left on a map,
    # overtime) the threshold is a minute regardless — see hltv_notify.watchdog.
    degraded_alert_seconds: int = field(
        default_factory=lambda: _int("DEGRADED_ALERT_SECONDS", 300))

    # pause after a 403 on the live feed: the source is asking us to back off,
    # and seconds will not help. The service lives on match-page polling then.
    live_feed_cooldown: int = field(
        default_factory=lambda: _int("LIVE_FEED_COOLDOWN_SECONDS", 600))

    # alert on a multikill by a player of OUR team, so a highlight can be clipped
    multikill_alerts: bool = field(default_factory=lambda: _bool("MULTIKILL_ALERTS", True))
    multikill_threshold: int = field(default_factory=lambda: _int("MULTIKILL_THRESHOLD", 4))

    # Alert on the half, and on every new overtime. Two separate switches:
    # a half is routine and happens on every map, an overtime does not happen
    # at all most of the time and is the more interesting of the two, so
    # wanting one without the other is the normal case rather than a corner.
    #
    # PHASE_ALERTS is what used to cover both, and is still read as the
    # fallback for each, so an existing .env keeps behaving exactly as it did.
    # It is deliberately NOT a field of its own: it is resolved here, from the
    # environment, and a field nothing reads is a trap — `Config(phase_alerts=
    # True)` would look like it turned both on and would do nothing at all.
    # Off by default either way: it is one more message per map for something
    # the live card already shows.
    half_alerts: bool = field(
        default_factory=lambda: _bool("HALF_ALERTS", _bool("PHASE_ALERTS", False)))
    overtime_alerts: bool = field(
        default_factory=lambda: _bool("OVERTIME_ALERTS", _bool("PHASE_ALERTS", False)))

    # How big a swing in the score difference counts as a comeback. Not a
    # streak: 3:11 -> 13:11 and 1:7 -> 13:9 are both swings of ten, and only
    # the first is a streak. 0 switches the line off entirely.
    comeback_rounds: int = field(default_factory=lambda: _int("COMEBACK_ROUNDS", 9))

    # Broadcast links under a multikill, so the moment can be clipped by hand
    # before it scrolls off the stream. All three are DEFAULTS: each is a knob
    # in /settings, and a row exists there only once somebody changes it.
    stream_links: bool = field(default_factory=lambda: _bool("STREAM_LINKS", True))
    # 0 means every stream, not "off" — off is STREAM_LINKS. See
    # settings.Setting.zero_word.
    stream_links_max: int = field(default_factory=lambda: _int("STREAM_LINKS_MAX", 3))
    # Languages worth showing. Anything else appears ONLY when the match has no
    # broadcast in any of them at all.
    stream_languages: str = field(
        default_factory=lambda: _str("STREAM_LANGUAGES", "en,ru"))
    # Which flags mean which language. HLTV marks a broadcast with a COUNTRY,
    # so English arrives under half a dozen of them; without the aliases an
    # Australian cast on 155 viewers loses to a Russian one on 8. In the
    # environment rather than in code so a missing flag needs no release.
    stream_language_aliases: str = field(
        default_factory=lambda: _str("STREAM_LANGUAGE_ALIASES",
                                     "en:GB,US,WORLD,AU,CA,NZ,IE"))

    # Pre-match reminders: the defaults handed to a new subscriber, who then
    # edits them via /remind.
    default_reminders: str = field(default_factory=lambda: _str("REMINDERS", "15"))

    # How long history is kept. The event journal is the protection against
    # re-sending, so it is pruned far more cautiously than the queue.
    outbox_keep_days: int = field(default_factory=lambda: _int("OUTBOX_KEEP_DAYS", 90))
    events_keep_days: int = field(default_factory=lambda: _int("EVENTS_KEEP_DAYS", 365))

    # event thresholds
    e2_min_shift_minutes: int = field(default_factory=lambda: _int("E2_MIN_SHIFT_MINUTES", 5))
    e2_debounce_minutes: int = field(default_factory=lambda: _int("E2_DEBOUNCE_MINUTES", 10))
    stale_minutes: int = field(default_factory=lambda: _int("STALE_MINUTES", 15))

    # HTTP
    # The proxy comes from the standard HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/
    # NO_PROXY — we deliberately do not invent our own variables, see
    # hltv_notify.proxy.
    proxy: ProxySettings = field(default_factory=ProxySettings.from_env)
    impersonate: str = field(default_factory=lambda: _str("HTTP_IMPERSONATE", "chrome"))
    http_retries: int = field(default_factory=lambda: _int("HTTP_RETRIES", 3))
    failures_before_alert: int = field(
        default_factory=lambda: _int("FAILURES_BEFORE_ALERT", 3))
    raw_log_days: int = field(default_factory=lambda: _int("RAW_LOG_DAYS", 7))

    @property
    def team_url(self) -> str:
        return f"{HLTV_BASE}/team/{self.team_id}/{self.team_slug}"

    def proxies_for(self, url: str) -> Dict[str, str]:
        """The proxy for one address, in the shape curl_cffi expects."""
        return self.proxy.for_url(url)

    def interval_for(self, mode: str) -> int:
        """Polling interval, ceiling included: config cannot break through it."""
        base = {
            "idle": self.poll_idle,
            "prematch": self.poll_prematch,
            "live": self.poll_live,
            "live_with_feed": self.poll_live_with_feed,
        }[mode]
        return max(base, int(HARD_MIN_REQUEST_INTERVAL_SECONDS))

    def telegram_enabled(self) -> bool:
        return bool(self.bot_token and self.allowed_chat_ids())

    def reminder_minutes(self) -> List[int]:
        values = []
        for part in self.default_reminders.replace(";", ",").split(","):
            part = part.strip()
            if part.isdigit():
                values.append(int(part))
        return sorted(set(values), reverse=True)

    def stream_language_list(self) -> List[str]:
        """The default primary languages, in the order they were written."""
        return parse_languages(self.stream_languages)

    def flag_languages(self) -> Dict[str, str]:
        """`{"GB": "en", "US": "en", ...}` — flags that stand for a language.

        A flag missing from here is its own language, lowercased: `RU` -> `ru`,
        `BR` -> `br`. So the table only has to carry the exceptions.

        Written `en:GB,US,WORLD ru:BY`. Two rules, and they are deliberately
        the whole of it:

        * a COLON opens a language, and the flags after it — separated by
          commas — belong to it. The next colon opens the next language,
          wherever it stands: after a space, a semicolon or a comma;
        * anything following a SPACE without a colon of its own is not part of
          the format. It is dropped and said so, never guessed at — and the
          drop ENDS the current language, so the flags trailing a group we
          could not read do not attach to the one before it.

        The second rule is the one that had to be learned. Without it a group
        that lost its colon — `en:GB,US ru BY` — does not go missing, it maps
        `RU` onto English, and every Russian broadcast is labelled English
        while the table looks perfectly healthy. Dropping is the only safe
        direction: a flag left out is still its own language, which is right,
        whereas a flag pointed at the wrong one is silently wrong everywhere it
        is read.

        This is the third version. The first split groups on whitespace and
        lost `en: GB, US` entirely; the second squeezed the whitespace out
        around every separator and then read the comma in `en:GB,US, ru:BY` as
        belonging inside English. `tests/test_streams.py` holds every shape at
        once now, working and broken, so a fourth cannot trade one for another.

        An unreadable value is not a visible failure: the block still appears,
        it just stops counting `AU` and `US` as English and drops the most
        watched cast. Hence the warnings below.
        """
        table: Dict[str, str] = {}
        dropped: List[str] = []
        language = ""
        # Whitespace around the COLON goes first, so `en : GB` still opens a
        # language. Safe there and only there: a colon always binds a language
        # to what follows it, whereas doing the same around the comma is what
        # merged two groups into one.
        text = re.sub(r"\s*:\s*", ":", self.stream_language_aliases)
        # The separators are KEPT. Which one preceded a token is exactly what
        # tells a further flag of the language just opened (a comma) from a
        # group that lost its colon (a space).
        pieces = re.split(r"([\s,;]+)", text)
        separator = ""
        for index, piece in enumerate(pieces):
            if index % 2:
                separator = piece
                continue
            token = piece.strip()
            if not token:
                continue
            if ":" in token:
                name, *flags = token.split(":")
                language = name.strip().lower()
                if not language:
                    # And it stays empty on purpose: nothing following a group
                    # we could not read may attach to whatever came before it.
                    dropped.append(token)
                    continue
            elif language and "," in separator:
                flags = [token]
            else:
                # Same clearing as above, and for the same reason: the flags
                # trailing a group we could not read — `ru BY,KZ` — must not
                # attach to the language before it. Only KZ is separated by a
                # comma there, so without this it alone would come out English
                # while its own group was dropped.
                dropped.append(token)
                language = ""
                continue
            for flag in flags:
                flag = flag.strip().upper()
                if flag:
                    table[flag] = language
        if dropped:
            log.warning(
                "STREAM_LANGUAGE_ALIASES: ignored %s — a group is a language, a "
                "colon and its flags, like en:GB,US. A flag nobody claims stays "
                "its own language, which is safer than guessing at one.",
                ", ".join(repr(one) for one in dropped))
        if not table and self.stream_language_aliases.strip():
            log.warning(
                "STREAM_LANGUAGE_ALIASES=%r left an empty table. Every flag now "
                "stands for its own language, so an English cast under AU or US "
                "no longer counts as English.",
                self.stream_language_aliases)
        return table

    def allowed_chat_ids(self) -> List[str]:
        """Allowed chats in declaration order, without duplicates.

        There is a single source, `TELEGRAM_CHAT_ID`, where ids are listed
        separated by commas (semicolons and surrounding spaces are accepted
        too).
        """
        ids: List[str] = []
        for part in self.chat_id.replace(";", ",").split(","):
            part = part.strip()
            if not part or part in ids:
                continue
            if not _CHAT_ID_RE.match(part):
                # Do not fail the startup: the other ids work, and this one
                # would receive nothing anyway — Telegram is addressed by number.
                log.warning("skipping %r in the chat list: a numeric id is "
                            "required, /whoami will tell you yours", part)
                continue
            ids.append(part)
        return ids

    @property
    def main_chat_id(self) -> str:
        """The main chat: the first one in the list.

        It is also where the first team from TEAM_ID is seeded and the fallback
        recipient while there are no subscribers in the database.
        """
        ids = self.allowed_chat_ids()
        return ids[0] if ids else ""

    def chat_allowed(self, chat_id: str) -> bool:
        if not self.whitelist_only:
            return True
        return str(chat_id) in self.allowed_chat_ids()


def load() -> Config:
    return Config()
