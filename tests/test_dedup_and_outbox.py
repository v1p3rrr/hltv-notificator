CHAT = "555"

"""Deduplication is a hard requirement of the spec, not a nice-to-have.

The protection sits on the unique index sent_events.idempotency_key: an insert
either goes through or does not. A separate "is it already there" query would
be a race.
"""

import asyncio

from conftest import entry, later
from hltv_notify.models import Event
from hltv_notify.notify.outbox import Notifier
from hltv_notify.state.machine import ScheduleMachine

TEAM_ID = 12857


def notifier(storage, config) -> Notifier:
    return Notifier(storage, config, telegram=None)


def test_same_event_enqueued_twice_is_sent_once(storage, config):
    n = notifier(storage, config)
    event = Event(type="E1", idempotency_key="E1:42:new", match_id=42,
                  payload={"opponent": "Color", "event_name": "Test",
                           "start_utc": "2026-09-01T15:00:00+00:00",
                           "url": "https://www.hltv.org/matches/42/x"})
    assert n.enqueue(event) is True
    assert n.enqueue(event) is False
    assert storage.pending_count() == 1
    assert storage.sent_event_count() == 1


def test_replaying_whole_schedule_twice_changes_nothing(storage, config):
    """Replaying the same observation twice in a row does not change the
    notification count — the same scenario as a live-feed reconnect."""
    storage.add_team(CHAT, TEAM_ID, 'forze-reload', 'FORZE Reload')
    machine = ScheduleMachine(storage, config)
    n = notifier(storage, config)
    machine.apply([entry(1, start=later(600))], TEAM_ID)  # bootstrap

    schedule = [entry(1, start=later(600)), entry(2, start=later(900))]
    for _ in range(2):
        for event in machine.apply(schedule, TEAM_ID):
            n.enqueue(event)

    assert storage.sent_event_count() == 1
    assert storage.pending_count() == 1


def test_restart_does_not_resend(tmp_path, config):
    """A service restart must not resend what was already sent: the journal
    lives in the same database as the state."""
    from hltv_notify.state.db import Storage

    path = tmp_path / "restart.db"
    schedule = [entry(1, start=later(600)), entry(2, start=later(900))]

    first = Storage(path)
    first.add_team(CHAT, TEAM_ID, 'forze-reload', 'FORZE Reload')
    machine = ScheduleMachine(first, config)
    machine.apply([entry(1, start=later(600))], TEAM_ID)
    for event in machine.apply(schedule, TEAM_ID):
        Notifier(first, config, None).enqueue(event)
    sent_before = first.sent_event_count()
    first.close()

    second = Storage(path)
    machine2 = ScheduleMachine(second, config)
    for event in machine2.apply(schedule, TEAM_ID):
        Notifier(second, config, None).enqueue(event)
    assert second.sent_event_count() == sent_before
    second.close()


def test_dry_run_marks_sent_without_telegram(storage, config, caplog):
    n = notifier(storage, config)
    n.enqueue(Event(type="E8", idempotency_key="E8:test:1", match_id=None,
                    payload={"reason": "check", "detail": "detail"}))
    assert storage.pending_count() == 1
    asyncio.run(n._drain())
    assert storage.pending_count() == 0


def test_event_and_queue_row_are_written_atomically(storage, config):
    """If the journal is written and the queue is not, the notification is
    lost forever, because it will never be born a second time."""
    n = notifier(storage, config)
    n.enqueue(Event(type="E1", idempotency_key="E1:7:new", match_id=7,
                    payload={"opponent": "X", "event_name": "E",
                             "start_utc": "2026-09-01T15:00:00+00:00", "url": "u"}))
    keys = [row["idempotency_key"] for row in storage.due_outbox()]
    assert keys == ["E1:7:new"]
    assert storage.sent_event_count() == 1


# ---------------------------------------------------------------- shutdown


