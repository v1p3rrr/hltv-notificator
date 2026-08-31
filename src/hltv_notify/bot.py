"""Bot commands — the service's interface.

You should be able to work out why a notification did not arrive without
opening an SSH session. Command replies are sent directly, bypassing the
outbox: these are not notifications, so they need neither deduplication nor
re-delivery after a restart.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Optional

from zoneinfo import ZoneInfo

from .config import HLTV_BASE, Config
from .notify import format as fmt
from .sources import team_page
from .notify.telegram import Telegram, TelegramError
from . import menu
from . import settings as prefs
from .models import MatchState
from .scheduler import LAST_ERROR_KEY, LAST_POLL_KEY, SchedulePoller
from .state.db import Storage, parse_iso, utcnow
from .watchdog import Watchdog

log = logging.getLogger(__name__)

TEAM_URL_RE = re.compile(r"/team/(\d+)(?:/([^/?#\s]+))?")


# How often one refused chat may write to the log. See _log_refusal.
REFUSAL_LOG_INTERVAL = 600.0

# How many chats the command rate limiter remembers at once.
MAX_RATE_TRACKED_CHATS = 512

DURATION_RE = re.compile(r"^(\d+)\s*([mh]?)", re.IGNORECASE)


def _parse_minutes(argument: str):
    """"15", "15m", "90", "2h" -> minutes. None means we could not parse it."""
    found = DURATION_RE.match((argument or "").strip())
    if not found:
        return None
    value = int(found.group(1))
    if found.group(2).lower() == "h":
        value *= 60
    return value if 1 <= value <= 24 * 60 else None


def _human(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h" if rest == 0 else f"{hours} h {rest} min"


def _quoted(text: str) -> str:
    """A word a person typed, quoted and escaped for Telegram's HTML."""
    return '"' + fmt.escape(text) + '"'


def _parse_team_ref(argument: str):
    """id and slug out of a team link, or out of a bare id."""
    argument = (argument or "").strip()
    found = TEAM_URL_RE.search(argument)
    if found:
        return int(found.group(1)), found.group(2)
    if argument.isdigit():
        return int(argument), None
    return None, None

# One list, shared by the text /mute and by the buttons. There must not be two
# copies — they would drift apart, and a button would start muting something
# the command cannot.
MUTABLE_EVENTS = tuple(code for code, _ in menu.MUTABLE)

# Commands that STORE something for the person sending them. Only these
# create a subscriber: looking at /status or /next leaves no trace.
WRITING_COMMANDS = frozenset({"/track", "/untrack", "/mute", "/unmute",
                             "/remind", "/tz", "/pause", "/resume", "/settings"})

# One list of commands, in the order a person meets them. The /help text and
# the hint list Telegram shows when you type "/" are both generated from it —
# two copies would drift, and the drift is invisible from inside: /live had
# existed for a long time and /help had never mentioned it.
#
# Telegram's limits on setMyCommands: the name is 1-32 characters of
# [a-z0-9_], the description 1-256 characters and plain text. The description
# below is written to fit that hint list, where there is room for one line.
COMMANDS = (
    ("menu", "", "Buttons for most of the below"),
    ("status", "", "Service and source health"),
    ("live", "", "The running match, and where the data comes from"),
    ("next", "", "Upcoming matches as the service sees them"),
    ("teams", "", "Which teams you follow"),
    ("track", "<team link>", "Start following a team"),
    ("untrack", "<id>", "Stop following a team"),
    ("mute", "<id> <E5,E9>", "Mute event types for a team"),
    ("unmute", "<id>", "Clear all mutes for a team"),
    ("remind", "[15m|1h]", "Pre-match reminders; /remind rm 15m removes one"),
    ("tz", "<Europe/Berlin>", "Your timezone"),
    ("settings", "[name] [value]", "Your thresholds: multikill, comeback, phase, card"),
    ("pause", "", "Go quiet"),
    ("resume", "", "Start sending again"),
    ("check", "", "Read the schedule now, without waiting for the next cycle"),
    ("whoami", "", "Your chat_id"),
    ("verbose", "on|off", "Debug logging in the service log (main chat only)"),
    ("help", "", "This list of commands"),
)


def _help_text() -> str:
    """The /help message. Sent with parse_mode=HTML, so the arguments — which
    are written in angle brackets — have to be escaped."""
    lines = ["Commands:"]
    for name, args, description in COMMANDS:
        head = f"/{name} {fmt.escape(args)}" if args else f"/{name}"
        lines.append(f"{head} — {description}")
    return "\n".join(lines)


# Commands that act on the whole service rather than on the caller's own
# subscription. Answered for the main chat alone, and kept out of everybody
# else's hint list — offering a command that will refuse you is worse than not
# offering it. The same set gates the answer and the advertising, so the two
# cannot disagree.
OWNER_ONLY = frozenset({"verbose"})


