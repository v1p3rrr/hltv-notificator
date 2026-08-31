"""Bot commands — the service's interface.

You should be able to work out why a notification did not arrive without
opening an SSH session. So what is checked is not "the command did not crash"
but that the reply contains something you can judge the state by.
"""

import asyncio
from datetime import timedelta

import pytest

from hltv_notify.bot import CommandBot
from hltv_notify.models import MatchState
from hltv_notify.state.db import Storage, utcnow

CHAT = "555"
MATCH_ID = 900


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.answered = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))
        return len(self.sent)

    async def answer_callback_query(self, callback_id, text=""):
        self.answered.append((callback_id, text))

    async def get_updates(self, offset, timeout=25):
        return []


class FakeSupervisor:
    def __init__(self, feeds=None):
        self._feeds = feeds or {}

    def connected_matches(self):
        return dict(self._feeds)


class FakePoller:
    def __init__(self, storage, supervisor=None, mode="live"):
        self.storage = storage
        self.supervisor = supervisor
        self.mode = mode
        self.forced = 0

    def active(self, now=None):
        return self.storage.active_matches(now)

    def request_poll(self):
        self.forced += 1


class FakeSchedulePoller:
    def __init__(self, http_failures=0):
        self.mode = "idle"
        self.http = type("H", (), {"consecutive_failures": http_failures})()
        self.forced = 0

    def request_poll(self):
        self.forced += 1


