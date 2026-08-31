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
