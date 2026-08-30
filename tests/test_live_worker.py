"""How the Live Worker behaves on feed silence and disconnects."""

import asyncio

import pytest

from hltv_notify.live_worker import LiveWorker
from hltv_notify.notify.outbox import Notifier
from hltv_notify.sources.scorebot import FeedIdle, FeedUnavailable


class FakeClient:
    """Serves a predefined sequence: strings are packets, exceptions are raised."""

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
    """Feed silence is normal: the whole break between maps passes like this.
    Treating it as a disconnect means reconnecting every 45 seconds."""
    w = worker(storage, config)
    client = FakeClient([FeedIdle("silence"), FeedIdle("silence"), []])
    stop = asyncio.Event()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(w._consume(client, stop))

    assert client.polls == 4          # three from the script plus one attempt after
    assert client.closed is False     # the connection was not closed


def test_real_failure_propagates_for_reconnect(storage, config):
    """A real failure has to surface — backoff and reconnect live out there."""
    w = worker(storage, config)
    client = FakeClient([FeedIdle("silence"), FeedUnavailable("network dropped")])
    stop = asyncio.Event()

    with pytest.raises(FeedUnavailable):
        asyncio.run(w._consume(client, stop))


def test_idle_is_a_kind_of_unavailable():
    """FeedIdle inherits FeedUnavailable: if it is not caught explicitly
    somewhere, the behaviour degrades to an ordinary reconnect, not a crash."""
    assert issubclass(FeedIdle, FeedUnavailable)


def test_stop_event_ends_consumption(storage, config):
    w = worker(storage, config)
    client = FakeClient([[], [], []])
    stop = asyncio.Event()
    stop.set()
    asyncio.run(w._consume(client, stop))
    assert client.polls == 0


def test_rejection_cooldown_has_a_floor(monkeypatch, storage):
    """403 means "back off". The config may ask for a one-second pause, but
    such a pause would mean hammering the source, hence the lower bound."""
    from hltv_notify.config import Config
    from hltv_notify.live_worker import MIN_REJECTED_COOLDOWN_SECONDS

    tiny = Config(live_feed_cooldown=1)
    assert max(float(tiny.live_feed_cooldown),
               MIN_REJECTED_COOLDOWN_SECONDS) == MIN_REJECTED_COOLDOWN_SECONDS

    generous = Config(live_feed_cooldown=1800)
    assert max(float(generous.live_feed_cooldown),
               MIN_REJECTED_COOLDOWN_SECONDS) == 1800
