"""The inline menu: button presses.

A button must always be answered — otherwise Telegram spins the indicator
until it times out and the person thinks the bot has hung. So what is checked
here is not only the effect of the press but the fact of the answer itself.
"""

import asyncio

import pytest

from hltv_notify import menu
from hltv_notify.bot import CommandBot
from hltv_notify.config import Config
from hltv_notify.state.db import Storage, utcnow

CHAT = "111"
STRANGER = "999"
TEAM = 12857


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.edited = []
        self.answered = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return len(self.sent)

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edited.append((chat_id, message_id, text, reply_markup))

    async def answer_callback_query(self, callback_id, text=""):
        self.answered.append((callback_id, text))

    async def get_updates(self, offset, timeout=25):
        return []


class FakePoller:
    mode = "idle"
    http = type("H", (), {"consecutive_failures": 0})()

    def __init__(self, storage=None):
        self.storage = storage
        self.supervisor = None
        self.forced = 0

    def active(self, now=None):
        return []

    def request_poll(self):
        self.forced += 1


@pytest.fixture()
def bot(tmp_path):
    storage = Storage(tmp_path / "menu.db")
    storage.add_subscriber(CHAT)
    storage.add_team(CHAT, TEAM, "forze-reload", "FORZE Reload")
    cfg = Config(chat_id=CHAT, bot_token="t")
    telegram = FakeTelegram()
    command_bot = CommandBot(storage, cfg, telegram, FakePoller(storage),
                             FakePoller(storage))
    yield command_bot, telegram, storage
    storage.close()


def press(command_bot, data, chat=CHAT):
    query = {"id": "cb1", "data": data,
             "message": {"message_id": 7, "chat": {"id": chat}}}
    asyncio.run(command_bot._handle_callback(query))


# ---------------------------------------------------------------- basics


def test_every_press_is_acknowledged(bot):
    command_bot, telegram, _ = bot
    press(command_bot, "m:main")
    assert telegram.answered           # without this the button keeps spinning


def test_unknown_data_does_not_crash(bot):
    command_bot, telegram, _ = bot
    press(command_bot, "some nonsense")
    assert telegram.answered
    assert telegram.edited == []


def test_press_from_a_stranger_is_refused(bot):
    command_bot, telegram, _ = bot
    press(command_bot, "m:status", chat=STRANGER)
    assert telegram.answered[-1][1] == "No access"
    assert telegram.edited == []


def test_section_redraws_the_same_message(bot):
    """Sections redraw the message instead of spawning new ones."""
    command_bot, telegram, _ = bot
    press(command_bot, "m:status")
    assert len(telegram.edited) == 1
    assert telegram.edited[0][1] == 7
    assert telegram.sent == []


# ---------------------------------------------------------------- pause


def test_pause_button_toggles(bot):
    command_bot, telegram, storage = bot
    press(command_bot, "p:on")
    assert storage.subscriber_paused(CHAT) is True
    press(command_bot, "p:off")
    assert storage.subscriber_paused(CHAT) is False


def test_pause_button_swaps_its_label(bot):
    command_bot, telegram, storage = bot
    press(command_bot, "p:on")
    labels = [b["text"] for row in telegram.edited[-1][3]["inline_keyboard"] for b in row]
    assert any("Turn notifications on" in label for label in labels)


# ---------------------------------------------------------------- reminders


def test_reminder_button_adds_and_removes(bot):
    command_bot, telegram, storage = bot
    press(command_bot, "r:15")
    assert storage.reminders(CHAT) == [15]
    press(command_bot, "r:15")
    assert storage.reminders(CHAT) == []


def test_reminder_marks_active_presets(bot):
    command_bot, telegram, storage = bot
    storage.add_reminder(CHAT, 60)
    press(command_bot, "m:rem")
    labels = [b["text"] for row in telegram.edited[-1][3]["inline_keyboard"] for b in row]
    assert any(label.startswith("✅") and "1 h" in label for label in labels)


# ---------------------------------------------------------------- teams


def test_team_menu_lists_event_types(bot):
    command_bot, telegram, _ = bot
    press(command_bot, f"t:{TEAM}")
    data = [b["callback_data"] for row in telegram.edited[-1][3]["inline_keyboard"]
            for b in row]
    assert f"t:{TEAM}:x:E6" in data
    assert f"t:{TEAM}:rm" in data


def test_mute_toggle_button(bot):
    command_bot, telegram, storage = bot
    press(command_bot, f"t:{TEAM}:x:E6")
    assert storage.team_mutes(CHAT, TEAM) == ["E6"]
    press(command_bot, f"t:{TEAM}:x:E6")
    assert storage.team_mutes(CHAT, TEAM) == []


def test_untrack_button_disables_the_team(bot):
    command_bot, telegram, storage = bot
    press(command_bot, f"t:{TEAM}:rm")
    assert storage.get_team(CHAT, TEAM)["enabled"] == 0
    # and the menu offers to switch it back on
    data = [b["callback_data"] for row in telegram.edited[-1][3]["inline_keyboard"]
            for b in row]
    assert f"t:{TEAM}:on" in data


def test_team_of_another_subscriber_is_not_reachable(bot):
    command_bot, telegram, storage = bot
    press(command_bot, "t:99999")
    assert "do not have such a team" in telegram.edited[-1][2]


# ---------------------------------------------------------------- consistency


def test_text_command_and_buttons_share_one_list():
    """There must not be two lists of mutable types: they would drift apart
    and a button would start muting what the text command cannot."""
    from hltv_notify.bot import MUTABLE_EVENTS

    assert MUTABLE_EVENTS == tuple(code for code, _ in menu.MUTABLE)


def test_service_alerts_cannot_be_muted():
    """E8 is deliberately not on the list: muting it means possibly never
    learning that the service has gone blind."""
    codes = [code for code, _ in menu.MUTABLE]
    assert "E8" not in codes and "E8R" not in codes


def test_callback_data_fits_telegram_limit():
    """callback_data is capped at 64 bytes."""
    keyboards = [menu.main(False), menu.reminders([15]),
                 menu.team(999999, "X", ["E6"], True)]
    for board in keyboards:
        for row in board["inline_keyboard"]:
            for button in row:
                assert len(button["callback_data"].encode("utf-8")) <= 64
