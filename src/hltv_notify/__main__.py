"""Entry point: brings the tasks up and shuts them down cleanly.

One process, one user. The components do not call each other directly: polling
records observations, the state machine gives birth to events, the notifier
sends them. Deduplication lives in one place rather than being smeared across
the code.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import List, Optional

from . import config as config_module
from .bot import CommandBot
from .http import HltvHttp
from .live_worker import LiveSupervisor
from .match_poller import MatchPoller
from .notify.live_message import LiveMessenger
from .notify.outbox import Notifier
from .notify.telegram import API_BASE as TELEGRAM_API_BASE, Telegram
from .reminders import ReminderScheduler
from .scheduler import SchedulePoller
from .watchdog import Watchdog
from .state.db import Storage

log = logging.getLogger("hltv_notify")


def load_dotenv(path: Path) -> None:
    """Secrets come only from the environment or from a .env outside the repo."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


# How long we wait for the queue to finish writing out what has already been
# decided. Less than stop_grace_period in compose (15s): after that Docker
# sends SIGKILL, and failing to finish is better than being killed mid-write.
SHUTDOWN_DRAIN_SECONDS = 8.0


def _revoke_removed_subscribers(storage, config) -> None:
    """Take those no longer on the whitelist out of the mailing.

    The whitelist only closed the WAY IN: commands and buttons. Delivery went
    by the subscribers table, which knows nothing about the config and whose
    rows are never deleted. So removing a chat from TELEGRAM_CHAT_ID meant
    taking away control of the bot but not unsubscribing them from
    notifications — and the only fix was editing the database by hand.

    In open mode (TELEGRAM_WHITELIST_ONLY=false) nothing is touched: there is
    no list there at all. An empty list is not a reason to unsubscribe everyone
    either — that is almost certainly an unfinished .env rather than an intent.
    """
    if not config.whitelist_only:
        return
    allowed = set(config.allowed_chat_ids())
    if not allowed:
        return
    for row in storage.subscribers():
        chat = row["chat_id"]
        if chat not in allowed:
            storage.set_subscriber_enabled(chat, False)
            log.warning("chat %s is no longer in TELEGRAM_CHAT_ID — notifications "
                        "to it are switched off", chat)


def _warn_about_retired_variables(config) -> None:
    """Retired variables are obliged to announce themselves.

    `TELEGRAM_ALLOWED_CHATS` is no longer read — ids are listed in
    `TELEGRAM_CHAT_ID` separated by commas. Staying quiet here is not an
    option: if the whole list lived in the old variable, the whitelist ends up
    empty and the bot stops answering anyone at all. From the outside that
    looks like "the bot died".
    """
    if not os.environ.get("TELEGRAM_ALLOWED_CHATS", "").strip():
        return
    known = ", ".join(config.allowed_chat_ids()) or "THE LIST IS EMPTY"
    log.warning("TELEGRAM_ALLOWED_CHATS is no longer read: move the ids into "
                "TELEGRAM_CHAT_ID, separated by commas. Currently allowed: %s", known)


def setup_logging(level: str) -> None:
    # Log lines can carry non-ASCII (team names, player nicknames): on Windows
    # the console defaults to cp1252 and the first such line would bring the
    # logging handler down.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("hltv_notify").setLevel(getattr(logging, level.upper(), logging.INFO))