class SlowTelegram:
    """Sends slowly, so the final pass's deadline is tangible."""

    def __init__(self, delay=0.0):
        self.delay = delay
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.sent.append(chat_id)
        return len(self.sent)


def pending_event(number: int) -> Event:
    return Event(type="E1", idempotency_key=f"E1:{number}:new", match_id=number,
                 payload={"opponent": "X", "event_name": "E",
                          "start_utc": later(60).isoformat(), "url": "u"})


def test_stop_flushes_what_was_already_decided(storage, config):
    """On shutdown the queue finishes what it started.

    An event may have been born a second ago — the end of a map in a match
    that finished right during the restart. Without the final pass it would sit
    there until the next start, by which time nobody needs the notification.
    """
    from dataclasses import replace

    storage.add_subscriber(CHAT)
    telegram = SlowTelegram()
    live = Notifier(storage, replace(config, dry_run=False, chat_id=CHAT), telegram)
    live.enqueue(pending_event(1))
    assert storage.pending_count() == 1

    stop = asyncio.Event()
    stop.set()                      # the stop arrived before the worker woke up
    asyncio.run(live.run(stop))

    assert telegram.sent == [CHAT]
    assert storage.pending_count() == 0


def test_final_pass_respects_its_deadline(storage, config):
    """The deadline outranks completeness: SIGKILL is right behind us, and
    sending what we managed beats being killed mid-write. The remainder goes
    out on the next start — it has not left the queue."""
    from dataclasses import replace

    from hltv_notify.notify import outbox as outbox_module

    storage.add_subscriber(CHAT)
    telegram = SlowTelegram(delay=0.05)
    live = Notifier(storage, replace(config, dry_run=False, chat_id=CHAT), telegram)
    for number in range(1, 4):
        live.enqueue(pending_event(number))

    original = outbox_module.FINAL_DRAIN_SECONDS
    outbox_module.FINAL_DRAIN_SECONDS = 0.0      # no time granted at all
    try:
        stop = asyncio.Event()
        stop.set()
        asyncio.run(live.run(stop))
    finally:
        outbox_module.FINAL_DRAIN_SECONDS = original

    assert telegram.sent == []
    assert storage.pending_count() == 3          # nothing was lost


def test_a_fan_out_does_not_queue_people_behind_each_other(storage, config):
    """The per-chat pause is per chat.

    Telegram tolerates about one message a second into ONE chat and some
    thirty across different ones. Spacing every message from every other by
    1.2 s made twenty recipients of one map result wait twenty-four seconds
    for a score that is only interesting while the match is on.
    """
    import time
    from dataclasses import replace

    chats = [str(700 + n) for n in range(20)]
    for chat in chats:
        storage.add_subscriber(chat)
    telegram = SlowTelegram()
    live = Notifier(storage, replace(config, dry_run=False, chat_id=",".join(chats)),
                    telegram)
    live.enqueue(pending_event(1))
    assert storage.pending_count() == len(chats)

    started = time.monotonic()
    asyncio.run(live._drain())
    elapsed = time.monotonic() - started

    assert sorted(telegram.sent) == sorted(chats)
    assert storage.pending_count() == 0
    # One per chat, so nobody waits on anybody: only the global pacer applies,
    # which is 25 a second. Sequentially this was 1.2 s x 19 = 22.8 s.
    assert elapsed < 2.0


def test_two_messages_for_one_person_keep_their_order_and_spacing(storage, config):
    """Within a chat nothing changes: the live card continues the "match
    started" it follows, so the two must not overtake each other."""
    import time
    from dataclasses import replace

    from hltv_notify.notify import outbox as outbox_module

    storage.add_subscriber(CHAT)
    telegram = SlowTelegram()
    live = Notifier(storage, replace(config, dry_run=False, chat_id=CHAT), telegram)
    for number in (1, 2, 3):
        live.enqueue(pending_event(number))

    original = outbox_module.SEND_INTERVAL_SECONDS
    outbox_module.SEND_INTERVAL_SECONDS = 0.05
    try:
        started = time.monotonic()
        asyncio.run(live._drain())
        elapsed = time.monotonic() - started
    finally:
        outbox_module.SEND_INTERVAL_SECONDS = original

    assert telegram.sent == [CHAT, CHAT, CHAT]
    # Two gaps between three messages, and they were really waited out.
    assert elapsed >= 0.1