@pytest.fixture()
def bot(tmp_path, config, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    from hltv_notify.config import Config
    cfg = Config(chat_id=CHAT, bot_token="t", team_name="FORZE Reload")

    storage = Storage(tmp_path / "bot.db")
    storage.upsert_match(
        match_id=MATCH_ID, opponent_id=13973, opponent_name="Color",
        event_name="GLuck Qualifier", start_utc=utcnow() - timedelta(minutes=30),
        url="https://www.hltv.org/matches/900/x", snapshot={}, snapshot_hash="h")
    storage.set_state(MATCH_ID, MatchState.LIVE, source="scorebot",
                      current_map_number=2, current_map_name="Dust2",
                      current_map_score="7-5", series_score="1-0")
    storage.record_map_result(match_id=MATCH_ID, map_number=1, map_name="Mirage",
                              score_team=13, score_opponent=10, overtime=False)

    telegram = FakeTelegram()
    supervisor = FakeSupervisor({MATCH_ID: True})
    matches = FakePoller(storage, supervisor)
    command_bot = CommandBot(storage, cfg, telegram, FakeSchedulePoller(), matches)
    yield command_bot, telegram, storage
    storage.close()


def send(command_bot, text, chat=CHAT):
    update = {"update_id": 1, "message": {"chat": {"id": chat}, "text": text}}
    asyncio.run(command_bot._handle(update))


def test_status_reports_what_matters(bot):
    command_bot, telegram, _ = bot
    storage_obj = bot[2]
    storage_obj.add_team(CHAT, 12857, "forze-reload", "FORZE Reload")
    send(command_bot, "/status")
    reply = telegram.sent[-1][1]
    assert "teams watched: 1" in reply
    assert "Live feed" in reply
    assert "Matches in the database" in reply


def test_status_shows_dry_run_state(bot):
    command_bot, telegram, _ = bot
    send(command_bot, "/status")
    assert "DRY_RUN" in telegram.sent[-1][1]


def test_live_shows_score_and_source(bot):
    """When a notification did not arrive, what matters is whether the score
    reached the service at all, and from which source."""
    command_bot, telegram, _ = bot
    send(command_bot, "/live")
    reply = telegram.sent[-1][1]
    assert "Dust2" in reply
    assert "7-5" in reply
    assert "1-0" in reply
    assert "scorebot" in reply
    assert "Mirage — 13:10" in reply


def test_live_without_matches(tmp_path, bot):
    command_bot, telegram, storage = bot
    storage.set_state(MATCH_ID, MatchState.FINISHED, source="match_page")
    send(command_bot, "/live")
    assert "No matches right now" in telegram.sent[-1][1]


def test_check_forces_a_poll(bot):
    command_bot, telegram, _ = bot
    send(command_bot, "/check")
    assert command_bot.poller.forced == 1


def test_unknown_command_shows_help(bot):
    command_bot, telegram, _ = bot
    send(command_bot, "/somethingelse")
    assert "I do not know that command" in telegram.sent[-1][1]


def test_commands_from_other_chats_are_ignored(bot):
    """The bot answers only whitelisted accounts. A stranger gets silence:
    answering with a refusal confirms the bot exists to anyone who probes."""
    command_bot, telegram, _ = bot
    send(command_bot, "/status", chat="999")
    assert telegram.sent == []


def test_a_stranger_cannot_flood_the_log(bot, caplog):
    """The refusal line is how the owner reads the id of a chat that is not on
    the list yet — and the only thing an outsider can make the bot do. One line
    per message would let them push the useful history out of a rotated log, so
    a chat is written about once and then left alone for a while."""
    import logging

    from hltv_notify.bot import REFUSAL_LOG_INTERVAL

    command_bot, telegram, _ = bot
    with caplog.at_level(logging.WARNING, logger="hltv_notify.bot"):
        for _ in range(50):
            send(command_bot, "/status", chat="999")
    refusals = [r for r in caplog.records if "refused" in r.getMessage()]
    assert len(refusals) == 1
    assert "999" in refusals[0].getMessage()

    # A different chat is still seen at once.
    with caplog.at_level(logging.WARNING, logger="hltv_notify.bot"):
        send(command_bot, "/status", chat="888")
    assert len([r for r in caplog.records if "refused" in r.getMessage()]) == 2

    # And the same one is heard again once the interval has passed.
    command_bot._refused["999"] -= REFUSAL_LOG_INTERVAL + 1
    with caplog.at_level(logging.WARNING, logger="hltv_notify.bot"):
        send(command_bot, "/status", chat="999")
    assert len([r for r in caplog.records if "refused" in r.getMessage()]) == 3
    assert telegram.sent == []


def test_whoami_is_silent_to_strangers(bot):
    """There is no exception to the whitelist. /whoami used to be one, so that
    a newcomer could learn their own id — but it also handed anyone who found
    the bot a command that always answers. @userinfobot reports the same
    number without involving this bot at all."""
    command_bot, telegram, _ = bot
    send(command_bot, "/whoami", chat="999")
    assert telegram.sent == []


def test_whoami_answers_an_allowed_chat(bot):
    command_bot, telegram, _ = bot
    send(command_bot, "/whoami")
    assert CHAT in telegram.sent[-1][1]


def test_only_the_main_chat_changes_the_log_level(bot, monkeypatch):
    """The log level belongs to the whole process, unlike every other command,
    which touches only the caller's own subscription."""
    from hltv_notify.config import Config

    command_bot, telegram, _ = bot
    command_bot.config = Config(chat_id=f"{CHAT},222", bot_token="t")
    send(command_bot, "/verbose on", chat="222")
    assert "Only the main chat" in telegram.sent[-1][1]
    send(command_bot, "/verbose on")
    assert "Verbose mode on" in telegram.sent[-1][1]


def test_allowed_chat_becomes_a_subscriber(bot):
    """An allowed chat becomes a subscriber on its first message: otherwise it
    would have to be written into the database by hand."""
    command_bot, telegram, storage = bot
    assert storage.get_subscriber(CHAT) is None
    send(command_bot, "/status")
    assert storage.get_subscriber(CHAT) is not None


def test_verbose_toggles_and_reports(bot):
    command_bot, telegram, _ = bot
    send(command_bot, "/verbose on")
    assert "Verbose mode on" in telegram.sent[-1][1]
    send(command_bot, "/verbose off")
    assert "Verbose mode off" in telegram.sent[-1][1]
    send(command_bot, "/verbose")
    assert "Usage" in telegram.sent[-1][1]


def test_next_lists_upcoming(bot):
    command_bot, telegram, storage = bot
    storage.upsert_match(
        match_id=901, opponent_id=1, opponent_name="ex-RUSTEC",
        event_name="Kibertochka", start_utc=utcnow() + timedelta(hours=3),
        url="https://www.hltv.org/matches/901/y", snapshot={}, snapshot_hash="h")
    send(command_bot, "/next")
    reply = telegram.sent[-1][1]
    assert "ex-RUSTEC" in reply and "Kibertochka" in reply


def test_failed_command_still_answers(bot, monkeypatch):
    """An answer has to go out regardless, otherwise the command looks hung."""
    command_bot, telegram, _ = bot
    monkeypatch.setattr(command_bot, "_status",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ouch")))
    send(command_bot, "/status")
    assert "crashed" in telegram.sent[-1][1]


# ---------------------------------------------------------------- per-chat answers


def test_live_names_the_teams_of_that_match(bot):
    """The team name comes from the match, not from the config: there can be
    several tracked teams and the .env value would only fit the first."""
    command_bot, telegram, storage = bot
    storage.add_subscriber(CHAT)
    storage.add_team(CHAT, 4494, "mouz", "MOUZ")
    storage.link_match_team(MATCH_ID, 4494)
    send(command_bot, "/live")
    reply = telegram.sent[-1][1]
    assert "MOUZ — Color" in reply
    assert "FORZE Reload" not in reply


def test_next_shows_only_your_own_matches(bot):
    """Accounts keep their own team lists, and /next must not show other
    people's."""
    command_bot, telegram, storage = bot
    other_chat = "777"
    storage.add_subscriber(CHAT)
    storage.add_subscriber(other_chat)
    storage.add_team(CHAT, 12857, "forze-reload", "FORZE Reload")
    storage.add_team(other_chat, 4494, "mouz", "MOUZ")

    for match_id, team_id, opponent in ((901, 12857, "Color"), (902, 4494, "Vitality")):
        storage.upsert_match(
            match_id=match_id, team_id=team_id, opponent_id=1, opponent_name=opponent,
            event_name="Major", start_utc=utcnow() + timedelta(hours=1),
            url=f"https://www.hltv.org/matches/{match_id}/x",
            snapshot={}, snapshot_hash="h")
        storage.link_match_team(match_id, team_id)

    send(command_bot, "/next")
    reply = telegram.sent[-1][1]
    assert "Color" in reply
    assert "Vitality" not in reply


def test_next_uses_the_personal_timezone(bot):
    """Notifications arrive in the personal timezone — /next must answer in the
    same one, otherwise the same match is shown at two different times."""
    command_bot, telegram, storage = bot
    storage.add_subscriber(CHAT)
    storage.add_team(CHAT, 12857, "forze-reload", "FORZE Reload")
    storage.upsert_match(
        match_id=903, team_id=12857, opponent_id=1, opponent_name="Color",
        event_name="Major",
        start_utc=utcnow().replace(hour=12, minute=0) + timedelta(days=1),
        url="https://www.hltv.org/matches/903/x", snapshot={}, snapshot_hash="h")
    storage.link_match_team(903, 12857)

    send(command_bot, "/next")
    moscow = telegram.sent[-1][1]
    storage.set_subscriber_timezone(CHAT, "UTC")
    send(command_bot, "/next")
    assert telegram.sent[-1][1] != moscow
