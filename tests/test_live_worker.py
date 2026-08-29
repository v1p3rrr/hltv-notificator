"""Поведение Live Worker на молчании и обрывах фида."""

import asyncio

import pytest

from hltv_notify.live_worker import LiveWorker
from hltv_notify.notify.outbox import Notifier
from hltv_notify.sources.scorebot import FeedIdle, FeedUnavailable


class FakeClient:
    """Отдаёт заранее заданную последовательность: строки — пакеты,
    исключения — бросаются."""

    def __init__(self, script):
        self.script = list(script)
        self.polls = 0
        self.closed = False

    async def poll(self, timeout=45):
        self.polls += 1
        if not self.script:
            raise asyncio.CancelledError
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        self.closed = True


def worker(storage, config) -> LiveWorker:
    return LiveWorker(storage, config, Notifier(storage, config, None),
                      match_id=1, url="https://example.test/match")


def test_idle_does_not_tear_down_the_connection(storage, config):
    """Молчание фида — норма: в перерыве между картами так проходит вся пауза.
    Считать это обрывом значит переподключаться каждые 45 секунд."""
    w = worker(storage, config)
    client = FakeClient([FeedIdle("тишина"), FeedIdle("тишина"), []])
    stop = asyncio.Event()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(w._consume(client, stop))

    assert client.polls == 4          # три из скрипта плюс попытка после него
    assert client.closed is False     # соединение не закрывали


def test_real_failure_propagates_for_reconnect(storage, config):
    """Настоящий сбой обязан всплыть наружу — там backoff и переподключение."""
    w = worker(storage, config)
    client = FakeClient([FeedIdle("тишина"), FeedUnavailable("сеть отвалилась")])
    stop = asyncio.Event()

    with pytest.raises(FeedUnavailable):
        asyncio.run(w._consume(client, stop))


def test_idle_is_a_kind_of_unavailable():
    """FeedIdle наследует FeedUnavailable: если его где-то не поймали явно,
    поведение деградирует до обычного переподключения, а не до падения."""
    assert issubclass(FeedIdle, FeedUnavailable)


def test_stop_event_ends_consumption(storage, config):
    w = worker(storage, config)
    client = FakeClient([[], [], []])
    stop = asyncio.Event()
    stop.set()
    asyncio.run(w._consume(client, stop))
    assert client.polls == 0