def test_one_failing_chat_does_not_leave_the_others_running(storage, config):
    """Without return_exceptions the first failure is raised while the other
    chats keep going detached, and the caller then reads the queue again and
    sends what those tasks had not yet marked — the same message twice."""
    from dataclasses import replace

    chats = ["801", "802", "803"]
    for chat in chats:
        storage.add_subscriber(chat)

    class OneBadChat(SlowTelegram):
        async def send_message(self, chat_id, text, reply_markup=None):
            if chat_id == "802":
                raise RuntimeError("not a TelegramError")
            return await super().send_message(chat_id, text, reply_markup)

    telegram = OneBadChat()
    live = Notifier(storage, replace(config, dry_run=False, chat_id=",".join(chats)),
                    telegram)
    live.enqueue(pending_event(1))

    asyncio.run(live._drain())          # must not raise

    # The two healthy chats got exactly one message each, and the failure did
    # not take the pass down with it.
    assert sorted(telegram.sent) == ["801", "803"]
    assert storage.pending_count() == 1     # only the failed one is left


# ---------------------------------------------------------------- update E4 picks


class MockTelegram(SlowTelegram):
    def __init__(self):
        super().__init__()
        self.edits = []

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append((chat_id, message_id, text))


def test_update_start_message_picks_edits_sent_message(storage, config):
    from dataclasses import replace
    from datetime import timezone
    from hltv_notify.sources.match_page import MapLine, MatchObservation

    match_id = 99
    storage.add_subscriber(CHAT)
    storage.add_team(CHAT, TEAM_ID, "forze-reload", "FORZE Reload")
    storage.upsert_match(
        match_id=match_id, opponent_id=13973, opponent_name="Color",
        event_name="Test Event", start_utc=later(60),
        url=f"https://www.hltv.org/matches/{match_id}/x",
        snapshot={}, snapshot_hash="h",
    )

    telegram = MockTelegram()
    live = Notifier(storage, replace(config, dry_run=False, chat_id=CHAT), telegram)

    # 1. Enqueue E4 without picks (all TBA)
    e4 = Event(
        type="E4", idempotency_key=f"E4:{match_id}:started", match_id=match_id,
        payload={
            "team_name": "FORZE Reload", "team_id": TEAM_ID,
            "opponent": "Color", "opponent_id": 13973,
            "event_name": "Test Event", "best_of": 3,
            "picks": [], "url": f"https://www.hltv.org/matches/{match_id}/x",
        },
    )
    assert live.enqueue(e4) is True

    # Drain to send it
    asyncio.run(live._drain())
    rows = storage.outbox_rows_for_match_event("E4", match_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "sent"
    assert "<code>" not in rows[0]["body"]
    msg_id = rows[0]["telegram_message_id"]
    assert msg_id is not None

    # 2. Map picks appear on the match page
    maps = [
        MapLine(number=1, name="Mirage", score_left=None, score_right=None, halves=None, picked_by="right"),
        MapLine(number=2, name="Nuke", score_left=None, score_right=None, halves=None, picked_by="left"),
        MapLine(number=3, name="Dust2", score_left=None, score_right=None, halves=None, picked_by=None),
    ]
    obs = MatchObservation(
        match_id=match_id, status="live",
        start_utc=later(60), event_name="Test Event", event_id=1,
        best_of=3, team1_id=13973, team1_name="Color",
        team2_id=TEAM_ID, team2_name="FORZE Reload",
        maps=maps, scorebot_id=match_id,
    )
    match_row = storage.get_match(match_id)

    updated = asyncio.run(live.update_start_message_picks(obs, match_row))
    assert updated is True

    # Verify Telegram edit_message_text was called
    assert len(telegram.edits) == 1
    edit_chat, edit_msg_id, edit_text = telegram.edits[0]
    assert edit_chat == CHAT
    assert edit_msg_id == msg_id
    assert "<code>" in edit_text
    assert "Mirage" in edit_text
    assert "Nuke" in edit_text
    assert "Dust2" in edit_text

    # Verify outbox row in DB is also updated
    updated_rows = storage.outbox_rows_for_match_event("E4", match_id)
    assert "<code>" in updated_rows[0]["body"]

    # 3. Calling again with the same observation is a no-op (idempotent)
    assert asyncio.run(live.update_start_message_picks(obs, match_row)) is False
    assert len(telegram.edits) == 1


def test_update_start_message_picks_updates_pending_message(storage, config):
    from dataclasses import replace
    from hltv_notify.sources.match_page import MapLine, MatchObservation

    match_id = 100
    storage.add_subscriber(CHAT)
    storage.add_team(CHAT, TEAM_ID, "forze-reload", "FORZE Reload")
    storage.upsert_match(
        match_id=match_id, opponent_id=13973, opponent_name="Color",
        event_name="Test Event", start_utc=later(60),
        url=f"https://www.hltv.org/matches/{match_id}/x",
        snapshot={}, snapshot_hash="h",
    )

    telegram = MockTelegram()
    live = Notifier(storage, replace(config, dry_run=False, chat_id=CHAT), telegram)

    e4 = Event(
        type="E4", idempotency_key=f"E4:{match_id}:started", match_id=match_id,
        payload={
            "team_name": "FORZE Reload", "team_id": TEAM_ID,
            "opponent": "Color", "opponent_id": 13973,
            "event_name": "Test Event", "best_of": 3,
            "picks": [], "url": f"https://www.hltv.org/matches/{match_id}/x",
        },
    )
    assert live.enqueue(e4) is True

    # Notice message is still pending (not drained yet)
    rows = storage.outbox_rows_for_match_event("E4", match_id)
    assert rows[0]["status"] == "pending"

    maps = [
        MapLine(number=1, name="Mirage", score_left=None, score_right=None, halves=None, picked_by="right"),
    ]
    obs = MatchObservation(
        match_id=match_id, status="live",
        start_utc=later(60), event_name="Test Event", event_id=1,
        best_of=1, team1_id=13973, team1_name="Color",
        team2_id=TEAM_ID, team2_name="FORZE Reload",
        maps=maps, scorebot_id=match_id,
    )
    match_row = storage.get_match(match_id)

    updated = asyncio.run(live.update_start_message_picks(obs, match_row))
    assert updated is True
    # Not edited via telegram since it's not sent yet
    assert len(telegram.edits) == 0

    # But outbox body is updated
    updated_rows = storage.outbox_rows_for_match_event("E4", match_id)
    assert "Mirage" in updated_rows[0]["body"]

    # When drained, it sends the updated body
    asyncio.run(live._drain())
    assert len(telegram.sent) == 1
    sent_rows = storage.outbox_rows_for_match_event("E4", match_id)
    assert sent_rows[0]["status"] == "sent"


def test_update_start_message_picks_noop_when_not_started(storage, config):
    from hltv_notify.sources.match_page import MapLine, MatchObservation

    match_id = 101
    live = Notifier(storage, config, None)
    maps = [
        MapLine(number=1, name="Mirage", score_left=None, score_right=None, halves=None, picked_by="right"),
    ]
    obs = MatchObservation(
        match_id=match_id, status="live",
        start_utc=later(60), event_name="Test Event", event_id=1,
        best_of=1, team1_id=13973, team1_name="Color",
        team2_id=TEAM_ID, team2_name="FORZE Reload",
        maps=maps, scorebot_id=match_id,
    )
    assert asyncio.run(live.update_start_message_picks(obs, None)) is False

