"""Live Worker: holds the feed connection for the duration of a match.

It only lives while the match is running. A dropped connection is normal, not
an emergency: over a recording of a real match there were 15 connects and 14
drops in an hour. Hence reconnecting with backoff, and all the protection
against repeats living in the state machine.

403 is handled separately: it is not a network failure but a "back off".
Reconnecting with the usual backoff in that situation only pesters the source,
so the pause is measured in minutes while match-page polling carries on as
before.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import replace
from typing import Dict, Optional

from .config import Config
from .models import Event
from .notify.live_message import LiveMessenger
from .notify.outbox import Notifier
from .sources.scorebot import (FeedIdle, FeedRejected, FeedUnavailable,
                               ScorebotClient, frames_from_packets)
from .state.db import Storage, utcnow
from .state.live_machine import LiveMachine

log = logging.getLogger(__name__)

# The lower bound of the pause after a 403. The source explicitly asked us to
# back off, and config must not lower it below something reasonable.
MIN_REJECTED_COOLDOWN_SECONDS = 60.0
MAX_BACKOFF_SECONDS = 60.0


class LiveWorker:
    """One connection for one match."""

    def __init__(self, storage: Storage, config: Config, notifier: Notifier,
                 match_id: int, url: str, messenger: Optional[LiveMessenger] = None):
        self.storage = storage
        self.config = config
        self.notifier = notifier
        self.messenger = messenger
        self.match_id = match_id
        self.url = url
        self.machine = LiveMachine(storage, config)
        self.connected = False
        self.rejected_until: float = 0.0

    async def run(self, stop: asyncio.Event) -> None:
        attempt = 0
        while not stop.is_set():
            client = ScorebotClient(
                self.match_id, referer=self.url,
                impersonate=self.config.impersonate,
                proxy=self.config.proxy)
            try:
                await client.connect()
                await client.subscribe()
                self.connected = True
                attempt = 0
                log.info("live feed of match %s connected (sid %s)", self.match_id, client.sid)
                await self._consume(client, stop)
            except FeedRejected as exc:
                self.connected = False
                cooldown = max(float(self.config.live_feed_cooldown),
                               MIN_REJECTED_COOLDOWN_SECONDS)
                log.error("live feed of match %s rejected (%s) — pausing %.0f min, "
                          "working from the match page",
                          self.match_id, exc, cooldown / 60)
                await self._sleep(cooldown, stop)
            except FeedUnavailable as exc:
                self.connected = False
                attempt += 1
                delay = min(2 ** attempt, MAX_BACKOFF_SECONDS) * random.uniform(0.8, 1.2)
                log.warning("live feed of match %s dropped (%s), reconnecting in %.0fs",
                            self.match_id, exc, delay)
                await self._sleep(delay, stop)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the worker must not bring the process down
                self.connected = False
                log.exception("unexpected failure of the live feed of match %s", self.match_id)
                await self._sleep(30, stop)
            finally:
                self.connected = False
                await client.close()

    async def _consume(self, client: ScorebotClient, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                packets = await client.poll()
            except FeedIdle:
                # The feed is quiet — a pause on the map or the break between
                # maps. The connection is alive, no need to reconnect.
                log.debug("live feed of match %s is quiet, polling again", self.match_id)
                continue
            for frame in frames_from_packets(packets):
                events = self.machine.apply(self.match_id, frame)
                # E5 is held back: where the live message is on, it carries the
                # map start itself. Sending both would mean two messages about
                # one thing, and in the wrong order at that — the live message
                # goes straight to Telegram while events wait in the queue.
                started = next((e for e in events if e.type == "E5"), None)
                for event in events:
                    if event is not started:
                        self.notifier.enqueue(event)
                await self._refresh_live_message(frame, events, started)

    async def _refresh_live_message(self, frame, events, started=None) -> None:
        """The live message is redrawn after apply, so that the final edit
        already carries the series score including the map just taken."""
        snapshot = self.machine.snapshot(self.match_id, frame) if self.messenger else None
        if self.messenger is None or not snapshot:
            # No live message at all — then E5 goes the ordinary way.
            if started is not None:
                self.notifier.enqueue(started)
            return

        if any(event.type == "E6" for event in events):
            await self.messenger.finalize(self.match_id, snapshot)
            return

        missed = await self.messenger.update(self.match_id, snapshot,
                                             map_started=started is not None)
        if started is None:
            return
        for chat_id in missed:
            # The map's card did not reach this chat. A milestone must not be
            # lost on the best-effort path, so it goes through the queue —
            # addressed to that one chat, so the others get no second copy.
            log.warning("the live message for match %s did not reach %s, "
                        "sending the map start through the queue",
                        self.match_id, chat_id)
            self.notifier.enqueue(replace(
                started, payload={**started.payload, "only_chat": chat_id}))

    @staticmethod
    async def _sleep(seconds: float, stop: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


class LiveSupervisor:
    """Brings workers up and takes them down for running matches.

    Several matches can run at once — this team does that — so it is one worker
    per match, not one per service.
    """

    def __init__(self, storage: Storage, config: Config, notifier: Notifier,
                 messenger: Optional[LiveMessenger] = None):
        self.storage = storage
        self.config = config
        self.notifier = notifier
        self.messenger = messenger
        self._workers: Dict[int, LiveWorker] = {}
        self._tasks: Dict[int, asyncio.Task] = {}
        self._stops: Dict[int, asyncio.Event] = {}

    @property
    def any_connected(self) -> bool:
        return any(worker.connected for worker in self._workers.values())

    def connected_matches(self) -> Dict[int, bool]:
        return {match_id: worker.connected for match_id, worker in self._workers.items()}

    def ensure(self, match_id: int, url: str) -> None:
        if match_id in self._tasks and not self._tasks[match_id].done():
            return
        stop = asyncio.Event()
        worker = LiveWorker(self.storage, self.config, self.notifier, match_id, url,
                            messenger=self.messenger)
        self._workers[match_id] = worker
        self._stops[match_id] = stop
        self._tasks[match_id] = asyncio.create_task(
            worker.run(stop), name=f"live-{match_id}")
        log.info("live feed brought up for match %s", match_id)

    def release(self, match_id: int) -> None:
        stop = self._stops.pop(match_id, None)
        if stop is not None:
            stop.set()
        task = self._tasks.pop(match_id, None)
        if task is not None:
            task.cancel()
        self._workers.pop(match_id, None)
        log.info("live feed for match %s stopped", match_id)

    def reconcile(self, live_match_ids: Dict[int, str]) -> None:
        """Bring the set of workers in line with the running matches."""
        for match_id, url in live_match_ids.items():
            self.ensure(match_id, url)
        for match_id in list(self._tasks):
            if match_id not in live_match_ids:
                self.release(match_id)

    async def shutdown(self) -> None:
        # The list is collected BEFORE release(): release removes the task from
        # _tasks, so this used to end up with an empty list every time, gather
        # was never called and nobody waited for the workers to finish. Their
        # finally, which closes the session, did not get to run before the
        # event loop closed.
        tasks = list(self._tasks.values())
        for match_id in list(self._tasks):
            self.release(match_id)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
