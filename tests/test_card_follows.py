"""The live card follows the conversation down.

The card is what a person watches during a map, and it is a fixed message in a
moving chat: a map point or a half-time message pushes it out of view. So after
one of those the card is deleted and sent again, below.

The interesting part is the seam. Events go through the queue; the card goes
straight to Telegram. Those two paths are kept apart on purpose, and the move
has to work anyway — including at half time, which is exactly when the feed
falls silent and no frame is coming to trigger a redraw.
"""

import asyncio

import pytest

from hltv_notify.config import Config
from hltv_notify.models import Event
from hltv_notify.notify.live_message import LiveMessenger
from hltv_notify.notify.outbox import Notifier
from hltv_notify.notify.telegram import TelegramError
from hltv_notify.state.db import Storage, utcnow

MATCH_ID = 42
OTHER_MATCH = 43
CHAT = "1"
TEAM_ID = 12857


class FakeTelegram:
    def __init__(self, *, fail_delete=False):
        self.sent = []
        self.edited = []
        self.deleted = []
        self.calls = []          # everything, in order
        self.fail_delete = fail_delete

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append(text)
        self.calls.append(("send", text))
        return 1000 + len(self.sent)

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edited.append((message_id, text))
        self.calls.append(("edit", message_id))

    async def delete_message(self, chat_id, message_id):
        if self.fail_delete:
            raise TelegramError("Telegram 400: message can't be deleted", fatal=True)
        self.deleted.append(message_id)
        self.calls.append(("delete", message_id))


def snapshot(score=(6, 6), rnd=13, map_number=1):
    return {
        "map_number": map_number, "map_name": "Mirage",
        "score_team": score[0], "score_opponent": score[1],
        "round": rnd, "round_state": "started", "in_play": True,
        "series_team": 0, "series_opponent": 0,
        "opponent": "Color", "event_name": "Test", "url": "https://example.test/m",
    }


def live_config(**overrides) -> Config:
    # phase_alerts on, or E12 never reaches anybody and there is nothing to
    # move the card for: it is off by default and per person (/settings phase).
    base = dict(dry_run=False, bot_token="t", chat_id=CHAT, live_edit_seconds=0,
                phase_alerts=True)
    base.update(overrides)
    return Config(**base)


def half_time(match_id=MATCH_ID) -> Event:
    return Event(type="E12", idempotency_key=f"E12:{match_id}:map:1:half",
                 match_id=match_id,
                 payload={"map_name": "Mirage", "map_number": 1, "overtime": 0,
                          "score_team": 6, "score_opponent": 6,
                          "opponent": "Color", "team_id": TEAM_ID,
                          "url": "https://example.test/m"})


def multikill() -> Event:
    return Event(type="E9", idempotency_key="E9:42:r13:4", match_id=MATCH_ID,
                 payload={"kills": 4, "player": "sh1ro", "team_id": TEAM_ID,
                          "team_name": "FORZE Reload", "opponent": "Color",
                          "map_name": "Mirage", "round": 13,
                          "score_team": 6, "score_opponent": 6,
                          "url": "https://example.test/m"})


@pytest.fixture()
def world(tmp_path):
    """A chat with a card already on screen, and a queue wired to it."""
    storage = Storage(tmp_path / "card.db")
    for match_id in (MATCH_ID, OTHER_MATCH):
        storage.upsert_match(match_id=match_id, opponent_id=1, opponent_name="Color",
                             event_name="Test", start_utc=utcnow(), url="u",
                             snapshot={}, snapshot_hash="h")
    telegram = FakeTelegram()
    config = live_config()
    messenger = LiveMessenger(storage, config, telegram)
    notifier = Notifier(storage, config, telegram, live_messenger=messenger)
    # The card is on screen, id 1001.
    asyncio.run(messenger.update(MATCH_ID, snapshot()))
    assert storage.live_message(CHAT, MATCH_ID, 1)["telegram_message_id"] == 1001
    telegram.calls.clear()
    yield messenger, notifier, telegram, storage
    storage.close()


def drain(notifier):
    asyncio.run(notifier._drain())


# ---------- the ordinary case ----------

def test_half_time_moves_the_card_below_it(world):
    messenger, notifier, telegram, storage = world
    notifier.enqueue(half_time())
    drain(notifier)

    # The half-time message goes out, then the old card is deleted, then the
    # card is sent again — in that order, so it lands underneath.
    assert [kind for kind, _ in telegram.calls] == ["send", "delete", "send"]
    assert telegram.deleted == [1001]
    assert storage.live_message(CHAT, MATCH_ID, 1)["telegram_message_id"] == 1003


def test_the_moved_card_keeps_the_score_it_had(world):
    """The text comes from the database, not from a fresh snapshot — that is
    what lets the move happen with no feed at all."""
    messenger, notifier, telegram, storage = world
    before = storage.live_message(CHAT, MATCH_ID, 1)["last_text"]
    notifier.enqueue(half_time())
    drain(notifier)
    assert telegram.sent[-1] == before


