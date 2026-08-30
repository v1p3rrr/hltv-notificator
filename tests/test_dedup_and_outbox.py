CHAT = "555"

"""Дедупликация — жёсткое требование ТЗ, а не пожелание.

Защита стоит на уникальном индексе sent_events.idempotency_key: вставка либо
проходит, либо нет. Отдельный запрос «есть ли уже такое» был бы гонкой.
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
    """Прогон одного и того же наблюдения дважды подряд не меняет число
    уведомлений — тот же сценарий, что реконнект живого фида на этапе 4."""
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
    """Рестарт сервиса не должен пересылать уже отправленное: журнал живёт
    в той же базе, что и состояние."""
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
                    payload={"reason": "проверка", "detail": "деталь"}))
    assert storage.pending_count() == 1
    asyncio.run(n._drain())
    assert storage.pending_count() == 0


def test_event_and_queue_row_are_written_atomically(storage, config):
    """Если журнал записан, а очередь нет — уведомление потеряно навсегда,
    потому что повторно оно уже не родится."""
    n = notifier(storage, config)
    n.enqueue(Event(type="E1", idempotency_key="E1:7:new", match_id=7,
                    payload={"opponent": "X", "event_name": "E",
                             "start_utc": "2026-09-01T15:00:00+00:00", "url": "u"}))
    keys = [row["idempotency_key"] for row in storage.due_outbox()]
    assert keys == ["E1:7:new"]
    assert storage.sent_event_count() == 1


# ---------------------------------------------------------------- остановка


class SlowTelegram:
    """Отправляет медленно — чтобы дедлайн последнего прохода был осязаем."""

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
    """На остановке очередь дописывает начатое.

    Событие могло родиться секунду назад — конец карты у матча, доигравшегося
    прямо во время рестарта. Без последнего прохода оно пролежало бы до
    следующего запуска, когда уведомление уже никому не нужно.
    """
    from dataclasses import replace

    storage.add_subscriber(CHAT)
    telegram = SlowTelegram()
    live = Notifier(storage, replace(config, dry_run=False, chat_id=CHAT), telegram)
    live.enqueue(pending_event(1))
    assert storage.pending_count() == 1

    stop = asyncio.Event()
    stop.set()                      # останов пришёл раньше, чем воркер проснулся
    asyncio.run(live.run(stop))

    assert telegram.sent == [CHAT]
    assert storage.pending_count() == 0


def test_final_pass_respects_its_deadline(storage, config):
    """Дедлайн важнее полноты: за нами SIGKILL, и лучше отправить сколько
    успели, чем быть убитыми посреди записи. Остаток уйдёт при следующем
    запуске — он никуда из очереди не делся."""
    from dataclasses import replace

    from hltv_notify.notify import outbox as outbox_module

    storage.add_subscriber(CHAT)
    telegram = SlowTelegram(delay=0.05)
    live = Notifier(storage, replace(config, dry_run=False, chat_id=CHAT), telegram)
    for number in range(1, 4):
        live.enqueue(pending_event(number))

    original = outbox_module.FINAL_DRAIN_SECONDS
    outbox_module.FINAL_DRAIN_SECONDS = 0.0      # времени не отпущено вовсе
    try:
        stop = asyncio.Event()
        stop.set()
        asyncio.run(live.run(stop))
    finally:
        outbox_module.FINAL_DRAIN_SECONDS = original

    assert telegram.sent == []
    assert storage.pending_count() == 3          # ничего не потеряно
