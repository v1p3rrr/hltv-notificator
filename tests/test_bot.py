"""Команды боту — интерфейс сервиса.

Пользователь должен понимать, почему уведомление не пришло, не подключаясь по
SSH. Поэтому проверяется не «команда не упала», а что в ответе есть то, по
чему можно судить о состоянии.
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
    assert "команд под наблюдением: 1" in reply
    assert "Живой фид" in reply
    assert "Матчей в базе" in reply


def test_status_shows_dry_run_state(bot):
    command_bot, telegram, _ = bot
    send(command_bot, "/status")
    assert "DRY_RUN" in telegram.sent[-1][1]


def test_live_shows_score_and_source(bot):
    """Когда уведомление не пришло, важно понять: счёт до сервиса вообще
    дошёл, и от какого источника."""
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
    assert "Сейчас матчей нет" in telegram.sent[-1][1]


def test_check_forces_a_poll(bot):
    command_bot, telegram, _ = bot
    send(command_bot, "/check")
    assert command_bot.poller.forced == 1


def test_unknown_command_shows_help(bot):
    command_bot, telegram, _ = bot
    send(command_bot, "/somethingelse")
    assert "Не знаю такой команды" in telegram.sent[-1][1]


def test_commands_from_other_chats_are_ignored(bot):
    """Бот отвечает только аккаунтам из белого списка. Чужому — молчание:
    отвечать отказом значит подтверждать существование бота кому попало."""
    command_bot, telegram, _ = bot
    send(command_bot, "/status", chat="999")
    assert telegram.sent == []


def test_whoami_answers_anyone(bot):
    """Единственное исключение: свой chat_id человек должен узнать, иначе его
    некому внести в белый список."""
    command_bot, telegram, _ = bot
    send(command_bot, "/whoami", chat="999")
    assert "999" in telegram.sent[-1][1]


def test_allowed_chat_becomes_a_subscriber(bot):
    """Разрешённый чат заводится подписчиком при первом обращении: иначе его
    пришлось бы прописывать в базе руками."""
    command_bot, telegram, storage = bot
    assert storage.get_subscriber(CHAT) is None
    send(command_bot, "/status")
    assert storage.get_subscriber(CHAT) is not None


def test_verbose_toggles_and_reports(bot):
    command_bot, telegram, _ = bot
    send(command_bot, "/verbose on")
    assert "включён" in telegram.sent[-1][1]
    send(command_bot, "/verbose off")
    assert "выключен" in telegram.sent[-1][1]
    send(command_bot, "/verbose")
    assert "Использование" in telegram.sent[-1][1]


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
    """Ответить надо в любом случае, иначе команда выглядит зависшей."""
    command_bot, telegram, _ = bot
    monkeypatch.setattr(command_bot, "_status",
                        lambda: (_ for _ in ()).throw(RuntimeError("боль")))
    send(command_bot, "/status")
    assert "упала" in telegram.sent[-1][1]
