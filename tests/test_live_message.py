"""The live message per map: creation, throttling, freezing, resilience."""

import asyncio

import pytest

from hltv_notify.config import Config
from hltv_notify.notify.live_message import HARD_MIN_EDIT_SECONDS, LiveMessenger
from hltv_notify.notify.telegram import TelegramError
from hltv_notify.state.db import Storage

MATCH_ID = 42
CHAT = "1"   # the chat_id from live_config()


class FakeTelegram:
    def __init__(self, fail_edit=False):
        self.sent = []
        self.answered = []
        self.edited = []
        self.fail_edit = fail_edit

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append(text)
        return 1000 + len(self.sent)

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
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
    """The config may ask for an update every second — Telegram dislikes that,
    so the lower bound is hardcoded."""
    m, telegram, _ = messenger
    assert m._interval == HARD_MIN_EDIT_SECONDS
    asyncio.run(m.update(MATCH_ID, snapshot(score=(1, 0))))
    for kills in range(2, 12):
        asyncio.run(m.update(MATCH_ID, snapshot(score=(kills, 0))))
    assert len(telegram.sent) == 1
    assert telegram.edited == []      # all filtered out by the throttle


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
    assert len(telegram.edited) == 1      # after freezing there are no more edits


def test_message_id_survives_restart(tmp_path):
    """Otherwise a restart would start a second message for the same map."""
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
    assert len(telegram.sent) == 1        # no second message was created
    assert len(telegram.edited) == 1
    second.close()


def test_telegram_failure_does_not_break_the_worker(tmp_path):
    """The live message is auxiliary: milestones must not be lost over it."""
    from hltv_notify.state.db import utcnow

    storage = Storage(tmp_path / "fail.db")
    storage.upsert_match(match_id=MATCH_ID, opponent_id=1, opponent_name="Color",
                         event_name="Test", start_utc=utcnow(), url="u",
                         snapshot={}, snapshot_hash="h")
    telegram = FakeTelegram(fail_edit=True)
    m = LiveMessenger(storage, live_config(), telegram)
    asyncio.run(m.update(MATCH_ID, snapshot(score=(1, 0))))
    asyncio.run(m.update(MATCH_ID, snapshot(score=(2, 0)), force=True))   # does not raise
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


# ------------------------------------------------- the map card (E5 merged in)


def test_the_map_card_carries_the_start(messenger):
    """One message per map instead of two.

    They used to be separate, and the live message always won the race to the
    chat: it goes straight to Telegram while events wait in the queue, so
    "the map has started" arrived seconds AFTER the score for that map.
    """
    m, telegram, _ = messenger
    missed = asyncio.run(m.update(MATCH_ID, snapshot(score=(0, 0), rnd=1),
                                  map_started=True))
    assert missed == []
    assert len(telegram.sent) == 1
    text = telegram.sent[0]
    assert "Map 1: Mirage" in text          # what E5 used to say
    assert "0:0" in text                     # and the score, from the start
    assert "Test" in text                    # the event name came along too


def test_the_card_keeps_its_shape_while_the_score_moves(messenger):
    """The heading must not change on the way: it is one message being edited."""
    m, telegram, _ = messenger
    asyncio.run(m.update(MATCH_ID, snapshot(score=(0, 0), rnd=1), map_started=True))
    asyncio.run(m.update(MATCH_ID, snapshot(score=(13, 5), rnd=18), force=True))
    assert len(telegram.sent) == 1
    assert telegram.edited[-1][1].startswith("🗺")
    assert "13:5" in telegram.edited[-1][1]


def test_a_muted_map_start_leaves_the_plain_score(messenger):
    """Muting E5 asked for exactly that: no map-start framing, just the score.
    The queue will not send them E5 either, so nothing is lost."""
    m, telegram, storage = messenger
    storage.add_subscriber(CHAT)
    storage.add_team(CHAT, 555, "x", "X")
    storage.link_match_team(MATCH_ID, 555)
    storage.set_team_mutes(CHAT, 555, ["E5"])

    missed = asyncio.run(m.update(MATCH_ID, snapshot(score=(0, 0), rnd=1),
                                  map_started=True))
    assert missed == []                      # nothing to fall back on
    # The plain form still names the map, but on the second line, as a detail
    # of the score rather than as the heading.
    first_line = telegram.sent[0].splitlines()[0]
    assert first_line.startswith("🎯")
    assert "Map 1: Mirage" not in first_line


def test_a_failed_card_is_reported_for_the_fallback(tmp_path):
    """A milestone must not be lost on the best-effort path: if the card cannot
    be created, the caller is told which chat needs a plain E5 through the
    queue."""
    from hltv_notify.state.db import utcnow

    storage = Storage(tmp_path / "fail.db")
    storage.upsert_match(match_id=MATCH_ID, opponent_id=1, opponent_name="Color",
                         event_name="Test", start_utc=utcnow(), url="u",
                         snapshot={}, snapshot_hash="h")

    class Broken(FakeTelegram):
        async def send_message(self, chat_id, text, reply_markup=None):
            raise TelegramError("Telegram 400: chat not found", fatal=True)

    telegram = Broken()
    m = LiveMessenger(storage, live_config(), telegram)
    missed = asyncio.run(m.update(MATCH_ID, snapshot(score=(0, 0), rnd=1),
                                  map_started=True))
    assert missed == [CHAT]
    storage.close()


def test_the_first_card_is_not_delayed_by_the_throttle(messenger):
    """The throttle exists to spare Telegram's limits on EDITS. Holding the
    first message back would delay the map's card by the whole interval."""
    m, telegram, _ = messenger
    m.config = live_config(live_edit_seconds=600)
    asyncio.run(m.update(MATCH_ID, snapshot(score=(0, 0), rnd=1), map_started=True))
    assert len(telegram.sent) == 1
