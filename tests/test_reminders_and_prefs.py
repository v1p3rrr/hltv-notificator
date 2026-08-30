"""Pre-match reminders, the pause and the personal timezone."""

from datetime import timedelta

import pytest

from hltv_notify.config import Config
from hltv_notify.models import Event
from hltv_notify.notify.outbox import Notifier
from hltv_notify.reminders import ReminderScheduler
from hltv_notify.state.db import Storage, utcnow

ILYA = "111"
FRIEND = "222"
TEAM = 12857
MATCH = 800


@pytest.fixture()
def config():
    return Config(chat_id=f"{ILYA},{FRIEND}", bot_token="t")


@pytest.fixture()
def store(tmp_path):
    storage = Storage(tmp_path / "prefs.db")
    storage.add_subscriber(ILYA)
    storage.add_subscriber(FRIEND)
    storage.add_team(ILYA, TEAM, "forze-reload", "FORZE Reload")
    storage.add_team(FRIEND, TEAM, "forze-reload", "FORZE Reload")
    yield storage
    storage.close()


def add_match(storage, *, minutes_ahead=60, match_id=MATCH):
    storage.upsert_match(
        match_id=match_id, team_id=TEAM, opponent_id=1, opponent_name="Color",
        event_name="GLuck", start_utc=utcnow() + timedelta(minutes=minutes_ahead),
        url="https://www.hltv.org/matches/800/x", snapshot={}, snapshot_hash="h")
    storage.link_match_team(match_id, TEAM)


# ---------------------------------------------------------------- reminders


def test_no_reminders_configured_means_silence(store, config):
    add_match(store, minutes_ahead=10)
    assert ReminderScheduler(store, config).due() == []


def test_reminder_fires_inside_its_window(store, config):
    store.add_reminder(ILYA, 15)
    add_match(store, minutes_ahead=10)
    events = ReminderScheduler(store, config).due()
    assert [e.type for e in events] == ["E10"]
    assert events[0].payload["only_chat"] == ILYA
    assert events[0].payload["minutes_before"] == 15


def test_reminder_does_not_fire_too_early(store, config):
    store.add_reminder(ILYA, 15)
    add_match(store, minutes_ahead=40)
    assert ReminderScheduler(store, config).due() == []


def test_reminder_does_not_fire_after_the_start(store, config):
    """The match is already running — too late to remind, that is what E4 is for."""
    store.add_reminder(ILYA, 15)
    add_match(store, minutes_ahead=-5)
    assert ReminderScheduler(store, config).due() == []


def test_several_offsets_are_separate_events(store, config):
    store.add_reminder(ILYA, 60)
    store.add_reminder(ILYA, 15)
    add_match(store, minutes_ahead=10)
    events = ReminderScheduler(store, config).due()
    assert sorted(e.payload["minutes_before"] for e in events) == [15, 60]
    assert len({e.idempotency_key for e in events}) == 2


def test_reminder_is_sent_once(store, config):
    store.add_reminder(ILYA, 15)
    add_match(store, minutes_ahead=10)
    scheduler = ReminderScheduler(store, config)
    notifier = Notifier(store, config, telegram=None)

    for _ in range(5):                       # the scheduler ticks often
        for event in scheduler.due():
            notifier.enqueue(event)
    assert store.pending_count() == 1


def test_reminder_is_addressed_not_broadcast(store, config):
    """Intervals differ between subscribers, so a reminder goes to one specific
    chat rather than to everyone following that team."""
    store.add_reminder(ILYA, 15)
    add_match(store, minutes_ahead=10)

    notifier = Notifier(store, config, telegram=None)
    for event in ReminderScheduler(store, config).due():
        notifier.enqueue(event)

    chats = {row["chat_id"] for row in store.due_outbox(limit=50)}
    assert chats == {ILYA}


def test_reminder_only_for_own_teams(store, config):
    """Someone else's team is not my match, even with reminders configured."""
    store.add_reminder(FRIEND, 15)
    store.set_team_enabled(FRIEND, TEAM, False)
    add_match(store, minutes_ahead=10)
    assert [e.payload["only_chat"] for e in ReminderScheduler(store, config).due()] == []


# ---------------------------------------------------------------- pause


def test_paused_subscriber_gets_nothing(store, config):
    add_match(store)
    store.set_subscriber_paused(FRIEND, True)

    event = Event(type="E4", idempotency_key="E4:800:started", match_id=MATCH,
                  payload={"team_id": TEAM, "opponent": "Color", "event_name": "GLuck",
                           "url": "u"})
    Notifier(store, config, telegram=None).enqueue(event)
    assert {row["chat_id"] for row in store.due_outbox(limit=50)} == {ILYA}


def test_pause_does_not_defer_notifications(store, config):
    """The pause means silence, not deferred delivery: nothing piles up."""
    add_match(store)
    store.set_subscriber_paused(ILYA, True)
    store.set_subscriber_paused(FRIEND, True)

    event = Event(type="E4", idempotency_key="E4:800:started", match_id=MATCH,
                  payload={"team_id": TEAM, "opponent": "Color", "event_name": "GLuck",
                           "url": "u"})
    assert Notifier(store, config, telegram=None).enqueue(event) is False

    store.set_subscriber_paused(ILYA, False)
    assert store.pending_count() == 0        # nothing accumulated


def test_pause_covers_service_alerts_too(store, config):
    store.set_subscriber_paused(FRIEND, True)
    event = Event(type="E8", idempotency_key="E8:schedule:down:x", match_id=None,
                  payload={"reason": "The schedule cannot be read", "detail": "timeout"})
    Notifier(store, config, telegram=None).enqueue(event)
    assert {row["chat_id"] for row in store.due_outbox(limit=50)} == {ILYA}


