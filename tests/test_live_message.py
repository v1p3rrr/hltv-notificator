"""Живое сообщение на карту: создание, троттлинг, заморозка, устойчивость."""

import asyncio

import pytest

from hltv_notify.config import Config
from hltv_notify.notify.live_message import HARD_MIN_EDIT_SECONDS, LiveMessenger
from hltv_notify.notify.telegram import TelegramError
from hltv_notify.state.db import Storage

MATCH_ID = 42
CHAT = "1"   # chat_id из live_config()


class FakeTelegram:
    def __init__(self, fail_edit=False):
        self.sent = []
        self.edited = []
        self.fail_edit = fail_edit

    async def send_message(self, chat_id, text):
        self.sent.append(text)
        return 1000 + len(self.sent)

    async def edit_message_text(self, chat_id, message_id, text):
        if self.fail_edit:
            raise TelegramError("Telegram 429: Too Many Requests", retry_after=5)
        self.edited.append((message_id, text))


def snapshot(score=(3, 2), rnd=6, map_number=1):
    return {
        "map_number": map_number, "map_name": "Mirage",
        "score_team": score[0], "score_opponent": score[1],
        "round": rnd, "round_state": "started", "in_play": True,
        "series_team": 0, "series_opponent": 0,
        "opponent": "Color", "event_name": "Test", "url": "https://example.test/m",
    }


def live_config(**overrides) -> Config:
    base = dict(dry_run=False, bot_token="t", chat_id="1", live_edit_seconds=0)
    base.update(overrides)
    return Config(**base)


@pytest.fixture()
def messenger(tmp_path):
    storage = Storage(tmp_path / "live.db")
    storage.upsert_match(match_id=MATCH_ID, opponent_id=1, opponent_name="Color",
                         event_name="Test", start_utc=__import__("hltv_notify.state.db",
                         fromlist=["utcnow"]).utcnow(), url="u",
                         snapshot={}, snapshot_hash="h")
    telegram = FakeTelegram()
    yield LiveMessenger(storage, live_config(), telegram), telegram, storage
    storage.close()


def test_first_update_creates_a_message(messenger):
    m, telegram, storage = messenger
    asyncio.run(m.update(MATCH_ID, snapshot()))
    assert len(telegram.sent) == 1
    row = storage.live_message(CHAT, MATCH_ID, 1)
    assert row["telegram_message_id"] == 1001


def test_second_update_edits_instead_of_sending(messenger):
    m, telegram, _ = messenger
    asyncio.run(m.update(MATCH_ID, snapshot(score=(3, 2))))
    asyncio.run(m.update(MATCH_ID, snapshot(score=(4, 2)), force=True))
    assert len(telegram.sent) == 1
    assert len(telegram.edited) == 1
    assert telegram.edited[0][0] == 1001


def test_hard_minimum_interval_cannot_be_configured_away(messenger):
    """Конфиг может просить обновлять хоть каждую секунду — Telegram этого не
    любит, поэтому нижняя граница зашита в код."""
    m, telegram, _ = messenger
    assert m._interval == HARD_MIN_EDIT_SECONDS
    asyncio.run(m.update(MATCH_ID, snapshot(score=(1, 0))))
    for kills in range(2, 12):
        asyncio.run(m.update(MATCH_ID, snapshot(score=(kills, 0))))
    assert len(telegram.sent) == 1
    assert telegram.edited == []      # всё отсеяно троттлингом


def test_unchanged_score_does_not_burn_the_limit(messenger):
    m, telegram, _ = messenger
    same = snapshot(score=(5, 5))
    asyncio.run(m.update(MATCH_ID, same))
    asyncio.run(m.update(MATCH_ID, same, force=True))
    assert telegram.edited == []


def test_finalize_freezes_the_message(messenger):
    m, telegram, storage = messenger
    asyncio.run(m.update(MATCH_ID, snapshot(score=(12, 9))))
    asyncio.run(m.finalize(MATCH_ID, snapshot(score=(13, 9))))
    assert storage.live_message(CHAT, MATCH_ID, 1)["finalized"] == 1

    asyncio.run(m.update(MATCH_ID, snapshot(score=(99, 0)), force=True))
    assert len(telegram.edited) == 1      # после заморозки правок больше нет


def test_message_id_survives_restart(tmp_path):
    """Иначе после перезапуска на ту же карту завелось бы второе сообщение."""
    from hltv_notify.state.db import utcnow

    path = tmp_path / "restart.db"
    first = Storage(path)
    first.upsert_match(match_id=MATCH_ID, opponent_id=1, opponent_name="Color",
                       event_name="Test", start_utc=utcnow(), url="u",
                       snapshot={}, snapshot_hash="h")
    telegram = FakeTelegram()
    asyncio.run(LiveMessenger(first, live_config(), telegram).update(MATCH_ID, snapshot()))
    first.close()

    second = Storage(path)
    asyncio.run(LiveMessenger(second, live_config(), telegram)
                .update(MATCH_ID, snapshot(score=(9, 1)), force=True))
    assert len(telegram.sent) == 1        # второе сообщение не заводилось
    assert len(telegram.edited) == 1
    second.close()


def test_telegram_failure_does_not_break_the_worker(tmp_path):
    """Живое сообщение вспомогательное: из-за него нельзя терять вехи."""
    from hltv_notify.state.db import utcnow

    storage = Storage(tmp_path / "fail.db")
    storage.upsert_match(match_id=MATCH_ID, opponent_id=1, opponent_name="Color",
                         event_name="Test", start_utc=utcnow(), url="u",
                         snapshot={}, snapshot_hash="h")
    telegram = FakeTelegram(fail_edit=True)
    m = LiveMessenger(storage, live_config(), telegram)
    asyncio.run(m.update(MATCH_ID, snapshot(score=(1, 0))))
    asyncio.run(m.update(MATCH_ID, snapshot(score=(2, 0)), force=True))   # не бросает
    storage.close()


def test_dry_run_touches_no_telegram(tmp_path):
    from hltv_notify.state.db import utcnow

    storage = Storage(tmp_path / "dry.db")
    storage.upsert_match(match_id=MATCH_ID, opponent_id=1, opponent_name="Color",
                         event_name="Test", start_utc=utcnow(), url="u",
                         snapshot={}, snapshot_hash="h")
    telegram = FakeTelegram()
    m = LiveMessenger(storage, live_config(dry_run=True), telegram)
    asyncio.run(m.update(MATCH_ID, snapshot()))
    assert telegram.sent == [] and telegram.edited == []
    assert storage.live_message(CHAT, MATCH_ID, 1) is not None
    storage.close()


def test_disabled_by_config(tmp_path):
    from hltv_notify.state.db import utcnow

    storage = Storage(tmp_path / "off.db")
    storage.upsert_match(match_id=MATCH_ID, opponent_id=1, opponent_name="Color",
                         event_name="Test", start_utc=utcnow(), url="u",
                         snapshot={}, snapshot_hash="h")
    telegram = FakeTelegram()
    m = LiveMessenger(storage, live_config(live_message=False), telegram)
    asyncio.run(m.update(MATCH_ID, snapshot()))
    assert telegram.sent == []
    assert storage.live_message(CHAT, MATCH_ID, 1) is None
    storage.close()
