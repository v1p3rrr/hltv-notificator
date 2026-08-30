"""Transitions born from match-page observations: E4, E7 and the stall detector.

As with the schedule, an event is born on a transition. The match page is
polled every minute and returns the same thing — "we saw LIVE, send E4" would
mean a notification on every poll.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from ..config import Config
from ..models import Event, MatchState
from ..sources import match_page, scorebot
from ..sources.match_page import MapLine, MatchObservation
from .db import Storage, parse_iso, utcnow

log = logging.getLogger(__name__)

STATUS_TO_STATE = {
    match_page.STATUS_LIVE: MatchState.LIVE,
    match_page.STATUS_OVER: MatchState.FINISHED,
    match_page.STATUS_UPCOMING: MatchState.SCHEDULED,
}

TERMINAL = {MatchState.FINISHED, MatchState.CANCELLED}

# A break between maps is a normal pause, and it can be long: at a LAN twenty
# minutes pass between maps. The "match stalled" threshold is stretched for
# that time, otherwise every break would raise a false alarm.
BREAK_THRESHOLD_MULTIPLIER = 3

# How long the phase the feed reported stays worth believing. Longer than any
# gap between frames, shorter than a warmup: if the feed went quiet, the page
# must get back to deciding for itself.
FEED_PHASE_FRESH_SECONDS = 120


def _overtime(line: MapLine) -> bool:
    """Overtime is judged by the number of halves, not by score arithmetic.

    Overtime rules differ between tournaments, and tying this to "more than 13
    rounds" means breaking on the first non-standard format. More than two
    halves means there was an overtime.
    """
    if not line.halves:
        return False
    return line.halves.count(";") >= 2


class MatchMachine:
    def __init__(self, storage: Storage, config: Config):
        self.storage = storage
        self.config = config

    # ------------------------------------------------------------------

    def apply(self, observation: MatchObservation, now: Optional[datetime] = None,
              *, feed_connected: bool = False) -> List[Event]:
        now = now or utcnow()
        match_id = observation.match_id
        # The perspective comes from the match, not from the config: there can
        # be several tracked teams, and in a match between two of them the
        # score must be counted from ONE of them — otherwise the idempotency
        # keys come out mirrored.
        team_id = self.storage.canonical_team(match_id) or self.config.team_id

        if observation.our_side(team_id) is None:
            # We were served the wrong page, or the markup changed. Silently
            # interpreting that is dangerous: we could record someone else's
            # score.
            log.warning("team %s is not on match page %s — observation discarded",
                        team_id, match_id)
            return []

        row = self.storage.get_match(match_id)
        state_row = self.storage.get_state(match_id)
        previous = state_row["state"] if state_row else MatchState.SCHEDULED
        # Specifically the first observation FROM THE MATCH PAGE, not the first
        # ever: the state row is created by the schedule polling, so a
        # "state_row is None" check here is almost always false and maps played
        # before we started watching would receive E6 retroactively.
        #
        # The marker is a dedicated field, not last_source. Through last_source
        # this did not work: the live feed rewrites it on every frame, so "the
        # match page has not looked yet" was true THE WHOLE TIME the feed ran,
        # and page-side E6 always went down the silent branch. The page stopped
        # being a confirming source exactly when the feed missed something.
        first_observation = state_row is None or state_row["page_seen_utc"] is None
        target = STATUS_TO_STATE.get(observation.status, previous)

        # The snapshot is taken BEFORE the results are written: it shows which
        # maps were decided just now. Otherwise there would be nothing to
        # compare against.
        known_maps = {r["map_number"] for r in self.storage.map_results(match_id)}
        seen_live = previous in (MatchState.LIVE, MatchState.MAP_LIVE, MatchState.MAP_BREAK)

        events: List[Event] = []
        ours, theirs = observation.series_score(team_id)
        current_map = self._current_map(observation)

        self.storage.set_state(
            match_id, target, source="match_page",
            current_map_number=current_map.number if current_map else None,
            current_map_name=current_map.name if current_map else None,
            series_score=f"{ours}-{theirs}",
        )
        self._store_map_results(observation, team_id)
        if observation.max_rounds_regulation and observation.max_rounds_overtime:
            self.storage.set_map_format(match_id, observation.max_rounds_regulation,
                                        observation.max_rounds_overtime)
        # The series format is stored for the live feed: only with it can the
        # feed tell that the last map ended the match.
        self.storage.set_best_of(match_id, observation.best_of)
        # The marker is set AFTER first_observation has been computed: it
        # refers to the previous observations, not to this one.
        self.storage.mark_page_seen(match_id)
        # The live feed needs the map lineup: it knows the map name but not its
        # number in the series. Record it as soon as the veto has been played.
        lineup = [line.name for line in observation.maps]
        if any(name and name.upper() != "TBA" for name in lineup):
            self.storage.set_map_lineup(match_id, lineup)

        if target == MatchState.LIVE and previous not in TERMINAL:
            # Not "on the transition to LIVE" any more, but "while it is live
            # and the start has not been announced". The transition happens
            # once, and the announcement may have to wait for the warmup to
            # end — so the attempt has to be repeatable.
            # "The page has not spoken about this match yet" counts as the
            # transition too. The state is shared, and the live machine writes
            # LIVE into it as well — if the feed happened to come up first, the
            # page would decide the start had already been announced and stay
            # quiet forever. `first_observation` is the page's own memo.
            started = self._start_event(observation, row, team_id, now,
                                        first_time=first_observation
                                        or previous != MatchState.LIVE,
                                        feed_connected=feed_connected)
            if started is not None:
                events.append(started)

        discovered_finished = target == MatchState.FINISHED and not seen_live
        events.extend(self._map_events(
            observation, row, team_id, known_maps,
            silent=first_observation or discovered_finished))

        if target == MatchState.FINISHED and previous not in TERMINAL:
            if not seen_live:
                # The match is already over and we never saw it running.
                # Sending E4 and E6 after the fact is pointless — go straight
                # to the result.
                log.info("match %s discovered already finished, E4 and E6 skipped",
                         match_id)
            events.append(self._event_e7(observation, row, team_id, ours, theirs))

        events.extend(self._check_stall(observation, team_id, target, now,
                                        feed_connected=feed_connected))
        return events

    # ------------------------------------------------------------------

    def _current_map(self, observation: MatchObservation) -> Optional[MapLine]:
        """The current map: the first undecided one with a known name."""
        live = observation.live_map()
        if live is not None:
            return live
        for line in observation.maps:
            if not observation.is_final(line) and line.name and line.name.upper() != "TBA":
                return line
        final = observation.final_maps()
        return final[-1] if final else None

    def _map_events(self, observation: MatchObservation, row, team_id: int,
                    known_maps: set, *, silent: bool) -> List[Event]:
        """E6 — on a map's transition from undecided to decided.

        The event is born once per map: later polls see the same map already in
        known_maps. The idempotency key includes the score, so even a score
        correction on HLTV's side will not lead to silence.
        """
        events: List[Event] = []
        for line in observation.final_maps():
            if line.number in known_maps:
                continue
            if silent:
                # The map was played before we started watching this match.
                # That is not a transition but the state at the moment we met.
                log.info("match %s: map %d had already been played by the time we "
                         "looked, E6 skipped", observation.match_id, line.number)
                continue
            events.append(self._event_e6(observation, row, team_id, line))
        return events

    def _store_map_results(self, observation: MatchObservation, team_id: int) -> None:
        for line in observation.final_maps():
            ours, theirs = observation.map_score(line, team_id)
            if ours is None or theirs is None:
                continue
            self.storage.record_map_result(
                match_id=observation.match_id, map_number=line.number, map_name=line.name,
                score_team=ours, score_opponent=theirs, overtime=_overtime(line),
            )

    # ------------------------------------------------------------------

    def _check_stall(self, observation: MatchObservation, team_id: int,
                     state: str, now: datetime, *, feed_connected: bool = False) -> List[Event]:
        """"The match has stalled": it is live but nothing changes past a threshold.

        The point of the event is "I have gone blind", not "the players are
        taking their time". Hence:

        * while the live feed is connected there is no alarm at all. We see the
          match through the feed, and its silence during a pause is not
          blindness. The feed's own health is tracked separately;
        * between maps the threshold is stretched: at a LAN a break easily
          lasts twenty minutes, and on a real match this already produced a
          false alarm.
        """
        if state != MatchState.LIVE:
            return []
        if feed_connected:
            # The timer does not accumulate, otherwise the alarm would fire
            # instantly after the feed disconnects, for all the time it worked.
            self.storage.set_progress(
                observation.match_id, observation.progress_signature(team_id), now)
            return []

        signature = observation.progress_signature(team_id)
        state_row = self.storage.get_state(observation.match_id)
        previous_hash = state_row["progress_hash"] if state_row else None
        since_raw = state_row["progress_since_utc"] if state_row else None

        if previous_hash != signature or since_raw is None:
            self.storage.set_progress(observation.match_id, signature, now)
            return []

        between_maps = observation.live_map() is None
        threshold = timedelta(minutes=self.config.stale_minutes)
        if between_maps:
            threshold *= BREAK_THRESHOLD_MULTIPLIER

        stalled_for = now - parse_iso(since_raw)
        if stalled_for < threshold:
            return []

        digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12]
        minutes = int(stalled_for.total_seconds() // 60)
        return [Event(
            type="E8",
            idempotency_key=f"E8:match:{observation.match_id}:stale:{digest}",
            match_id=observation.match_id,
            payload={
                "reason": "The match has stalled",
                "detail": (f"The score and state have not changed for {minutes} min, "
                           f"the match page still says LIVE, "
                           f"there is no live feed."),
            },
        )]

    # ------------------------------------------------------------------

    def _start_event(self, observation: MatchObservation, row, team_id: int,
                     now: datetime, *, first_time: bool,
                     feed_connected: bool) -> Optional[Event]:
        """"The match has started" — but only once it has.

        The page's LIVE flag is not the start of play. HLTV raises it when the
        teams connect to the server, and the warmup before the first map can
        run twenty minutes with the score at 0:0. So while the live feed says
        this very moment that it is a warmup, the message is written and put
        aside; the live machine sends it on the first round actually played,
        with the same idempotency key.

        The gate is deliberately narrow. If the feed is not connected, or has
        not spoken for a while, or has never been up at all (a 403 cooldown),
        the page decides on its own exactly as it always did — losing E4
        entirely would be far worse than sending it during a warmup.
        """
        match_id = observation.match_id
        held = self.storage.pending_start_event(match_id)
        if not first_time and held is None:
            # Not the transition to LIVE and nothing was put aside: the message
            # has already gone out. This is the ordinary polling of a running
            # match, which must stay silent.
            return None
        if self.storage.start_event_sent(match_id):
            # The live machine got there first.
            self.storage.set_pending_start_event(match_id, None)
            return None

        event = self._event_e4(observation, row, team_id)
        if not self._feed_says_warmup(match_id, now, feed_connected=feed_connected):
            self.storage.set_pending_start_event(match_id, None)
            return event

        log.info("match %s is live on the page but the feed says warmup — "
                 "the start message waits for the first round", match_id)
        self.storage.set_pending_start_event(match_id, event.payload)
        return None

    def _feed_says_warmup(self, match_id: int, now: datetime, *,
                          feed_connected: bool) -> bool:
        if not feed_connected:
            return False
        state_row = self.storage.get_state(match_id)
        if state_row is None or state_row["live_round_state"] != scorebot.ROUND_WARMUP:
            return False
        seen = state_row["live_frame_utc"]
        if not seen:
            return False
        # A phase nobody has confirmed for minutes is not an answer. The feed
        # may have dropped mid-warmup, and then there is nothing left to wait
        # for.
        return now - parse_iso(seen) <= timedelta(seconds=FEED_PHASE_FRESH_SECONDS)

    def _event_e4(self, observation: MatchObservation, row, team_id: int) -> Event:
        opponent_id, opponent_name = observation.opponent(team_id)
        return Event(
            type="E4",
            idempotency_key=f"E4:{observation.match_id}:started",
            match_id=observation.match_id,
            payload={
                "team_name": self.storage.team_name(team_id, self.config.team_name),
                "team_id": team_id,
                "opponent": opponent_name or (row["opponent_name"] if row else ""),
                "opponent_id": opponent_id,
                "event_name": observation.event_name or (row["event_name"] if row else ""),
                "best_of": observation.best_of,
                "picks": observation.picks(team_id),
                "url": row["url"] if row else "",
            },
        )

    def _event_e6(self, observation: MatchObservation, row, team_id: int,
                  line: MapLine) -> Event:
        our_score, their_score = observation.map_score(line, team_id)
        opponent_id, opponent_name = observation.opponent(team_id)
        # The series score is taken as of this map, not the final one: two maps
        # can finish between two polls, and the final score would be a lie in
        # the message about the first of them.
        series_ours, series_theirs = observation.series_after(line.number, team_id)
        return Event(
            type="E6",
            idempotency_key=(f"E6:{observation.match_id}:map:{line.number}"
                             f":result:{our_score}-{their_score}"),
            match_id=observation.match_id,
            payload={
                "team_name": self.storage.team_name(team_id, self.config.team_name),
                "team_id": team_id,
                "opponent": opponent_name or (row["opponent_name"] if row else ""),
                "opponent_id": opponent_id,
                "event_name": observation.event_name or (row["event_name"] if row else ""),
                "map_number": line.number,
                "map_name": line.name,
                "score_team": our_score,
                "score_opponent": their_score,
                "overtime": _overtime(line),
                "halves": line.halves,
                "series_team": series_ours,
                "series_opponent": series_theirs,
                "url": row["url"] if row else "",
            },
        )

    def _event_e7(self, observation: MatchObservation, row, team_id: int,
                  ours: int, theirs: int) -> Event:
        opponent_id, opponent_name = observation.opponent(team_id)
        # The live feed may already have reported the match as finished from
        # the map count. If the page agrees, the key matches and the unique
        # index swallows this one silently. If it disagrees, the key differs
        # and the message goes out — as a correction, which it must say.
        key = f"E7:{observation.match_id}:finished:{ours}-{theirs}"
        corrects = [k for k in self.storage.finished_event_keys(observation.match_id)
                    if not k.endswith(key)]
        maps = []
        for line in observation.final_maps():
            our_score, their_score = observation.map_score(line, team_id)
            maps.append({
                "number": line.number,
                "name": line.name,
                "score_team": our_score,
                "score_opponent": their_score,
                "overtime": _overtime(line),
            })
        return Event(
            type="E7",
            idempotency_key=key,
            match_id=observation.match_id,
            payload={
                "team_name": self.storage.team_name(team_id, self.config.team_name),
                "team_id": team_id,
                "opponent": opponent_name or (row["opponent_name"] if row else ""),
                "opponent_id": opponent_id,
                "event_name": observation.event_name or (row["event_name"] if row else ""),
                "series_team": ours,
                "series_opponent": theirs,
                # None means a draw: a BO2 quite happily ends 1:1. A boolean
                # here would mean a defeat, and for the recipient following the
                # opponent orient() would flip it into a win — about one and
                # the same result.
                "won": None if ours == theirs else ours > theirs,
                "maps": maps,
                "corrected": bool(corrects),
                "url": row["url"] if row else "",
            },
        )
