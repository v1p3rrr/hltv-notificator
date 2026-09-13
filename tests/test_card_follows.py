"""The live card absorbs the map's milestones.

The card is what a person watches during a map, and it is a fixed message in a
moving chat: a map point or a half-time message would push it out of view. So
the milestone goes INTO the card — the card deletes itself and comes back with
the milestone on top and the score as of now, one message instead of two.

The interesting part is the seam. Events go through the queue; the card goes
straight to Telegram. Those two paths are kept apart on purpose, and the
hand-over has to work anyway — including at half time, which is exactly when
the feed falls silent and no frame is coming to trigger a redraw.
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
        self.sent_ids = []
        self.edited = []
        self.deleted = []
        self.calls = []          # everything, in order
        self.fail_delete = fail_delete

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append(text)
        self.calls.append(("send", text))
        message_id = 1000 + len(self.sent)
        self.sent_ids.append(message_id)
        return message_id

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edited.append((message_id, text))
        self.calls.append(("edit", message_id))

    async def delete_message(self, chat_id, message_id):
        if self.fail_delete:
            raise TelegramError("Telegram 400: message can't be deleted", fatal=True)
        self.deleted.append(message_id)
        self.calls.append(("delete", message_id))


def snapshot(score=(6, 6), rnd=13, map_number=1, state="started"):
    return {
        "map_number": map_number, "map_name": "Mirage",
        "score_team": score[0], "score_opponent": score[1],
        "round": rnd, "round_state": state, "in_play": True,
        "series_team": 0, "series_opponent": 0,
        "opponent": "Color", "event_name": "Test", "url": "https://example.test/m",
    }


def live_config(**overrides) -> Config:
    # half_alerts on, or E12 never reaches anybody and there is nothing for
    # the card to absorb: it is off by default and per person (/settings half).
    base = dict(dry_run=False, bot_token="t", chat_id=CHAT, live_edit_seconds=0,
                half_alerts=True, overtime_alerts=True)
    base.update(overrides)
    return Config(**base)


def half_time(match_id=MATCH_ID, map_number=1) -> Event:
    return Event(type="E12", idempotency_key=f"E12:{match_id}:map:{map_number}:half",
                 match_id=match_id,
                 payload={"map_name": "Mirage", "map_number": map_number, "overtime": 0,
                          "score_team": 6, "score_opponent": 6,
                          "opponent": "Color", "team_id": TEAM_ID,
                          "url": "https://example.test/m"})


def map_point(score=(12, 6), streams=()) -> Event:
    return Event(
        type="E11", idempotency_key=f"E11:{MATCH_ID}:map:1:point:us:13", match_id=MATCH_ID,
        payload={"map_name": "Mirage", "map_number": 1, "opponent": "Color",
                 "score_team": score[0], "score_opponent": score[1], "team_id": TEAM_ID,
                 "decides_match": False, "url": "https://example.test/m",
                 "streams": list(streams)})


def multikill() -> Event:
    return Event(type="E9", idempotency_key="E9:42:r13:4", match_id=MATCH_ID,
                 payload={"kills": 4, "player": "sh1ro", "team_id": TEAM_ID,
                          "team_name": "FORZE Reload", "opponent": "Color",
                          "map_name": "Mirage", "round": 13,
                          "score_team": 6, "score_opponent": 6,
                          "url": "https://example.test/m"})


def fresh_storage(tmp_path) -> Storage:
    storage = Storage(tmp_path / "card.db")
    for match_id in (MATCH_ID, OTHER_MATCH):
        storage.upsert_match(match_id=match_id, opponent_id=1, opponent_name="Color",
                             event_name="Test", start_utc=utcnow(), url="u",
                             snapshot={}, snapshot_hash="h")
    return storage


@pytest.fixture()
def world(tmp_path):
    """A chat with a card already on screen, and a queue wired to it."""
    storage = fresh_storage(tmp_path)
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

def test_half_time_becomes_the_card(world):
    messenger, notifier, telegram, storage = world
    notifier.enqueue(half_time())
    drain(notifier)

    # The old card is deleted and ONE message goes out — the milestone on
    # top, the card's body underneath. No half-time message of its own.
    assert [kind for kind, _ in telegram.calls] == ["delete", "send"]
    assert telegram.deleted == [1001]
    text = telegram.sent[-1]
    assert text.startswith("🔄 <b>Half time</b>\n6:6 · sides swap\n\n")
    assert "Map 1: Mirage" in text
    row = storage.live_message(CHAT, MATCH_ID, 1)
    assert row["telegram_message_id"] == 1002
    assert row["banner"] == "🔄 <b>Half time</b>\n6:6 · sides swap"
    # The queue recorded the card's message as the milestone's delivery.
    assert storage.pending_count() == 0


def test_the_card_carries_the_score_as_of_now_not_as_of_the_last_edit(world):
    """The defect from the chat: "Map point 8:12" and, right under it, the card
    saying 8:11. The frame that produced the milestone had been submitted, but
    the edit throttle dropped its redraw — correctly — and the stored text was
    therefore a round behind at exactly the moment it was re-sent."""
    messenger, notifier, telegram, storage = world
    # A newer frame arrives; the throttle holds, so the row keeps 6:6.
    messenger._last_edit[(CHAT, MATCH_ID, 1)] = 10 ** 12   # a very recent edit
    asyncio.run(messenger.update(MATCH_ID, snapshot(score=(12, 6), rnd=19, state="ended")))
    assert "6:6" in storage.live_message(CHAT, MATCH_ID, 1)["last_text"]

    notifier.enqueue(map_point())
    drain(notifier)
    assert "<b>12:6</b>" in telegram.sent[-1]
    assert "round 19 · round over" in telegram.sent[-1]


def test_submit_counts_even_when_the_draw_is_throttled(world):
    """`submit` is the ordinary path from the feed; the score it hands over
    must reach the queue whether or not the draw happens."""
    messenger, notifier, telegram, storage = world
    messenger._last_edit[(CHAT, MATCH_ID, 1)] = 10 ** 12

    async def scenario():
        messenger.submit(MATCH_ID, snapshot(score=(12, 6), rnd=19))
        await messenger._settle(MATCH_ID)

    asyncio.run(scenario())
    notifier.enqueue(map_point())
    drain(notifier)
    assert "<b>12:6</b>" in telegram.sent[-1]


def test_the_next_redraw_edits_the_new_message_and_keeps_the_banner(world):
    messenger, notifier, telegram, storage = world
    notifier.enqueue(half_time())
    drain(notifier)
    messenger._last_edit.clear()
    asyncio.run(messenger.update(MATCH_ID, snapshot(score=(7, 6), rnd=14)))
    message_id, text = telegram.edited[-1]
    assert message_id == 1002
    assert text.startswith("🔄 <b>Half time</b>\n6:6 · sides swap\n\n")
    assert "<b>7:6</b>" in text


def test_a_later_milestone_replaces_the_banner(world):
    messenger, notifier, telegram, storage = world
    notifier.enqueue(half_time())
    drain(notifier)
    notifier.enqueue(map_point())
    drain(notifier)
    text = telegram.sent[-1]
    assert text.startswith("🏁 <b>Map point — FORZE Reload</b>")
    assert "Half time" not in text
    assert storage.live_message(CHAT, MATCH_ID, 1)["banner"].startswith("🏁")


def test_a_map_point_brings_the_streams_with_it(world):
    messenger, notifier, telegram, storage = world
    notifier.enqueue(map_point(streams=[
        {"name": "caster", "url": "https://www.twitch.tv/caster",
         "flag": "GB", "provider": "twitch", "viewers": 100}]))
    drain(notifier)
    text = telegram.sent[-1]
    assert "<blockquote>" in text
    assert "twitch.tv/caster" in text
    # The block sits in the banner, above the body.
    assert text.index("<blockquote>") < text.index("Map 1: Mirage")


def test_the_hand_over_needs_no_frame(world):
    """Half time is exactly when the feed goes quiet. Nothing here touches the
    messenger with a snapshot: the whole thing is driven by the queue."""
    messenger, notifier, telegram, storage = world
    notifier.enqueue(half_time())
    drain(notifier)
    assert telegram.deleted == [1001]


# ---------- what goes as a plain message instead ----------

def test_a_multikill_leaves_the_card_where_it_is(world):
    """There are several a map. A card that deletes and re-posts itself after
    each would jump around the chat and spend the rate budget doing it."""
    messenger, notifier, telegram, storage = world
    notifier.enqueue(multikill())
    drain(notifier)
    assert telegram.deleted == []
    assert storage.live_message(CHAT, MATCH_ID, 1)["telegram_message_id"] == 1001
    assert storage.live_message(CHAT, MATCH_ID, 1)["banner"] is None


def test_a_milestone_of_another_match_leaves_it_alone(world):
    messenger, notifier, telegram, storage = world
    notifier.enqueue(half_time(match_id=OTHER_MATCH))
    drain(notifier)
    assert telegram.deleted == []
    assert telegram.sent[-1].startswith("🔄 <b>Half time</b>\nMirage — ")


def test_a_finalized_card_is_never_rebuilt(world):
    """The map is over; its final score is meant to stay where it was written.
    The milestone still arrives, as its own message."""
    messenger, notifier, telegram, storage = world
    asyncio.run(messenger.finalize(MATCH_ID, snapshot(score=(13, 6), rnd=19)))
    telegram.calls.clear()
    notifier.enqueue(half_time())
    drain(notifier)
    assert telegram.deleted == []
    assert [kind for kind, _ in telegram.calls] == ["send"]
    assert "sides swap" in telegram.sent[-1]
    assert storage.live_message(CHAT, MATCH_ID, 1)["finalized"] == 1


def test_a_milestone_of_a_map_the_snapshot_is_not_about_goes_plain(world):
    """The snapshot in memory is map 1's; a milestone of map 2 delivered late
    must not be drawn on top of it."""
    messenger, notifier, telegram, storage = world
    notifier.enqueue(half_time(map_number=2))
    drain(notifier)
    assert telegram.deleted == []
    assert telegram.sent[-1].startswith("🔄 <b>Half time</b>\nMirage — ")


def test_no_snapshot_in_memory_goes_plain(tmp_path):
    """After a restart the row is there and the feed has not spoken yet. The
    stored text is NOT used — it is exactly what is stale — so the milestone
    goes as its own message and the card is left alone."""
    storage = fresh_storage(tmp_path)
    telegram = FakeTelegram()
    config = live_config()
    messenger = LiveMessenger(storage, config, telegram)
    notifier = Notifier(storage, config, telegram, live_messenger=messenger)
    storage.save_live_message(CHAT, MATCH_ID, 1, telegram_message_id=1001,
                              text="Mirage 6:6", finalized=False)
    notifier.enqueue(half_time())
    drain(notifier)
    assert telegram.deleted == []
    assert len(telegram.sent) == 1 and "sides swap" in telegram.sent[0]
    assert storage.live_message(CHAT, MATCH_ID, 1)["telegram_message_id"] == 1001
    storage.close()


def test_a_card_switched_off_gets_the_plain_message(tmp_path):
    """Rebuilding a card means SENDING one, so it answers to the same rules.
    `/settings card off` stops the card, and the milestone arrives on its own."""
    storage = fresh_storage(tmp_path)
    storage.add_subscriber(CHAT)
    storage.set_setting(CHAT, "card", 0)
    telegram = FakeTelegram()
    config = live_config()
    messenger = LiveMessenger(storage, config, telegram)
    notifier = Notifier(storage, config, telegram, live_messenger=messenger)
    storage.save_live_message(CHAT, MATCH_ID, 1, telegram_message_id=1001,
                              text="the card they switched off", finalized=False)
    asyncio.run(messenger.update(MATCH_ID, snapshot()))   # remembered, not drawn
    notifier.enqueue(half_time())
    drain(notifier)
    assert telegram.deleted == []
    assert len(telegram.sent) == 1 and "sides swap" in telegram.sent[0]
    storage.close()


def test_a_row_queued_before_the_upgrade_goes_plain(world):
    """NULL banner is the type test: an old row is a plain message."""
    messenger, notifier, telegram, storage = world
    storage.record_event(idempotency_key="E12:42:map:1:half", event_type="E12",
                         match_id=MATCH_ID, body="old half time", chat_id=CHAT)
    drain(notifier)
    assert telegram.deleted == []
    assert telegram.sent[-1] == "old half time"
    assert storage.live_message(CHAT, MATCH_ID, 1)["telegram_message_id"] == 1001


def test_dry_run_rebuilds_nothing(tmp_path):
    storage = fresh_storage(tmp_path)
    telegram = FakeTelegram()
    config = live_config(dry_run=True)
    messenger = LiveMessenger(storage, config, telegram)
    notifier = Notifier(storage, config, telegram, live_messenger=messenger)
    asyncio.run(messenger.update(MATCH_ID, snapshot()))
    notifier.enqueue(half_time())
    drain(notifier)
    assert telegram.calls == []
    assert storage.pending_count() == 0
    storage.close()


# ---------- failure ----------

def test_a_refused_delete_leaves_the_card_and_sends_the_milestone_beside_it(tmp_path):
    """Telegram refuses deletes it considers impossible. The card then stays
    where it is and keeps being edited — better than two copies — and the
    milestone lands below it as its own message."""
    storage = fresh_storage(tmp_path)
    telegram = FakeTelegram(fail_delete=True)
    config = live_config()
    messenger = LiveMessenger(storage, config, telegram)
    notifier = Notifier(storage, config, telegram, live_messenger=messenger)
    asyncio.run(messenger.update(MATCH_ID, snapshot()))
    notifier.enqueue(half_time())
    drain(notifier)

    assert len(telegram.sent) == 2 and "sides swap" in telegram.sent[1]
    row = storage.live_message(CHAT, MATCH_ID, 1)
    assert row["telegram_message_id"] == 1001
    assert row["banner"] is None
    messenger._last_edit.clear()
    asyncio.run(messenger.update(MATCH_ID, snapshot(score=(7, 6), rnd=14)))
    assert telegram.edited[-1][0] == 1001
    storage.close()


def test_a_failed_send_after_the_delete_leaves_no_ghost_id(tmp_path):
    """The delete went through and the send did not: the row must not keep
    pointing at a message that no longer exists, or the next redraw would edit
    a ghost forever. The milestone is then retried by the queue as usual."""
    storage = fresh_storage(tmp_path)

    class Broken(FakeTelegram):
        """The one send right after the delete fails; everything else works."""
        failed = False

        async def send_message(self, chat_id, text, reply_markup=None):
            if self.deleted and not self.failed:
                self.failed = True
                raise TelegramError("Telegram 500", retry_after=None)
            return await super().send_message(chat_id, text)

    telegram = Broken()
    config = live_config()
    messenger = LiveMessenger(storage, config, telegram)
    notifier = Notifier(storage, config, telegram, live_messenger=messenger)
    async def scenario():
        await messenger.update(MATCH_ID, snapshot())
        notifier.enqueue(half_time())
        await notifier._drain()
        row = storage.live_message(CHAT, MATCH_ID, 1)
        assert row["banner"] is None
        assert storage.pending_count() == 0     # went plain, right after
        assert "sides swap" in telegram.sent[-1]
        # The frame is handed back, and the redraw is not held back by the
        # throttle: there is no card left to edit, and a chat with the
        # milestone but no score under it is worse than one extra call.
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    row = storage.live_message(CHAT, MATCH_ID, 1)
    assert row["telegram_message_id"] == telegram.sent_ids[-1]
    assert row["banner"] is None
    assert "<b>6:6</b>" in telegram.sent[-1]     # a fresh card, below the milestone
    storage.close()


def test_the_banner_faces_the_same_side_as_the_card(tmp_path):
    """A chat following BOTH teams of a match, with the map point muted for the
    first one (Color, id 1 — the card is drawn from the first team's side).
    The second team lets the milestone through, and the banner used to be
    oriented on IT: "🏁 map point for us, 12:6" over a body reading
    "Color 6:12 FORZE Reload"."""
    storage = fresh_storage(tmp_path)
    storage.add_subscriber(CHAT)
    storage.add_team(CHAT, TEAM_ID, "forze", "FORZE Reload")
    storage.add_team(CHAT, 1, "color", "Color")
    storage.link_match_team(MATCH_ID, TEAM_ID)
    storage.link_match_team(MATCH_ID, 1)
    storage.set_team_mutes(CHAT, 1, ["E11"])
    telegram = FakeTelegram()
    config = live_config()
    messenger = LiveMessenger(storage, config, telegram)
    notifier = Notifier(storage, config, telegram, live_messenger=messenger)
    # The machine's snapshot is oriented on the canonical team and carries
    # both ids, which is what lets the card turn around for Color's follower.
    live = {**snapshot(score=(12, 6), rnd=19), "team_name": "FORZE Reload",
            "team_id": TEAM_ID, "opponent_id": 1}
    asyncio.run(messenger.update(MATCH_ID, live))
    assert "Color <b>6:12</b> FORZE Reload" in telegram.sent[-1]

    event = map_point()
    event.payload.update(team_name="FORZE Reload", opponent_id=1)
    notifier.enqueue(event)
    drain(notifier)
    text = telegram.sent[-1]
    assert text.startswith("🚨 <b>Map point — FORZE Reload</b>\n6:12")
    assert "Color <b>6:12</b> FORZE Reload" in text
    storage.close()


# ---------- the two writers must not collide ----------

def test_the_queue_and_a_redraw_cannot_both_write_the_card(tmp_path):
    """Two cards for one map is the failure this guards against.

    `_drawing` serialises the FEED's redraws, but the queue rebuilds cards
    from its own task, and between its delete and its send the row carries no
    id. A redraw reading it then would open a card of its own.
    """
    storage = fresh_storage(tmp_path)

    class SlowSend(FakeTelegram):
        async def send_message(self, chat_id, text, reply_markup=None):
            if self.deleted:                    # only the rebuild is slow
                await asyncio.sleep(0.05)
            return await FakeTelegram.send_message(self, chat_id, text)

    telegram = SlowSend()
    config = live_config()
    messenger = LiveMessenger(storage, config, telegram)
    notifier = Notifier(storage, config, telegram, live_messenger=messenger)

    async def scenario():
        await messenger.update(MATCH_ID, snapshot(score=(12, 10), rnd=23))
        notifier.enqueue(map_point(score=(12, 10)))

        async def an_ordinary_redraw():
            await asyncio.sleep(0.02)           # lands mid-rebuild
            messenger._last_edit.clear()
            await messenger.update(MATCH_ID, snapshot(score=(12, 11), rnd=24))

        await asyncio.gather(notifier._drain(), an_ordinary_redraw())

    asyncio.run(scenario())
    # One card at the start, one delete, one rebuild: nothing orphaned.
    assert len(telegram.sent) - len(telegram.deleted) == 1
    row = storage.live_message(CHAT, MATCH_ID, 1)
    assert row["telegram_message_id"] == telegram.sent_ids[-1]
    # And the redraw that waited kept the banner the rebuild had just written.
    assert row["banner"].startswith("🏁")
    assert telegram.edited[-1][1].startswith("🏁")
    storage.close()


def test_a_map_that_ends_mid_hand_over_keeps_its_freeze(tmp_path):
    """The map can end between the queue picking the row up and acting on it.
    The rebuild used to be able to delete the frozen final card, send it again
    and clear `finalized` on the way — the defect `_settle` was written for."""
    storage = fresh_storage(tmp_path)
    telegram = FakeTelegram()
    config = live_config()
    messenger = LiveMessenger(storage, config, telegram)
    notifier = Notifier(storage, config, telegram, live_messenger=messenger)
    asyncio.run(messenger.update(MATCH_ID, snapshot(score=(13, 10), rnd=23)))
    notifier.enqueue(half_time())

    async def scenario():
        async def the_map_ends():
            storage.save_live_message(CHAT, MATCH_ID, 1, telegram_message_id=None,
                                      text="Mirage 13:10", finalized=True)
        # `absorb` waits on the draw in flight; the map ends in there.
        messenger._drawing[MATCH_ID] = asyncio.create_task(the_map_ends())
        await notifier._drain()

    asyncio.run(scenario())
    assert storage.live_message(CHAT, MATCH_ID, 1)["finalized"] == 1
    assert telegram.deleted == []
    assert "sides swap" in telegram.sent[-1]     # went plain
    storage.close()


# ---------- the banner's wording ----------

def stream(flag="GB", name="caster"):
    return {"name": name, "url": f"https://www.twitch.tv/{name}", "flag": flag,
            "provider": "twitch", "viewers": 100}


def render_banner(event, streams_on=True):
    from hltv_notify import streams as st
    from hltv_notify.notify import format as fmt
    prefs = st.StreamPreference(limit=3, languages=("en",), aliases={}) if streams_on else None
    return fmt.render_banner(event, team_name="FORZE Reload", for_team_id=TEAM_ID,
                             stream_prefs=prefs)


def test_the_banner_says_the_milestone_and_the_score_and_nothing_the_body_says():
    """The score is on purpose: the banner stays for the rest of the map, and
    "12:6 · one round from the map" still reads as history under a body that
    has moved on. The map, the teams, the series and the link are the body's."""
    text = render_banner(map_point())
    assert text == "🏁 <b>Map point — FORZE Reload</b>\n12:6 · one round from the map"
    assert "Mirage" not in text and "href" not in text
    assert render_banner(half_time()) == "🔄 <b>Half time</b>\n6:6 · sides swap"


def test_the_banner_turns_around_for_the_other_side():
    """Whose map point it is follows from the oriented score, so a follower of
    the opponent reads the same event as a map point AGAINST them."""
    from hltv_notify.notify import format as fmt
    event = map_point()
    event.payload.update(team_name="FORZE Reload", opponent_id=7)
    ours = fmt.render_banner(event, team_name="x", for_team_id=TEAM_ID)
    assert ours.startswith("🏁 <b>Map point — FORZE Reload</b>\n12:6")
    theirs = fmt.render_banner(event, team_name="x", for_team_id=7)
    assert theirs.startswith("🚨 <b>Map point — FORZE Reload</b>\n6:12")
    assert render_banner(map_point(score=(6, 12))).startswith("🚨 <b>Map point — Color</b>")


def test_a_map_point_that_ends_the_match_says_so():
    event = map_point()
    event.payload["decides_match"] = True
    assert render_banner(event).endswith("12:6 · one round from the map and the match")


def test_an_overtime_banner():
    event = Event(type="E13", idempotency_key="E13:42:map:1:overtime:2", match_id=MATCH_ID,
                  payload={"map_name": "Mirage", "map_number": 1, "overtime": 2,
                           "score_team": 15, "score_opponent": 15, "opponent": "Color",
                           "team_id": TEAM_ID, "url": "u", "streams": [stream()]})
    text = render_banner(event)
    assert text.startswith("🕗 <b>Overtime 2 begins</b>\n15:15\n<blockquote>")


def test_streams_ride_on_a_map_point_and_an_overtime_but_not_on_the_half():
    from hltv_notify.notify import format as fmt
    with_streams = map_point(streams=[stream()])
    assert "<blockquote>" in render_banner(with_streams)
    assert "<blockquote>" not in render_banner(with_streams, streams_on=False)
    # The standalone message — for a reader with no card — carries it too,
    # above the link, exactly like a highlight.
    from hltv_notify import streams as st
    prefs = st.StreamPreference(limit=3, languages=("en",), aliases={})
    plain = fmt.render(with_streams, team_name="FORZE Reload", tz_name="UTC",
                       for_team_id=TEAM_ID, stream_prefs=prefs)
    assert plain.index("<blockquote>") < plain.index("Watch the match")
    # A half never carries one — decided in ONE place, the machine, which
    # puts none in its payload (the test below); the renderer draws what it
    # is given.
    assert "<blockquote>" not in fmt.render(half_time(), team_name="x", tz_name="UTC",
                                            for_team_id=TEAM_ID, stream_prefs=prefs)


def test_the_banner_is_not_for_other_types():
    assert render_banner(multikill()) == ""


def test_the_absorbed_card_survives_telegram_html():
    """The banner and the body are sent as one text with parse_mode=HTML; an
    unsupported tag anywhere in it and the whole card is refused."""
    from test_bot import unsupported_tags
    from hltv_notify.notify import format as fmt
    text = fmt.render_live(snapshot(), team_name="FORZE Reload", announces_start=True,
                           banner=render_banner(map_point(streams=[stream(name="<x>")])))
    assert unsupported_tags(text) == []
    assert text.count("\n\n") == 1


def test_the_machine_puts_streams_on_a_map_point_and_an_overtime_only(tmp_path, config):
    from dataclasses import replace
    from hltv_notify.state.live_machine import LiveMachine
    from hltv_notify.sources.scorebot import LiveFrame
    from hltv_notify.state.db import utcnow

    storage = fresh_storage(tmp_path)
    storage.set_map_lineup(MATCH_ID, ["Mirage"])
    storage.set_match_streams(MATCH_ID, [stream()])
    machine = LiveMachine(storage, replace(config, half_alerts=True, overtime_alerts=True))

    def at(ours, theirs, rnd):
        return LiveFrame(map_name="de_mirage", current_round=rnd, round_state="started",
                         live=True, ct_team_id=TEAM_ID, ct_team_name="us", ct_score=ours,
                         t_team_id=1, t_team_name="them", t_score=theirs,
                         regulation=12, overtime=3)

    machine.apply(MATCH_ID, at(0, 0, 1))
    by_type = {}
    for ours, theirs, rnd in [(6, 6, 13), (12, 11, 24), (12, 12, 25)]:
        for event in machine.apply(MATCH_ID, at(ours, theirs, rnd)):
            by_type[event.type] = event
    assert set(by_type) == {"E12", "E11", "E13"}
    assert by_type["E12"].payload["streams"] == []
    assert by_type["E11"].payload["streams"] == [stream()]
    assert by_type["E13"].payload["streams"] == [stream()]
    storage.close()


def test_a_refused_rebuild_hands_the_frame_back(tmp_path, monkeypatch):
    """`absorb` settles the draw in flight, which drops the frame it was about
    to draw. If the rebuild then does not happen, that frame must not be lost
    with it — at half time it is the last one for a minute."""
    from hltv_notify.notify import live_message
    monkeypatch.setattr(live_message, "HARD_MIN_EDIT_SECONDS", 0.05)
    storage = fresh_storage(tmp_path)
    telegram = FakeTelegram(fail_delete=True)
    config = live_config()
    messenger = LiveMessenger(storage, config, telegram)
    notifier = Notifier(storage, config, telegram, live_messenger=messenger)

    async def scenario():
        await messenger.update(MATCH_ID, snapshot(score=(6, 5), rnd=12))
        messenger.submit(MATCH_ID, snapshot(score=(6, 6), rnd=12, state="ended"))
        notifier.enqueue(half_time())
        await notifier._drain()                       # delete refused → plain
        assert "sides swap" in telegram.sent[-1]
        await asyncio.sleep(0.2)                      # the held frame is drawn

    asyncio.run(scenario())
    assert telegram.edited and "<b>6:6</b>" in telegram.edited[-1][1]
    storage.close()
