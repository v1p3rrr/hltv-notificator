"""The command list: one source for /help and for Telegram's own hint list.

The bot advertises its commands twice — in the /help text and through
setMyCommands, which is what fills the list Telegram offers when you type "/".
Both are generated from `bot.COMMANDS`, and what is checked here is that the
list does not drift away from what the bot actually answers. It had drifted
once already: /live worked and /help had never heard of it.
"""

import asyncio
import re
from pathlib import Path

import pytest

from hltv_notify import bot as bot_module
from hltv_notify.bot import COMMANDS, HELP, CommandBot, command_menu
from hltv_notify.config import Config
from hltv_notify.state.db import Storage

CHAT = "111"

# Telegram's own limits on setMyCommands.
NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")
MAX_DESCRIPTION = 256

# Dispatched by the bot but deliberately absent from the advertised list:
# Telegram sends /start itself when a chat is opened, and repeating it in the
# hint list only takes up a row.
UNADVERTISED = {"/start"}


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.commands = None

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))
        return len(self.sent)

    async def set_my_commands(self, commands):
        self.commands = commands

    async def get_updates(self, offset, timeout=25):
        return []


class FakePoller:
    mode = "idle"
    http = type("H", (), {"consecutive_failures": 0})()

    def __init__(self, storage=None):
        self.storage = storage
        self.supervisor = None

    def active(self, now=None):
        return []

    def request_poll(self):
        pass


@pytest.fixture()
def bot(tmp_path):
    storage = Storage(tmp_path / "commands.db")
    storage.add_subscriber(CHAT)
    telegram = FakeTelegram()
    command_bot = CommandBot(storage, Config(chat_id=CHAT, bot_token="t"),
                             telegram, FakePoller(storage), FakePoller(storage))
    yield command_bot, telegram
    storage.close()


def send(command_bot, text):
    update = {"message": {"chat": {"id": CHAT}, "text": text}}
    asyncio.run(command_bot._handle(update))


def test_every_advertised_command_is_answered(bot):
    """The hint list must not offer something the bot shrugs at."""
    command_bot, telegram = bot
    for name, _, _ in COMMANDS:
        send(command_bot, f"/{name}")
    replies = [text for _, text in telegram.sent]
    assert len(replies) == len(COMMANDS)
    unknown = [text for text in replies if text.startswith("I do not know")]
    assert unknown == []


def test_every_handled_command_is_advertised():
    """And the other direction, which is how /live went missing: a command the
    bot dispatches but never tells anyone about."""
    source = Path(bot_module.__file__).read_text(encoding="utf-8")
    dispatched = set(re.findall(r'command == "(/[a-z]+)"', source))
    dispatched |= set(re.findall(r'^\s+"(/[a-z]+)": ', source, re.M))
    advertised = {f"/{name}" for name, _, _ in COMMANDS} | UNADVERTISED
    assert dispatched - advertised == set()


def test_help_lists_every_command():
    for name, _, _ in COMMANDS:
        assert f"/{name}" in HELP


def test_the_payload_fits_telegrams_limits():
    payload = command_menu()
    assert len(payload) == len(COMMANDS)
    assert len(payload) <= 100
    for item in payload:
        assert NAME_RE.match(item["command"]), item
        assert 1 <= len(item["description"]) <= MAX_DESCRIPTION, item
        # Plain text: the hint list is not rendered as HTML, so an escape
        # would be shown literally.
        assert "<" not in item["description"] and "&" not in item["description"]


def test_the_list_is_registered_on_startup(bot):
    """Without this the hint list stays empty and the only way to find a
    command is to already know that /help exists."""
    command_bot, telegram = bot
    stop = asyncio.Event()
    stop.set()
    asyncio.run(command_bot.run(stop))
    assert telegram.commands == command_menu()


def test_startup_survives_telegram_refusing_the_list(bot):
    """It is a convenience, not a precondition: the bot must come up anyway."""
    from hltv_notify.notify.telegram import TelegramError

    command_bot, telegram = bot

    async def refuse(commands):
        raise TelegramError("Telegram 400: whatever")

    telegram.set_my_commands = refuse
    stop = asyncio.Event()
    stop.set()
    asyncio.run(command_bot.run(stop))  # must not raise


def test_arguments_are_escaped_in_help():
    """/help is sent with parse_mode=HTML, so <team link> has to arrive as
    text rather than as an unknown tag that Telegram would reject."""
    assert "&lt;team link&gt;" in HELP
    assert "<team link>" not in HELP