def test_the_next_redraw_edits_the_new_message(world):
    messenger, notifier, telegram, storage = world
    notifier.enqueue(half_time())
    drain(notifier)
    # The move counts as a write, so the interval applies to what follows it;
    # stepping over it here, the point being WHICH message gets edited.
    messenger._last_edit.clear()
    asyncio.run(messenger.update(MATCH_ID, snapshot(score=(7, 6), rnd=14)))
    assert telegram.edited[-1][0] == 1003


def test_a_map_point_moves_it_too(world):
    messenger, notifier, telegram, storage = world
    notifier.enqueue(Event(
        type="E11", idempotency_key="E11:42:map:1:12", match_id=MATCH_ID,
        payload={"map_name": "Mirage", "map_number": 1, "opponent": "Color",
                 "score_team": 12, "score_opponent": 6, "team_id": TEAM_ID,
                 "decides_match": False, "url": "https://example.test/m"}))
    drain(notifier)
    assert telegram.deleted == [1001]


# ---------- what must NOT move it ----------

def test_a_multikill_leaves_the_card_where_it_is(world):
    """There are several a map. A card that deletes and re-posts itself after
    each would jump around the chat and spend the rate budget doing it."""
    messenger, notifier, telegram, storage = world
    notifier.enqueue(multikill())
    drain(notifier)
    assert telegram.deleted == []
    assert storage.live_message(CHAT, MATCH_ID, 1)["telegram_message_id"] == 1001


def test_a_milestone_of_another_match_leaves_it_alone(world):
    messenger, notifier, telegram, storage = world
    notifier.enqueue(half_time(match_id=OTHER_MATCH))
    drain(notifier)
    assert telegram.deleted == []


def test_a_finalized_card_is_never_moved(world):
    """The map is over; its final score is meant to stay where it was written."""
    messenger, notifier, telegram, storage = world
    asyncio.run(messenger.finalize(MATCH_ID, snapshot(score=(13, 6), rnd=19)))
    telegram.calls.clear()
    notifier.enqueue(half_time())
    drain(notifier)
    assert telegram.deleted == []


def test_a_burst_costs_one_move(world):
    """A map point and then half time in the same pass: one move, not two."""
    messenger, notifier, telegram, storage = world
    notifier.enqueue(Event(
        type="E11", idempotency_key="E11:42:map:1:12", match_id=MATCH_ID,
        payload={"map_name": "Mirage", "map_number": 1, "opponent": "Color",
                 "score_team": 12, "score_opponent": 6, "team_id": TEAM_ID,
                 "decides_match": False, "url": "https://example.test/m"}))
    notifier.enqueue(half_time())
    drain(notifier)
    assert telegram.deleted == [1001]
    assert [kind for kind, _ in telegram.calls] == ["send", "send", "delete", "send"]


# ---------- the counter, and the race it exists for ----------

def test_a_burial_during_the_move_is_not_lost(world):
    """A flag would erase it.

    The move reads the counter, spends a second deleting and sending, and
    writes back the value it READ. An E12 that landed inside that second is
    therefore still ahead, and the card moves again. Had the move cleared a
    boolean instead, the card would sit above that message for the rest of the
    map.
    """
    messenger, notifier, telegram, storage = world
    storage.bury_live_card(CHAT, MATCH_ID)
    row = storage.live_message(CHAT, MATCH_ID, 1)
    seen = row["bury_seq"]
    # ... a second burial lands while the move is in flight ...
    storage.bury_live_card(CHAT, MATCH_ID)
    # ... and the move finishes, writing back only what it saw.
    storage.save_live_message(CHAT, MATCH_ID, 1, telegram_message_id=1002,
                              text=row["last_text"], posted_seq=seen)
    assert storage.buried_live_messages(CHAT)


def test_moving_it_clears_the_burial(world):
    messenger, notifier, telegram, storage = world
    storage.bury_live_card(CHAT, MATCH_ID)
    asyncio.run(messenger.repost_buried(CHAT))
    assert storage.buried_live_messages(CHAT) == []


# ---------- failure ----------

def test_a_refused_delete_leaves_one_card_and_is_not_retried(tmp_path):
    """Telegram refuses deletes it considers impossible.

    The card then stays where it is — better than two copies — and the burial
    is written off rather than retried on every frame for the rest of the map.
    """
    storage = Storage(tmp_path / "card.db")
    storage.upsert_match(match_id=MATCH_ID, opponent_id=1, opponent_name="Color",
                         event_name="Test", start_utc=utcnow(), url="u",
                         snapshot={}, snapshot_hash="h")
    telegram = FakeTelegram(fail_delete=True)
    messenger = LiveMessenger(storage, live_config(), telegram)
    asyncio.run(messenger.update(MATCH_ID, snapshot()))
    storage.bury_live_card(CHAT, MATCH_ID)

    asyncio.run(messenger.repost_buried(CHAT))
    assert telegram.sent == [telegram.sent[0]]          # nothing sent again
    assert storage.live_message(CHAT, MATCH_ID, 1)["telegram_message_id"] == 1001
    assert storage.buried_live_messages(CHAT) == []     # written off

    asyncio.run(messenger.repost_buried(CHAT))
    assert telegram.deleted == []
    storage.close()