async def run() -> int:
    load_dotenv(Path(".env"))
    config = config_module.load()
    setup_logging(config.log_level)

    _warn_about_retired_variables(config)

    storage = Storage(config.db_path)
    # The first seed: the team from .env becomes the first tracked one. After
    # that the list lives in the database and is edited through the bot, and
    # the environment variables remain only a fallback.
    # One-off: journal keys written before subscribers existed get a recipient.
    # Otherwise the very first upgrade would send the whole history again.
    adopted = storage.adopt_legacy_event_keys(config.main_chat_id)
    if adopted:
        log.info("event journal converted to the new format: %d records", adopted)

    for chat in config.allowed_chat_ids():
        if storage.add_subscriber(chat, note="from TELEGRAM_CHAT_ID"):
            # A new subscriber gets the default reminders; after that they edit
            # them themselves.
            for minutes in config.reminder_minutes():
                storage.add_reminder(chat, minutes)
    main_chat = config.main_chat_id
    _revoke_removed_subscribers(storage, config)

    if not storage.teams(enabled_only=False) and config.team_id and main_chat:
        storage.add_team(main_chat, config.team_id, config.team_slug, config.team_name)
        log.info("first tracked team taken from the config: %s (id %s) for chat %s",
                 config.team_name, config.team_id, main_chat)
    # TELEGRAM_CHAT_ID is required even with the whitelist off: the first id
    # is the main chat — where the seed team goes, where messages go while
    # nobody has subscribed, and the only chat allowed to change the log level.
    # Without it there is nobody to talk to, so the bot is not started at all;
    # say so, because silence from a bot looks exactly like a dead service.
    if config.bot_token and not config.allowed_chat_ids():
        log.warning("TELEGRAM_CHAT_ID is empty: the bot will not be started and "
                    "nothing will be sent. Put at least your own chat id there "
                    "— it is the main chat%s",
                    ", the whitelist being off does not replace it"
                    if not config.whitelist_only else "")
    http = HltvHttp(config)
    telegram: Optional[Telegram] = (
        Telegram(config.bot_token, config.proxies_for(TELEGRAM_API_BASE))
        if config.telegram_enabled() else None)
    notifier = Notifier(storage, config, telegram)
    poller = SchedulePoller(storage, config, http, notifier)
    messenger = LiveMessenger(storage, config, telegram)
    # Handed over after the fact: the queue moves the live card back to the
    # bottom once it has delivered a milestone of the same map, and the card
    # needs the queue's notifier for the map start it may have to fall back on.
    # One of the two references has to be set second.
    notifier.live_messenger = messenger
    supervisor = LiveSupervisor(storage, config, notifier, messenger)
    matches = MatchPoller(storage, config, http, notifier, supervisor)

    if config.dry_run:
        log.warning("DRY_RUN is on: notifications go to the log, not to Telegram")
    if config.proxy.configured:
        # Always printed when a proxy is set: a silent proxy is half an hour of
        # staring at timeouts for no reason.
        log.info("%s", config.proxy.describe())
    if telegram is None:
        missing = []
        if not config.bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not config.allowed_chat_ids():
            missing.append("TELEGRAM_CHAT_ID")
        log.warning("running without Telegram: %s is not set", ", ".join(missing))

    stop = asyncio.Event()
    _install_signal_handlers(stop)

    watchdog = Watchdog(storage, config)
    reminders = ReminderScheduler(storage, config)
    # The queue is kept separate: on shutdown it must not be torn down along
    # with the rest, see below.
    outbox = asyncio.create_task(notifier.run(stop), name="outbox")
    tasks: List[asyncio.Task] = [
        asyncio.create_task(reminders.run(stop, notifier), name="reminders"),
        asyncio.create_task(poller.run(stop), name="schedule-poller"),
        asyncio.create_task(watchdog.run(stop, notifier), name="watchdog"),
        asyncio.create_task(matches.run(stop), name="match-poller"),
        outbox,
    ]
    if telegram is not None:
        bot = CommandBot(storage, config, telegram, poller, matches, http)
        tasks.append(asyncio.create_task(bot.run(stop), name="command-bot"))

    log.info("service started: %d subscriber(s), %d team(s) watched (%s), "
             "sending mode %s",
             len(storage.subscribers()), len(storage.tracked_teams()),
             ", ".join(row["name"] for row in storage.tracked_teams()) or "none",
             "dry-run" if config.dry_run else "live")

    try:
        await stop.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        stop.set()

    log.info("shutting down")
    # Everything except the queue is stopped at once: those only produce new
    # work, and new work is no longer wanted. The bot meanwhile sits in
    # getUpdates for up to 25 seconds — we cannot wait for it, Docker has its
    # own timer.
    others = [task for task in tasks if task is not outbox]
    for task in others:
        task.cancel()
    await asyncio.gather(*others, return_exceptions=True)
    await supervisor.shutdown()
    await messenger.close()

    # The queue, on the other hand, finishes what it started and exits of its
    # own accord: stop is already set. Tearing it down with a cancel is not
    # allowed — a cancel in the middle of send_message would leave the message
    # SENT but not marked in the database, and on the next start it would go to
    # the person a second time.
    try:
        await asyncio.wait_for(outbox, timeout=SHUTDOWN_DRAIN_SECONDS)
    except asyncio.TimeoutError:
        log.warning("the queue did not finish within %.0f s; the remainder (%d) will "
                    "go out on the next start", SHUTDOWN_DRAIN_SECONDS,
                    storage.pending_count())
    except asyncio.CancelledError:
        pass

    await http.close()
    if telegram is not None:
        await telegram.close()
    storage.prune(sent_days=config.outbox_keep_days, events_days=config.events_keep_days)
    storage.close()
    return 0


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows: signal handlers cannot be installed on the loop, so
            # stopping goes through Ctrl+C.
            pass


def health(config_module_=None) -> int:
    """The liveness check for Docker's HEALTHCHECK.

    It looks not at "the process is running" but at "the service is doing its
    job": the database opens and the schedule was polled not too long ago. A
    hung process looks alive to Docker, and without this check the restart
    policy would never restart it.
    """
    load_dotenv(Path(".env"))
    config = config_module.load()
    setup_logging(config.log_level)
    try:
        storage = Storage(config.db_path)
    except Exception as exc:  # noqa: BLE001 - the reason has to be shown
        print(f"unhealthy: the database will not open: {exc}")
        return 1

    try:
        last_poll = storage.get_meta("last_schedule_poll_utc")
        if not last_poll:
            # The service has only just started and has not polled yet — no
            # reason to kill it.
            print("healthy: no polls yet")
            return 0
        from .state.db import parse_iso, utcnow
        age = (utcnow() - parse_iso(last_poll)).total_seconds()
        limit = config.interval_for("idle") * 2 + 300
        if age > limit:
            print(f"unhealthy: the schedule has not been polled for {int(age)} s "
                  f"(threshold {int(limit)})")
            return 1
        print(f"healthy: last poll {int(age)} s ago")
        return 0
    finally:
        storage.close()


def main() -> int:
    if "--health" in sys.argv:
        return health()
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