def test_pause_covers_the_live_score_message(store, config):
    """The live message is a notification too.

    It goes around the queue, along its own path, and that path once knew
    nothing about the pause: someone who asked for quiet kept receiving the
    score as the map went on.
    """
    import asyncio

    from hltv_notify.notify.live_message import LiveMessenger

    add_match(store)
    store.set_subscriber_paused(FRIEND, True)

    class FakeTelegram:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text, reply_markup=None):
            self.sent.append(chat_id)
            return len(self.sent)

        async def edit_message_text(self, *args, **kwargs):
            pass

    telegram = FakeTelegram()
    messenger = LiveMessenger(store, Config(chat_id=f"{ILYA},{FRIEND}",
                                            bot_token="t", dry_run=False), telegram)
    snapshot = {"map_number": 1, "map_name": "Mirage", "score_team": 5,
                "score_opponent": 3, "round": 9, "round_state": "started",
                "series_team": 0, "series_opponent": 0, "opponent": "Color",
                "team_name": "FORZE Reload", "team_id": TEAM, "opponent_id": 1,
                "event_name": "GLuck", "url": "u"}
    asyncio.run(messenger.update(MATCH, snapshot, force=True))
    assert telegram.sent == [ILYA]


# ---------------------------------------------------------------- timezone


def test_each_subscriber_sees_their_own_timezone(store, config):
    add_match(store)
    store.set_subscriber_timezone(ILYA, "Europe/Moscow")
    store.set_subscriber_timezone(FRIEND, "Asia/Tokyo")

    event = Event(type="E1", idempotency_key="E1:800:new", match_id=MATCH,
                  payload={"team_id": TEAM, "opponent": "Color", "event_name": "GLuck",
                           "start_utc": "2026-08-29T09:05:00+00:00", "url": "u"})
    Notifier(store, config, telegram=None).enqueue(event)

    bodies = {row["chat_id"]: row["body"] for row in store.due_outbox(limit=50)}
    assert "12:05" in bodies[ILYA]        # +3
    assert "18:05" in bodies[FRIEND]      # +9


def test_timezone_falls_back_to_config(store, config):
    assert store.subscriber_timezone(ILYA, config.timezone) == config.timezone


# ---------------------------------------------------------------- map picks


def test_picks_are_parsed_with_their_owner():
    from conftest import FIXTURES
    from hltv_notify.sources import match_page

    observation = match_page.parse(
        (FIXTURES / "match-2397053-live.html").read_text(encoding="utf-8"), 2397053)
    picks = observation.picks(12857)
    assert [(item["name"], item["pick"]) for item in picks] == [
        ("Mirage", "team"), ("Dust2", "opponent"), ("Ancient", "decider")]


def test_picks_flip_for_the_opponent_follower():
    """There is one veto on the page, but "our pick" differs per reader."""
    from conftest import FIXTURES
    from hltv_notify.sources import match_page

    observation = match_page.parse(
        (FIXTURES / "match-2397053-live.html").read_text(encoding="utf-8"), 2397053)
    assert [item["pick"] for item in observation.picks(13973)] == [
        "opponent", "team", "decider"]


def test_match_start_message_carries_a_copyable_block():
    from hltv_notify.notify import format as fmt

    event = Event(type="E4", idempotency_key="k", match_id=1, payload={
        "team_name": "FORZE Reload", "opponent": "Color", "event_name": "GLuck",
        "best_of": 3, "url": "u",
        "picks": [{"number": 1, "name": "Mirage", "pick": "team"},
                  {"number": 2, "name": "Dust2", "pick": "opponent"},
                  {"number": 3, "name": "Ancient", "pick": "decider"}]})
    text = fmt.render(event, team_name="FORZE Reload", tz_name="Europe/Moscow")
    assert "<pre>" in text and "</pre>" in text
    assert "Mirage" in text and "our pick" in text and "decider" in text


def _map_point_event():
    return Event(type="E11", idempotency_key="k", match_id=1, payload={
        "team_name": "FORZE Reload", "team_id": 12857,
        "opponent": "Color", "opponent_id": 13973,
        "event_name": "GLuck", "url": "u",
        "map_number": 1, "map_name": "Mirage",
        "score_team": 12, "score_opponent": 9, "round": 22, "overtime": 0})


def test_map_point_message_names_the_leader():
    from hltv_notify.notify import format as fmt

    text = fmt.render(_map_point_event(), team_name="FORZE Reload",
                      tz_name="Europe/Moscow")
    assert text.splitlines()[0] == "🏁 <b>Map point — FORZE Reload</b>"
    assert "Mirage — <b>12:9</b>" in text


def test_map_point_turns_around_for_the_other_side():
    """Whose map point it is follows from the score, and the score is flipped
    for a subscriber who follows the opponent. A stored "whose" would not have
    flipped with it."""
    from hltv_notify.notify import format as fmt

    text = fmt.render(_map_point_event(), team_name="Color",
                      tz_name="Europe/Moscow", for_team_id=13973)
    assert text.splitlines()[0] == "🚨 <b>Map point — FORZE Reload</b>"
    assert "Mirage — <b>9:12</b>" in text


def test_map_point_in_overtime_says_which_one():
    from hltv_notify.notify import format as fmt

    payload = {**_map_point_event().payload, "score_team": 18,
               "score_opponent": 17, "overtime": 2}
    event = Event(type="E11", idempotency_key="k", match_id=1, payload=payload)
    text = fmt.render(event, team_name="FORZE Reload", tz_name="Europe/Moscow")
    assert "(overtime 2)" in text
