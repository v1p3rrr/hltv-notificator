"""The daily digest: "here is what is on in the next 24 hours".

A reminder answers "this match starts soon"; this answers "is there anything
to plan the day around", and it is asked at times the person chooses — nine in
the morning, noon, whatever suits them. Silence is a real answer: on a day with
nothing on, nothing is sent. A digest that arrives every morning to say "no
matches" is a digest people mute.

Two things follow from the times being wall-clock:

* they are stored in the SUBSCRIBER'S zone (`/tz`), not in UTC. Nine o'clock
  means nine o'clock where they are, on both sides of a daylight-saving jump;
* the event is targeted at one chat (`only_chat`), like a reminder. Two people
  with different times, different zones and different teams do not share a
  digest, so there is nothing to gain from building it once.

The window is a rolling 24 hours from the moment it fires, not "the rest of
the calendar day". At nine in the morning a match at seven tomorrow is worth
knowing about, and one at eleven tonight is not more urgent for being on the
same date.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from .config import Config
from .models import Event, MatchState
from .state.db import Storage, iso, utcnow

log = logging.getLogger(__name__)

TICK_SECONDS = 30.0

# How far ahead a digest looks.
WINDOW_HOURS = 24

# How late a missed slot may still go out. The service is restarted, a host
# reboots, and the nine o'clock digest would otherwise be lost for the day —
# but one arriving at eleven at night because the container was down since
# morning is noise, not news. An hour covers a restart and stops well short of
# the next slot people usually pick.
CATCH_UP_MINUTES = 60

# What the page calls a match that is being played. Not TERMINAL's complement:
# SCHEDULED and IMMINENT are ahead of us, and only these three mean "on air".
PLAYING = (MatchState.LIVE, MatchState.MAP_LIVE, MatchState.MAP_BREAK)


def parse_time(text: str) -> Optional[int]:
    """`"9"`, `"9:00"`, `"09:30"`, `"21.30"` -> minutes from midnight.

    None means it could not be read. Deliberately strict about the RANGE and
    lenient about the shape: a person typing 25:00 has made a mistake worth
    telling them about, whereas a dot instead of a colon is not.
    """
    text = (text or "").strip().replace(".", ":").replace("-", ":")
    if not text:
        return None
    hours, _, minutes = text.partition(":")
    if not hours.isdigit() or (minutes and not minutes.isdigit()):
        return None
    hour = int(hours)
    minute = int(minutes or 0)
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def clock(minute_of_day: int) -> str:
    """540 -> "09:00"."""
    return f"{minute_of_day // 60:02d}:{minute_of_day % 60:02d}"


def describe_matches(storage: Storage, config: Config, chat_id: str,
                     rows) -> List[Dict]:
    """Database rows -> what a message says about them, this reader's team
    first.

    Shared with `/next`, which asks the same thing over a longer span. The
    conversion is here and not in the renderer because it needs the storage —
    and it is one function rather than two because a digest and `/next`
    disagreeing about which team leads a line would be a bug nobody would
    think to look for.

    Turned to face the reader HERE rather than at render time, which a score
    may not be: both callers know the one chat they are writing to, so there
    is no second reader to get it backwards.
    """
    mine = {row["team_id"] for row in storage.teams(chat_id)}
    found: List[Dict] = []
    for row in rows:
        team_id, opponent_id = row["team_id"], row["opponent_id"]
        opponent = row["opponent_name"]
        if team_id not in mine and opponent_id in mine:
            # The match is stored from the other team's point of view: the
            # perspective is chosen once, by whoever saw the match first.
            team_id, opponent_id = opponent_id, team_id
            opponent = storage.team_name(row["team_id"], config.team_name)
        found.append({
            "match_id": row["match_id"],
            "team_name": storage.team_name(team_id, config.team_name),
            "team_id": team_id,
            "opponent": opponent,
            "opponent_id": opponent_id,
            "event_name": row["event_name"],
            "start_utc": row["start_utc"],
            "url": row["url"],
            "live": row["state"] in PLAYING,
        })
    return found


class DigestScheduler:
    def __init__(self, storage: Storage, config: Config):
        self.storage = storage
        self.config = config

    def due(self, now: Optional[datetime] = None) -> List[Event]:
        """Digests that should go out right now."""
        now = now or utcnow()
        events: List[Event] = []
        for chat_id in self.storage.subscriber_ids():
            slots = self.storage.digest_times(chat_id)
            if not slots:
                continue
            local = self._local(chat_id, now)
            if local is None:
                continue
            for minute in slots:
                fired = local.replace(hour=minute // 60, minute=minute % 60,
                                      second=0, microsecond=0)
                if not (fired <= local < fired + timedelta(minutes=CATCH_UP_MINUTES)):
                    continue
                matches = self.matches_for(chat_id, now)
                if not matches:
                    # Nothing on. The digest is not sent at all rather than
                    # sent empty — see the module docstring.
                    log.debug("digest %s for %s: nothing in the next %dh",
                              clock(minute), chat_id, WINDOW_HOURS)
                    continue
                events.append(self._event(chat_id, minute, fired, matches, now))
        return events

    def _local(self, chat_id: str, now: datetime) -> Optional[datetime]:
        name = self.storage.subscriber_timezone(chat_id, self.config.timezone)
        try:
            return now.astimezone(ZoneInfo(name))
        except Exception:  # noqa: BLE001 - the zone comes from a person or .env
            # `/tz` validates what it stores, so this is the environment's
            # default being wrong. Skipping is right: guessing UTC would send
            # the digest at an hour nobody asked for.
            log.warning("chat %s has an unusable timezone %r — digest skipped",
                        chat_id, name)
            return None

    def matches_for(self, chat_id: str, now: Optional[datetime] = None) -> List[Dict]:
        """This subscriber's matches inside the window, their team first."""
        now = now or utcnow()
        visible = self.storage.visible_match_ids(chat_id)
        rows = [row for row in self.storage.matches_within(WINDOW_HOURS, now)
                if row["match_id"] in visible]
        return describe_matches(self.storage, self.config, chat_id, rows)

    def _event(self, chat_id: str, minute: int, fired: datetime,
               matches: List[Dict], now: datetime) -> Event:
        return Event(
            type="E14",
            # The LOCAL date and the slot, and nothing about the matches. The
            # message asserts "this is what the next 24 hours hold as of
            # 09:00", and that assertion does not change when a match is added
            # an hour later — a second digest for the same slot would be a
            # duplicate, not an update. The local date is the subscriber's own,
            # which is why the chat prefix `record_event` adds matters here.
            idempotency_key=f"E14:{fired.date().isoformat()}:{minute:04d}",
            match_id=None,
            payload={
                "only_chat": chat_id,
                "at": clock(minute),
                # What "Today" means in the message. Carried rather than read
                # off the clock at render time: the queue retries, and a late
                # delivery must not relabel the days.
                "now_utc": iso(now),
                "hours": WINDOW_HOURS,
                "matches": matches,
            },
        )

    async def run(self, stop: asyncio.Event, notifier) -> None:
        while not stop.is_set():
            try:
                for event in self.due():
                    notifier.enqueue(event)
            except Exception:  # noqa: BLE001 - the digest does not bring the process down
                log.exception("the digest scheduler failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
            except asyncio.TimeoutError:
                continue