def command_menu(*, owner: bool = False):
    """The payload for setMyCommands: the public list, or the owner's."""
    return [{"command": name, "description": description}
            for name, _, description in COMMANDS
            if owner or name not in OWNER_ONLY]


HELP = _help_text()


class CommandBot:
    def __init__(self, storage: Storage, config: Config, telegram: Telegram,
                 poller: SchedulePoller, matches=None, http=None):
        self.storage = storage
        self.config = config
        self.telegram = telegram
        self.poller = poller
        self.matches = matches
        self.http = http
        self._offset: Optional[int] = None
        # chat_id -> when it was last written about as refused
        self._refused = {}
        # chat_id -> the times of its recent commands, and whether it has
        # already been told it is going too fast in this window.
        self._commands = {}
        self._warned_rate = {}

    async def run(self, stop: asyncio.Event) -> None:
        # Hand Telegram the command list so it can offer it on "/" and behind
        # the Menu button. Without this the hint list stays empty and the only
        # way to find a command is to already know that /help exists. It is
        # sent on every start rather than once: the list changes with the code
        # and Telegram keeps whatever it was told last.
        try:
            await self.telegram.set_my_commands(command_menu())
            # The owner sees one more: the commands that act on the service as
            # a whole. A chat-scoped list wins over the default one, so this
            # adds them for the main chat without showing them to anyone else.
            if self.config.main_chat_id:
                await self.telegram.set_my_commands(
                    command_menu(owner=True),
                    scope={"type": "chat", "chat_id": self.config.main_chat_id})
        except TelegramError as exc:
            # Not fatal — the bot answers commands either way.
            log.warning("could not register the command list: %s", exc)

        # Skip whatever piled up while we were down: answering week-old
        # commands makes no sense.
        try:
            backlog = await self.telegram.get_updates(None, timeout=0)
            if backlog:
                self._offset = backlog[-1]["update_id"] + 1
        except TelegramError as exc:
            log.warning("could not read the command backlog: %s", exc)

        while not stop.is_set():
            try:
                updates = await self.telegram.get_updates(self._offset, timeout=25)
            except TelegramError as exc:
                log.warning("getUpdates failed: %s", exc)
                await asyncio.sleep(5)
                continue
            except Exception:  # noqa: BLE001 - the bot is not allowed to die
                log.exception("command polling failed")
                await asyncio.sleep(5)
                continue

            for update in updates:
                self._offset = update["update_id"] + 1
                if "callback_query" in update:
                    await self._handle_callback(update["callback_query"])
                else:
                    await self._handle(update)

    async def _handle(self, update: dict) -> None:
        message = update.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return
        command, _, argument = text.partition(" ")
        command = command.split("@")[0].lower()
        argument = argument.strip()

        if not self.config.chat_allowed(chat_id):
            # Silently, and with NO exceptions: any command that answers a
            # stranger confirms the bot exists to whoever probes it, and hands
            # them something to spam. /whoami used to be that exception, so
            # that a newcomer could learn their own id — but @userinfobot
            # tells them the same number without touching this bot, and the id
            # of whoever knocked goes to the log below anyway, which is where
            # the owner takes it from to widen the whitelist.
            self._log_refusal(chat_id, command)
            return

        limited = self._rate_limited(chat_id)
        if limited == "first":
            await self._reply(chat_id, "Too many commands at once — try again "
                                       "in a minute.")
            return
        if limited:
            return

        # A subscriber row is created by a command that STORES something, not
        # by one that shows something. Reading /status or /next leaves no
        # trace; /track, a reminder, a timezone or the quiet switch are a
        # person setting themselves up, and that is what a subscriber is.
        # Creating a row for anybody who says anything is a write an outsider
        # can make the service do, and with the whitelist off it is unbounded.
        if command in WRITING_COMMANDS and self.storage.get_subscriber(chat_id) is None:
            self.storage.add_subscriber(chat_id)
            log.info("new subscriber: %s", chat_id)

        handlers = {
            "/teams": lambda: self._teams(chat_id),
            "/status": lambda: self._status(chat_id),
            "/live": lambda: self._live(chat_id),
            "/next": lambda: self._next(chat_id),
            "/check": self._check,
            "/start": lambda: HELP,
            "/help": lambda: HELP,
            "/menu": lambda: self._menu_text(chat_id),
            "/whoami": lambda: f"Your chat_id: <code>{fmt.escape(chat_id)}</code>",
        }
        try:
            if command == "/verbose":
                reply = self._verbose(chat_id, argument.lower())
            elif command == "/track":
                reply = await self._track(chat_id, argument)
            elif command == "/untrack":
                reply = self._untrack(chat_id, argument)
            elif command == "/mute":
                reply = self._mute(chat_id, argument)
            elif command == "/unmute":
                reply = self._unmute(chat_id, argument)
            elif command == "/remind":
                reply = self._remind(chat_id, argument)
            elif command == "/tz":
                reply = self._timezone(chat_id, argument)
            elif command == "/settings":
                reply = self._settings(chat_id, argument)
            elif command == "/pause":
                reply = self._pause(chat_id, True)
            elif command == "/resume":
                reply = self._pause(chat_id, False)
            elif command in handlers:
                result = handlers[command]()
                reply = await result if asyncio.iscoroutine(result) else result
            else:
                reply = f"I do not know that command.\n\n{HELP}"
        except Exception:  # noqa: BLE001 - an answer has to go out regardless
            log.exception("failed to handle command %s", command)
            reply = "The command crashed, details are in the logs."

        await self._reply(chat_id, reply, self._markup_for(chat_id, command))

    def _rate_limited(self, chat_id: str) -> Optional[str]:
        """Whether this chat has run over its command allowance.

        A sliding minute per chat. The refusal is stated ONCE per window and
        then the chat is simply ignored: answering every message over the
        limit would mean the bot amplifying a flood with its own replies.

        Off unless `COMMAND_RATE_LIMIT` is set. With the whitelist on the
        people who can reach the bot are people the owner chose; this is for
        the open mode, and for the client that gets stuck in a loop.
        """
        limit = self.config.command_rate_limit
        if limit <= 0:
            return None
        now = time.monotonic()
        window = [when for when in self._commands.get(chat_id, ())
                  if now - when < 60.0]
        if len(self._commands) > MAX_RATE_TRACKED_CHATS:
            self._prune_rate_state(now)
        if len(window) >= limit:
            self._commands[chat_id] = window
            if self._warned_rate.get(chat_id):
                return "again"
            self._warned_rate[chat_id] = True
            log.warning("chat %s is over its command limit (%d/min)", chat_id, limit)
            return "first"
        window.append(now)
        self._commands[chat_id] = window
        self._warned_rate.pop(chat_id, None)
        return None

    def _prune_rate_state(self, now: float) -> None:
        """Keep the rate-limiting maps bounded.

        Dropping only what has aged out is not enough: a burst from a
        thousand different ids inside one minute leaves a thousand fresh
        entries, and that is exactly the shape an outsider would produce. So
        after the stale ones go, the map is cut back to the most recently
        seen chats.

        Evicting somebody hands them a fresh allowance, which is a real if
        small cost — and the alternative is unbounded memory driven from
        outside. With the whitelist on, which is the default, nothing reaches
        this code that the owner did not let in.
        """
        fresh = {chat: times for chat, times in self._commands.items()
                 if times and now - times[-1] < 60.0}
        if len(fresh) > MAX_RATE_TRACKED_CHATS:
            keep = sorted(fresh, key=lambda chat: fresh[chat][-1],
                          reverse=True)[:MAX_RATE_TRACKED_CHATS]
            fresh = {chat: fresh[chat] for chat in keep}
        self._commands = fresh
        # The "already told off" map follows the same fate: an entry is only
        # cleared on the path where a chat comes back under its limit, so
        # somebody who floods once and never returns would otherwise sit here
        # for the life of the process.
        self._warned_rate = {chat: True for chat in self._warned_rate
                             if chat in fresh}

    def _log_refusal(self, chat_id: str, command: str) -> None:
        """A refused chat is logged once, then not again for a while.

        The line matters — it is where the owner reads the id of a chat that
        is not on the list yet, a group's especially, since nothing else
        reports that number. But one line per refused message is also the only
        thing an outsider can make this bot do: flood it and the log fills
        with their noise, and under the container's rotation (10 MB × 3) that
        pushes out the history you would actually want to read.

        The interval is per chat, so a second person knocking is still seen
        immediately.

        "Never heard from" is `None`, not zero. `time.monotonic()` counts from
        the machine's boot, so on a freshly started host it is a small number:
        with 0.0 standing in for "last logged", the very first refusal from
        every chat looked recent and was swallowed for the first ten minutes
        of uptime — which is exactly the window in which somebody adds the bot
        to a group and goes looking for its id in the log. It passed on a
        developer's machine, whose uptime is days, and failed on a CI runner
        booted a minute earlier.
        """
        now = time.monotonic()
        last = self._refused.get(chat_id)
        if last is not None and now - last < REFUSAL_LOG_INTERVAL:
            return
        # Whoever has gone quiet is forgotten, so a flood from many different
        # ids cannot grow this map without bound.
        if len(self._refused) > 512:
            self._refused = {chat: when for chat, when in self._refused.items()
                             if now - when < REFUSAL_LOG_INTERVAL}
        self._refused[chat_id] = now
        log.warning("command %s from chat %s refused: not on the whitelist",
                    command, chat_id)

    def _markup_for(self, chat_id: str, command: str):
        """Buttons on the replies where they help: menu, team list, intervals."""
        if command in ("/start", "/help", "/menu"):
            return menu.main(self.storage.subscriber_paused(chat_id))
        if command == "/teams":
            return menu.teams(self.storage.teams(chat_id, enabled_only=False))
        if command == "/remind":
            return menu.reminders(self.storage.reminders(chat_id))
        if command == "/settings":
            return menu.settings_screen(self._setting_values(chat_id))
        return None

    async def _reply(self, chat_id: str, text: str, markup=None) -> None:
        try:
            await self.telegram.send_message(chat_id, text, reply_markup=markup)
        except TelegramError as exc:
            log.error("could not reply to chat %s: %s", chat_id, exc)

    # ------------------------------------------------------------------

    async def _handle_callback(self, query: dict) -> None:
        """A button press.

        We ALWAYS answer, even when there is nothing to do: otherwise Telegram
        keeps spinning the button's indicator until it times out and the person
        concludes the bot has hung.
        """
        callback_id = query.get("id", "")
        message = query.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        message_id = message.get("message_id")

        if not self.config.chat_allowed(chat_id):
            # Through the same throttle as commands, and for the same reason.
            # This path is reachable by an outsider: someone taken off the
            # whitelist still has the bot's old messages, inline keyboards and
            # all, and Telegram keeps delivering their presses.
            self._log_refusal(chat_id, "button")
            await self._answer(callback_id, "No access")
            return
        if self._rate_limited(chat_id):
            # A press must ALWAYS be answered, or Telegram spins the button's
            # indicator until it times out. So the refusal is a toast rather
            # than silence — and no work is done behind it.
            await self._answer(callback_id, "Too many requests, wait a moment")
            return

        data = query.get("data", "")
        # Same rule as for commands: the sections (m:...) only show things,
        # everything else stores something.
        if not data.startswith("m:") and self.storage.get_subscriber(chat_id) is None:
            self.storage.add_subscriber(chat_id)

        try:
            text, markup, toast = self._dispatch_callback(chat_id, data)
        except Exception:  # noqa: BLE001 - a button must not hang the bot
            log.exception("failed to handle button %s", query.get("data"))
            text, markup, toast = "The button crashed, details are in the logs.", None, ""

        await self._answer(callback_id, toast)
        if text is None:
            return
        try:
            await self.telegram.edit_message_text(chat_id, message_id, text,
                                                  reply_markup=markup)
        except TelegramError as exc:
            # For instance the text did not change — then we simply do nothing.
            log.debug("message %s was not redrawn: %s", message_id, exc)

    async def _answer(self, callback_id: str, toast: str = "") -> None:
        try:
            await self.telegram.answer_callback_query(callback_id, toast)
        except TelegramError as exc:
            log.debug("could not acknowledge the button press: %s", exc)

    def _dispatch_callback(self, chat_id: str, data: str):
        """Button action -> (text, keyboard, toast)."""
        parsed = menu.parse(data)
        if parsed is None:
            return None, None, ""
        kind, args = parsed

        if kind == "m":
            section = args[0] if args else "main"
            if section == "status":
                return self._status(chat_id), menu.keyboard([menu.back()]), ""
            if section == "live":
                return self._live(chat_id), menu.keyboard([menu.back()]), ""
            if section == "next":
                return self._next(chat_id), menu.keyboard([menu.back()]), ""
            if section == "teams":
                return self._teams(chat_id), menu.teams(
                    self.storage.teams(chat_id, enabled_only=False)), ""
            if section == "rem":
                return (self._remind_list(chat_id),
                        menu.reminders(self.storage.reminders(chat_id)), "")
            if section == "set":
                return self._settings_screen(chat_id, "")
            return self._menu_text(chat_id), menu.main(
                self.storage.subscriber_paused(chat_id)), ""

        if kind == "p":
            paused = args and args[0] == "on"
            self.storage.set_subscriber_paused(chat_id, bool(paused))
            return (self._menu_text(chat_id), menu.main(bool(paused)),
                    "Going quiet" if paused else "Back on")

        if kind == "r" and args:
            minutes = int(args[0])
            if self.storage.remove_reminder(chat_id, minutes):
                toast = "Removed"
            else:
                self.storage.add_reminder(chat_id, minutes)
                toast = "Added"
            return (self._remind_list(chat_id),
                    menu.reminders(self.storage.reminders(chat_id)), toast)

        if kind == "s" and args:
            return self._settings_callback(chat_id, args)

        if kind == "t" and args:
            return self._team_callback(chat_id, args)

        return None, None, ""

    def _team_callback(self, chat_id: str, args):
        team_id = int(args[0])
        row = self.storage.get_team(chat_id, team_id)
        if row is None:
            return ("You do not have such a team.",
                    menu.teams(self.storage.teams(chat_id, enabled_only=False)), "")

        action = args[1] if len(args) > 1 else ""
        toast = ""
        if action == "rm":
            self.storage.set_team_enabled(chat_id, team_id, False)
            toast = "No longer following"
        elif action == "on":
            self.storage.set_team_enabled(chat_id, team_id, True)
            toast = "Following again"
        elif action == "x" and len(args) > 2:
            code = args[2]
            muted = self.storage.team_mutes(chat_id, team_id)
            if code in muted:
                muted.remove(code)
                toast = f"{code} will arrive again"
            else:
                muted.append(code)
                toast = f"{code} muted"
            self.storage.set_team_mutes(chat_id, team_id, muted)

        row = self.storage.get_team(chat_id, team_id)
        return (self._team_text(row),
                menu.team(team_id, row["name"], self.storage.team_mutes(chat_id, team_id),
                          bool(row["enabled"])),
                toast)

    def _team_text(self, row) -> str:
        muted = [code for code in (row["muted_events"] or "").split(",") if code]
        lines = [f"<b>{fmt.escape(row['name'])}</b>  ·  id {row['team_id']}"]
        if not row["enabled"]:
            lines.append("Tracking is off.")
        lines.append("Muted: " + (", ".join(muted) if muted else "nothing"))
        lines.append("")
        lines.append("🔔 — arrives, 🔕 — muted. Tap to toggle.")
        return "\n".join(lines)

    def _menu_text(self, chat_id: str) -> str:
        teams = self.storage.teams(chat_id)
        paused = self.storage.subscriber_paused(chat_id)
        lines = ["<b>Menu</b>",
                 f"Teams you follow: {len(teams)}"]
        if paused:
            lines.append("⚠️ Notifications are currently off.")
        lines.append("")
        lines.append("All of this is available as commands — /help.")
        return "\n".join(lines)

    # ------------------------------------------------------------------

    def _status(self, chat_id: str) -> str:
        tz = self._tz(chat_id)
        last_poll = self.storage.get_meta(LAST_POLL_KEY)
        last_error = self.storage.get_meta(LAST_ERROR_KEY)
        matches = len(self.storage.all_matches())
        upcoming = len(self.storage.upcoming_matches())

        lines = [
            "<b>Service status</b>",
            f"Subscribers: {len(self.storage.subscribers())}, "
            f"teams watched: {len(self.storage.tracked_teams())}",
            f"Schedule polling mode: {self.poller.mode}",
            f"Match polling mode: {self.matches.mode if self.matches else '—'}",
            f"Active matches: {len(self.matches.active()) if self.matches else 0}",
            f"Sending: {'OFF (DRY_RUN)' if self.config.dry_run else 'on'}",
            f"Last poll: {fmt.human_time(last_poll, tz) if last_poll else 'none yet'}",
            f"Consecutive failures: {self.poller.http.consecutive_failures}",
            f"Matches in the database: {matches}, upcoming: {upcoming}",
            f"Queued for sending: {self.storage.pending_count()}",
            f"Events sent in total: {self.storage.sent_event_count()}",
        ]
        lines.append(self._feed_line())
        degraded = Watchdog(self.storage, self.config).degraded_subsystems()
        if degraded:
            lines.append("⚠️ Not working: " + ", ".join(degraded))
        if last_error:
            lines.append(f"Last error: <i>{fmt.escape(last_error)}</i>")
        return "\n".join(lines)

    def _teams(self, chat_id: str) -> str:
        rows = self.storage.teams(chat_id, enabled_only=False)
        if not rows:
            return "You are not following any team. Add one with /track &lt;link&gt;"
        lines = ["<b>Your teams</b>"]
        for row in rows:
            marks = []
            if not row["enabled"]:
                marks.append("off")
            if row["muted_events"]:
                marks.append("muted: " + row["muted_events"].replace(",", ", "))
            tail = ("  (" + "; ".join(marks) + ")") if marks else ""
            lines.append(f"{fmt.escape(row['name'])} — id {row['team_id']}{tail}")
        return "\n".join(lines)

    async def _track(self, chat_id: str, argument: str) -> str:
        """Add a team. Accepts a link to the team page or its id.

        The link is preferable: both the id and the slug come out of it. Teams
        have namesakes and ex- prefixes, so searching by name is impossible —
        only by id.
        """
        team_id, slug = _parse_team_ref(argument)
        if team_id is None:
            return ("I did not understand that. Send a link like\n"
                    "https://www.hltv.org/team/12857/forze-reload")
        # Checked before the page is fetched: a refusal must not spend a
        # request out of the very budget this limit exists to protect.
        full = self._team_limit_reached(chat_id, team_id)
        if full is not None:
            return full
        if self.http is None:
            return "Adding is unavailable: the service is running without an HTTP layer."

        url = f"{HLTV_BASE}/team/{team_id}/{slug or '-'}"
        try:
            html = await self.http.get_text(url)
        except Exception as exc:  # noqa: BLE001 - show the reason to the user
            return f"The team page did not open: {type(exc).__name__}: {exc}"

        name = team_page.parse_team_name(html)
        if not name:
            return f"No team name found on {url} — check the link."

        added = self.storage.add_team(chat_id, team_id, slug or str(team_id), name)
        self.poller.request_poll()
        if added:
            return (f"Now following <b>{fmt.escape(name)}</b> (id {team_id}).\n"
                    "The current schedule was recorded silently — notifications "
                    "start from the next change.")
        return (f"<b>{fmt.escape(name)}</b> (id {team_id}) was already followed, "
                "turned it back on.")

    def _team_limit_reached(self, chat_id: str, team_id: int) -> Optional[str]:
        """The refusal text when this chat is already at its limit, else None.

        The limit is not about tidiness. A schedule sweep costs one request per
        DISTINCT team and the ceiling is one request every 30 seconds, so ten
        teams already mean a five-minute sweep against the three minutes
        pre-match mode wants. Somebody adding teams one by one has no way of
        seeing that they are the reason notifications started arriving late.

        Re-enabling a team that is already on the list does not count as a new
        one; a team currently switched off does, because it goes back to being
        polled.
        """
        limit = self.config.max_teams_per_subscriber
        if limit <= 0:
            return None
        row = self.storage.get_team(chat_id, team_id)
        if row is not None and row["enabled"]:
            return None
        current = len(self.storage.teams(chat_id))
        if current < limit:
            return None
        return (f"You already follow {current} teams, which is the limit "
                f"({limit}). Every team is a separate page to read, and the "
                "service is deliberately slow with the source — more teams "
                "means every one of them is looked at less often.\n\n"
                "Drop one with /untrack &lt;id&gt; (or the button in /teams) "
                "and add this one again.")

    def _untrack(self, chat_id: str, argument: str) -> str:
        team_id, _ = _parse_team_ref(argument)
        if team_id is None:
            return "Usage: /untrack &lt;team id&gt;"
        row = self.storage.get_team(chat_id, team_id)
        if row is None:
            return f"You were not following team {team_id} anyway."
        self.storage.set_team_enabled(chat_id, team_id, False)
        # Match history is not deleted: if the team comes back, the journal of
        # what was already sent stops everything being sent out again.
        return (f"No longer following <b>{fmt.escape(row['name'])}</b> (id {team_id}). "
                "History is kept.")

    def _mute(self, chat_id: str, argument: str) -> str:
        """Mute event types for one team.

        If two tracked teams play each other, the event still arrives when AT
        LEAST ONE of your teams in that match wants it: otherwise one team
        would silently mute notifications about the other.
        """
        parts = argument.split()
        team_id, _ = _parse_team_ref(parts[0]) if parts else (None, None)
        if team_id is None or len(parts) < 2:
            return ("Usage: /mute &lt;team id&gt; &lt;types, comma separated&gt;\n"
                    f"Types: {', '.join(MUTABLE_EVENTS)}")
        row = self.storage.get_team(chat_id, team_id)
        if row is None:
            return f"You are not following team {team_id}."

        requested = [part.strip().upper() for part in parts[1].replace(";", ",").split(",")]
        requested = [part for part in requested if part]
        unknown = [part for part in requested if part not in MUTABLE_EVENTS]
        if unknown:
            return (f"Unknown type(s): {', '.join(unknown)}.\n"
                    f"Available: {', '.join(MUTABLE_EVENTS)}")

        self.storage.set_team_mutes(chat_id, team_id, requested)
        return (f"Muted for <b>{fmt.escape(row['name'])}</b>: "
                f"{', '.join(sorted(set(requested)))}")

    def _unmute(self, chat_id: str, argument: str) -> str:
        team_id, _ = _parse_team_ref(argument)
        if team_id is None:
            return "Usage: /unmute &lt;team id&gt;"
        row = self.storage.get_team(chat_id, team_id)
        if row is None:
            return f"You are not following team {team_id}."
        self.storage.set_team_mutes(chat_id, team_id, [])
        return f"Mutes cleared for <b>{fmt.escape(row['name'])}</b>."

    def _remind(self, chat_id: str, argument: str) -> str:
        """The list of intervals at which to remind before a match."""
        parts = argument.split()
        if not parts:
            return self._remind_list(chat_id)

        removing = parts[0].lower() in ("rm", "del", "-", "remove", "delete")
        value = _parse_minutes(parts[1] if removing and len(parts) > 1 else parts[0])
        if value is None:
            return ("Usage: /remind 15m to add, /remind rm 15m to remove.\n"
                    "Minutes and hours are accepted: 15, 30m, 1h, 2h.")

        if removing:
            if not self.storage.remove_reminder(chat_id, value):
                return f"There was no reminder at {fmt.escape(_human(value))}."
            return f"Removed the reminder at {fmt.escape(_human(value))}.\n\n" + \
                   self._remind_list(chat_id)
        if not self.storage.add_reminder(chat_id, value):
            return f"A reminder at {fmt.escape(_human(value))} is already set."
        return f"Will remind you {fmt.escape(_human(value))} before.\n\n" + \
               self._remind_list(chat_id)

    def _remind_list(self, chat_id: str) -> str:
        values = self.storage.reminders(chat_id)
        if not values:
            return "No reminders. Add one with /remind 15m"
        listed = ", ".join(_human(value) for value in values)
        return f"<b>Reminding you before:</b> {fmt.escape(listed)}"

    def _timezone(self, chat_id: str, argument: str) -> str:
        """Everyone has their own: subscribers may live in different zones."""
        current = self.storage.subscriber_timezone(chat_id, self.config.timezone)
        if not argument:
            return (f"Your timezone: <b>{fmt.escape(current)}</b>\n"
                    "Change it: /tz Europe/Berlin")
        try:
            ZoneInfo(argument)
        except Exception:  # noqa: BLE001 - the zone name comes from a person
            return (f"I do not know the zone \"{fmt.escape(argument)}\".\n"
                    "It must be an IANA name, for example Europe/Moscow or Asia/Tbilisi.")
        self.storage.set_subscriber_timezone(chat_id, argument)
        return f"Times will be shown in <b>{fmt.escape(argument)}</b>."

    # ---------- per-person settings ----------

    def _setting_values(self, chat_id: str):
        """This person's knobs, with the environment filling in the gaps."""
        return self.storage.settings_for(chat_id, prefs.defaults(self.config))

    def _settings(self, chat_id: str, argument: str) -> str:
        """`/settings`, `/settings multikill 5`, `/settings comeback default`."""
        parts = (argument or "").split()
        if not parts:
            return self._settings_text(chat_id)

        name = parts[0].lower()
        item = prefs.get(name)
        if item is None:
            known = ", ".join(f"<code>{one.name}</code>" for one in prefs.SETTINGS)
            return (f"There is no setting called {_quoted(parts[0])}.\n"
                    f"There is: {known}")

        if len(parts) == 1:
            current = self._setting_values(chat_id)[name]
            return (f"<b>{fmt.escape(item.label)}</b>: {item.describe(current)}\n"
                    f"{fmt.escape(item.summary)}\n"
                    f"Change it: /settings {name} {item.minimum}-{item.maximum}, "
                    f"or /settings {name} default")

        raw = parts[1].lower()
        if raw in {"default", "reset"}:
            self.storage.clear_setting(chat_id, name)
            restored = prefs.default_for(self.config, name)
            return (f"<b>{fmt.escape(item.label)}</b> is back to the service "
                    f"default: {item.describe(restored)}")

        value = prefs.parse_value(item, raw)
        if value is None:
            return (f"I could not read {_quoted(parts[1])}.\n"
                    f"{fmt.escape(item.label)} takes {item.minimum}-{item.maximum}, "
                    "or off.")
        if value < 0:
            # "on" for a numeric setting means "back to the service default".
            self.storage.clear_setting(chat_id, name)
            value = prefs.default_for(self.config, name)
            if value <= 0:
                # The default is itself "off", so there is nothing to return
                # to. Saying so beats silently doing nothing.
                return (f"The service default for <b>{fmt.escape(item.label)}</b> "
                        f"is off. Give a number: /settings {name} "
                        f"{max(1, item.minimum)}")
        else:
            value = item.clamp(value)
            self.storage.set_setting(chat_id, name, value)
        return (f"<b>{fmt.escape(item.label)}</b>: {item.describe(value)}\n"
                f"{fmt.escape(item.summary)}")

    def _settings_text(self, chat_id: str) -> str:
        values = self._setting_values(chat_id)
        lines = ["<b>Your settings</b>", ""]
        lines += prefs.summary_lines(values)
        lines += ["", "Change one: /settings multikill 3",
                  "Back to the service default: /settings multikill default"]
        return "\n".join(lines)

    def _settings_screen(self, chat_id: str, toast: str):
        return (self._settings_text(chat_id),
                menu.settings_screen(self._setting_values(chat_id)), toast)

    def _settings_callback(self, chat_id: str, args):
        """`s:<name>` is the caption row — pressing it only redraws.

        It still has to come back with something: a button press that is not
        answered leaves Telegram spinning until it times out, and the person
        decides the bot has hung.
        """
        name = args[0]
        item = prefs.get(name)
        if item is None:
            return self._settings_screen(chat_id, "")
        if len(args) < 2:
            return self._settings_screen(chat_id, item.summary)
        try:
            value = item.clamp(int(args[1]))
        except ValueError:
            return self._settings_screen(chat_id, "")
        self.storage.set_setting(chat_id, name, value)
        return self._settings_screen(
            chat_id, f"{item.label}: {item.describe(value)}")

    def _pause(self, chat_id: str, paused: bool) -> str:
        self.storage.set_subscriber_paused(chat_id, paused)
        if paused:
            return ("Going quiet. Notifications will neither arrive nor pile up — "
                    "what you miss is not delivered later.\nTurn back on: /resume")
        return "Back on."

    def _feed_line(self) -> str:
        """Live feed health: E5, multikills and the speed of E6 depend on it."""
        supervisor = getattr(self.matches, "supervisor", None) if self.matches else None
        if supervisor is None:
            return "Live feed: not enabled"
        feeds = supervisor.connected_matches()
        if not feeds:
            return "Live feed: no matches"
        connected = [str(mid) for mid, ok in feeds.items() if ok]
        pending = [str(mid) for mid, ok in feeds.items() if not ok]
        parts = []
        if connected:
            parts.append("connected: " + ", ".join(connected))
        if pending:
            parts.append("connecting: " + ", ".join(pending))
        return "Live feed: " + "; ".join(parts)

    def _tz(self, chat_id: str) -> str:
        """The asker's timezone. Notifications use it too — they must not differ."""
        return self.storage.subscriber_timezone(chat_id, self.config.timezone)

    def _mine(self, chat_id: str, rows):
        """Filter out other people's teams: accounts keep their own lists."""
        visible = self.storage.visible_match_ids(chat_id)
        return [row for row in rows if row["match_id"] in visible]

    def _live(self, chat_id: str) -> str:
        """What is happening in a running match right now.

        Useful when a notification did not arrive: you can see whether the
        score reached the service at all and which source last updated it.
        """
        rows = [row for row in self._mine(
                    chat_id, self.matches.active() if self.matches else [])
                if row["state"] == MatchState.LIVE]
        if not rows:
            return "No matches right now."

        supervisor = getattr(self.matches, "supervisor", None)
        feeds = supervisor.connected_matches() if supervisor else {}
        blocks = []
        for row in rows:
            state = self.storage.get_state(row["match_id"])
            score = state["current_map_score"] if state else None
            series = state["series_score"] if state else None
            map_name = state["current_map_name"] if state else None
            source = state["last_source"] if state else "?"
            feed = "connected" if feeds.get(row["match_id"]) else "no"
            block = [
                # The team name comes from the match itself: there can be
                # several tracked teams, and the config value would only fit
                # the first one.
                f"<b>{fmt.escape(self._match_team_name(row['match_id']))} — "
                f"{fmt.escape(row['opponent_name'])}</b>",
                fmt.escape(row["event_name"]),
                f"Map: {fmt.escape(map_name) if map_name else '—'}"
                f"   score: {score or '—'}   series: {series or '—'}",
                f"Live feed: {feed} · last update from source \"{source}\"",
            ]
            for result in self.storage.map_results(row["match_id"]):
                block.append(f"   {fmt.escape(result['map_name'])} — "
                             f"{result['score_team']}:{result['score_opponent']}")
            block.append(fmt.escape(row["url"]))
            blocks.append("\n".join(block))
        return "\n\n".join(blocks)

    def _match_team_name(self, match_id: int) -> str:
        team_id = self.storage.canonical_team(match_id)
        return self.storage.team_name(team_id, self.config.team_name)

    def _next(self, chat_id: str) -> str:
        rows = self._mine(chat_id, self.storage.upcoming_matches())
        if not rows:
            return ("No upcoming matches. For this team that is normal — it can "
                    "go weeks without playing.")
        lines = ["<b>Upcoming matches</b>"]
        for row in rows[:10]:
            when = fmt.human_time(row["start_utc"], self._tz(chat_id))
            lines.append(
                f"{when} — {fmt.escape(row['opponent_name'])}\n"
                f"    {fmt.escape(row['event_name'])}\n"
                f"    {fmt.escape(row['url'])}")
        return "\n".join(lines)

    async def _check(self) -> str:
        self.poller.request_poll()
        return "Checking the schedule. If anything changed, a notification will follow."

    def _verbose(self, chat_id: str, argument: str) -> str:
        """The log level of the whole process, so it is the owner's alone.

        Every other command touches only the caller's own subscription; this
        one changes what the service writes to disk for everybody. With
        several subscribers — and more so with the whitelist off — it must not
        be a lever anybody can pull.
        """
        if chat_id != self.config.main_chat_id:
            return ("Only the main chat can change the log level "
                    "— it is a setting of the whole service.")
        if argument not in {"on", "off"}:
            return "Usage: /verbose on | /verbose off"
        level = logging.DEBUG if argument == "on" else getattr(
            logging, self.config.log_level.upper(), logging.INFO)
        logging.getLogger("hltv_notify").setLevel(level)
        return f"Verbose mode {'on' if argument == 'on' else 'off'}."