def test_a_failed_resend_leaves_no_ghost_id(tmp_path):
    """The delete went through and the send did not: the row must not keep
    pointing at a message that no longer exists, or the next redraw would edit
    a ghost forever."""
    storage = Storage(tmp_path / "card.db")
    storage.upsert_match(match_id=MATCH_ID, opponent_id=1, opponent_name="Color",
                         event_name="Test", start_utc=utcnow(), url="u",
                         snapshot={}, snapshot_hash="h")

    class Broken(FakeTelegram):
        async def send_message(self, chat_id, text, reply_markup=None):
            if self.deleted:
                raise TelegramError("Telegram 500", retry_after=None)
            return await super().send_message(chat_id, text)

    telegram = Broken()
    messenger = LiveMessenger(storage, live_config(), telegram)
    asyncio.run(messenger.update(MATCH_ID, snapshot()))
    storage.bury_live_card(CHAT, MATCH_ID)
    asyncio.run(messenger.repost_buried(CHAT))

    assert storage.live_message(CHAT, MATCH_ID, 1)["telegram_message_id"] is None


# ---------- the half-time case: no frames at all ----------

def test_the_move_needs_no_frame(world):
    """Half time is exactly when the feed goes quiet.

    Nothing here touches the messenger with a snapshot: the whole move is
    driven by the queue. If it depended on the next frame, the card could sit
    above the half-time message for a minute.
    """
    messenger, notifier, telegram, storage = world
    notifier.enqueue(half_time())
    drain(notifier)
    assert telegram.deleted == [1001]


def test_the_feed_moves_it_too_if_the_queue_could_not(world):
    """The safety net: a restart, or a queue-side failure, leaves the card
    buried. The next redraw has to notice."""
    messenger, notifier, telegram, storage = world
    storage.bury_live_card(CHAT, MATCH_ID)
    asyncio.run(messenger.update(MATCH_ID, snapshot(score=(7, 6), rnd=14)))
    assert telegram.deleted == [1001]
    # Re-created with the FRESH score rather than deleted, re-sent and then
    # edited: three calls where two will do.
    assert [kind for kind, _ in telegram.calls] == ["delete", "send"]
    assert storage.buried_live_messages(CHAT) == []


def test_an_unchanged_score_does_not_swallow_the_move(world):
    """The "same text" shortcut and the throttle both exist to avoid pointless
    edits, and both would otherwise eat the move — the score at half time is
    the same one the card already shows."""
    messenger, notifier, telegram, storage = world
    storage.bury_live_card(CHAT, MATCH_ID)
    asyncio.run(messenger.update(MATCH_ID, snapshot()))   # identical snapshot
    assert telegram.deleted == [1001]


# ---------- dry run ----------

def test_dry_run_moves_nothing(tmp_path):
    storage = Storage(tmp_path / "card.db")
    storage.upsert_match(match_id=MATCH_ID, opponent_id=1, opponent_name="Color",
                         event_name="Test", start_utc=utcnow(), url="u",
                         snapshot={}, snapshot_hash="h")
    telegram = FakeTelegram()
    messenger = LiveMessenger(storage, live_config(dry_run=True), telegram)
    storage.bury_live_card(CHAT, MATCH_ID)
    asyncio.run(messenger.repost_buried(CHAT))
    assert telegram.calls == []
    storage.close()


# ---------- the two movers must not collide ----------

def test_the_queue_and_a_redraw_cannot_both_move_it(tmp_path):
    """Two cards for one map is the failure this guards against.

    `_drawing` serialises the FEED's redraws, but the queue moves cards from
    its own task. Without a lock both find the card buried, both delete it and
    both send a new one.
    """
    storage = Storage(tmp_path / "card.db")
    storage.upsert_match(match_id=MATCH_ID, opponent_id=1, opponent_name="Color",
                         event_name="Test", start_utc=utcnow(), url="u",
                         snapshot={}, snapshot_hash="h")

    class Slow(FakeTelegram):
        """A delete that takes long enough for the other mover to get in."""
        async def delete_message(self, chat_id, message_id):
            await asyncio.sleep(0.05)
            await super().delete_message(chat_id, message_id)

    telegram = Slow()
    messenger = LiveMessenger(storage, live_config(), telegram)

    async def scenario():
        await messenger.update(MATCH_ID, snapshot())
        storage.bury_live_card(CHAT, MATCH_ID)
        messenger._last_edit.clear()
        await asyncio.gather(
            messenger.repost_buried(CHAT),
            messenger.update(MATCH_ID, snapshot(score=(7, 6), rnd=14)))

    asyncio.run(scenario())
    # One delete, and one card left pointing at a message that exists.
    assert telegram.deleted == [1001]
    row = storage.live_message(CHAT, MATCH_ID, 1)
    assert row["telegram_message_id"] == 1002
    assert storage.buried_live_messages(CHAT) == []
    storage.close()
