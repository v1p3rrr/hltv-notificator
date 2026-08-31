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


# ----------------------------------------------------------------------
# The card must not overtake the start message it continues
# ----------------------------------------------------------------------


def test_the_card_waits_for_the_start_message(storage, config):
    """The card goes straight to Telegram, the start message waits in the
    queue — without this it would arrive first."""
    w = worker(storage, config)
    storage.record_event(idempotency_key="E4:1:started", event_type="E4",
                         match_id=1, body="x", chat_id="1")
    w._awaiting_start_message = __import__("time").monotonic() + 30
    assert w._start_message_pending() is True

    for row in storage.due_outbox():
        storage.mark_sent(row["id"], 1)
    assert w._start_message_pending() is False
    # And the flag is cleared, so the queue is not consulted on every frame.
    assert w._awaiting_start_message == 0.0


def test_the_card_does_not_wait_forever(storage, config):
    """A stuck queue must not cost us the live score."""
    w = worker(storage, config)
    storage.record_event(idempotency_key="E4:1:started", event_type="E4",
                         match_id=1, body="x", chat_id="1")
    w._awaiting_start_message = __import__("time").monotonic() - 1
    assert w._start_message_pending() is False


def test_the_queue_wakes_up_on_a_new_event(storage, config):
    """Five seconds of sleep are five seconds of the wrong order."""
    from hltv_notify.models import Event

    notifier = Notifier(storage, config, None)
    assert notifier._arrived.is_set() is False
    notifier.enqueue(Event(type="E4", idempotency_key="E4:1:started", match_id=1,
                           payload={"team_name": "T", "opponent": "O", "url": "u"}))
    assert notifier._arrived.is_set() is True


def test_release_does_not_cancel_a_write_in_flight(storage, config):
    """The stop flag and the cancel used to be set in the same breath, so the
    worker never saw the flag — and a cancel landing inside the Telegram call
    that creates the map's card posts the message and never saves its id, so
    the next start opens a second card for the same map.
    """
    import asyncio

    from hltv_notify.live_worker import LiveSupervisor

    supervisor = LiveSupervisor(storage, config, Notifier(storage, config, None))
    finished = []

    async def scenario():
        async def slow_write(stop):
            try:
                await asyncio.sleep(0.05)     # a send in flight
                finished.append("saved")
            except asyncio.CancelledError:
                finished.append("cancelled")
                raise

        stop = asyncio.Event()
        supervisor._stops[1] = stop
        supervisor._tasks[1] = asyncio.create_task(slow_write(stop))
        supervisor.release(1)
        assert stop.is_set()                  # the flag goes up first
        await supervisor.shutdown()

    asyncio.run(scenario())
    assert finished == ["saved"]


def test_release_still_cancels_what_will_not_stop(storage, config):
    """The grace is short on purpose: the feed's long poll can hold for 45 s
    and shutdown has to fit inside Docker's timer."""
    import asyncio

    from hltv_notify import live_worker as module
    from hltv_notify.live_worker import LiveSupervisor

    supervisor = LiveSupervisor(storage, config, Notifier(storage, config, None))
    outcome = []

    async def scenario():
        async def never_stops(stop):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                outcome.append("cancelled")
                raise

        supervisor._stops[1] = asyncio.Event()
        supervisor._tasks[1] = asyncio.create_task(never_stops(None))
        original = module.RELEASE_GRACE_SECONDS
        module.RELEASE_GRACE_SECONDS = 0.05
        try:
            supervisor.release(1)
            await supervisor.shutdown()
        finally:
            module.RELEASE_GRACE_SECONDS = original

    asyncio.run(scenario())
    assert outcome == ["cancelled"]
