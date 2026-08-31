"""Transitions driven by live-feed frames: E5 and an immediate E6.

What it is all for: the maps section on the match page updates late, so a
page-driven E6 does not arrive at the winning round. The feed knows the score
at once, and the decision is made from the score — the rule lives in
hltv_notify.scoring, with thresholds derived from the format the feed itself
reports.

Two properties of the feed force events to be born ONLY on transitions:
  * a scoreboard frame arrives several times a second and always in full;
  * on every connect the full state arrives again, and the log carries a
    backlog of things that already happened.
So decisions are built on comparison with the stored state, not on the fact
that a frame arrived. And that is also why the log is not used at all.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from .. import settings
from ..config import Config
from ..models import Event, MatchState
from ..scoring import map_completed, rounds_to_win, series_decided
from ..sources.scorebot import ROUND_WARMUP, LiveFrame, PlayerLine
from .comeback import ComebackTracker
from .db import Storage
from .multikill import MultikillTracker

log = logging.getLogger(__name__)

SOURCE = "scorebot"


def normalize_map_name(name: str) -> str:
    """`de_mirage` -> `Mirage`.

    The feed gives internal map names, the match page gives human ones. They
    have to be stored and compared in one shape, otherwise a change of source
    would look like a change of map and produce a false E5.
    """
    cleaned = (name or "").strip()
    for prefix in ("de_", "cs_"):
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    return cleaned[:1].upper() + cleaned[1:] if cleaned else ""


class LiveMachine:
    def __init__(self, storage: Storage, config: Config):
        self.storage = storage
        self.config = config
        # A tracker per EVERY tracked team in the match: if tracked teams play
        # each other, a 4k by a player of either is its own highlight, and one
        # must not be muted for the sake of the other. They live in the
        # worker's memory and survive reconnects inside it.
        self._multikill: Dict[int, MultikillTracker] = {}
        # Score milestones already announced — map points, halves, the start
        # of an overtime. The journal would swallow the repeats anyway (the key
        # is the same one), but a score stands for a whole round, i.e. some
        # hundreds of frames, and each of them would otherwise mean a write to
        # the queue and a line in the log. In memory, like the multikill
        # trackers: after a restart the journal is still there to keep the
        # message from going out twice.
        self._announced: set = set()
        # The score trajectory of the map being played, for the comeback line
        # on E6. In memory for the same reason as the multikill trackers: it
        # survives feed reconnects, and a restart in the middle of a map can
        # understate a comeback but never invent one.
        self._comeback: Dict[str, ComebackTracker] = {}

    def _threshold(self, name: str) -> int:
        """The lowest threshold any subscriber is waiting for.

        Thresholds are per person now, and an event is still born ONCE for
        everybody (see the architecture doc). The way out is to build at the
        lowest bar in use and let the queue withhold the result from whoever
        set a higher one — the payload carries the number it was measured
        against. The other direction does not work: an event never born cannot
        be given to the person who wanted it.

        Read on every map rather than cached: someone changes a setting between
        maps and expects the next one to obey it.
        """
        return self.storage.threshold_in_use(
            name, settings.default_for(self.config, name))

    def _comeback_tracker(self, map_name: str) -> ComebackTracker:
        """One tracker per map: a new map starts from an empty score."""
        if map_name not in self._comeback:
            self._comeback[map_name] = ComebackTracker(self._threshold("comeback"))
        return self._comeback[map_name]

    def _tracker(self, team_id: int) -> MultikillTracker:
        if team_id not in self._multikill:
            self._multikill[team_id] = MultikillTracker(self._threshold("multikill"))
        return self._multikill[team_id]

    # ------------------------------------------------------------------

    def apply(self, match_id: int, frame: LiveFrame) -> List[Event]:
        team_id = self.storage.canonical_team(match_id) or self.config.team_id
        ours, theirs = frame.our_score(team_id)
        if ours is None or theirs is None:
            # Recording somebody else's score is worse than staying quiet. But
            # it is only worth making noise when the team ids are filled in and
            # simply are not ours; while the feed has not filled them, these
            # are ordinary transitional frames between maps.
            if frame.ct_team_id or frame.t_team_id:
                log.warning("team %s is not in the frame of match %s — frame discarded",
                            team_id, match_id)
            else:
                log.debug("transitional frame of match %s with no teams — skipped", match_id)
            return []

        map_name = normalize_map_name(frame.map_name)
        if not map_name:
            return []

        recorded = {row["map_name"]: row["map_number"]
                    for row in self.storage.map_results(match_id)}
        if map_name in recorded:
            # The map is already recorded as played: the feed keeps sending its
            # final score for a while, there is nothing to react to.
            return []

        state_row = self.storage.get_state(match_id)
        # Read OUR OWN memo, not current_map_name: that field is written by
        # both machines, and the match page puts the first undecided, i.e. the
        # UPCOMING map there. Reading it, the live machine saw "the map has not
        # changed" at exactly the moment a map started, and E5 was never born.
        previous_map = state_row["live_map_name"] if state_row else None
        map_number = self._map_number(match_id, map_name, len(recorded))

        # The trajectory is followed on every frame, warmup included (0:0 costs
        # nothing), so that at the winning round the whole map is already
        # behind us and the comeback can be judged without asking anybody.
        self._comeback_tracker(map_name).observe(ours, theirs)

        events: List[Event] = []
        started = self._released_start_event(match_id, frame)
        if started is not None:
            events.append(started)
        events.extend(self._multikill_events(match_id, frame, map_number, map_name))
        if self._is_new_map(previous_map, map_name, frame):
            events.append(self._event_e5(match_id, frame, map_number, map_name, len(recorded)))

        self.storage.set_map_format(match_id, frame.regulation, frame.overtime)
        verdict = map_completed(ours, theirs,
                                regulation=frame.regulation, overtime=frame.overtime)
        if verdict.completed:
            events.append(self._event_e6(match_id, frame, map_number, map_name,
                                         ours, theirs, verdict.overtime_number > 0))
            self.storage.record_map_result(
                match_id=match_id, map_number=map_number, map_name=map_name,
                score_team=ours, score_opponent=theirs,
                overtime=verdict.overtime_number > 0)
            log.info("match %s: map %d (%s) taken at %d:%d, overtime #%d",
                     match_id, map_number, map_name, ours, theirs, verdict.overtime_number)
            finished = self._event_e7(match_id, frame, team_id)
            if finished is not None:
                events.append(finished)
        else:
            phase = self._event_e12(match_id, frame, map_number, map_name, ours, theirs)
            if phase is not None:
                events.append(phase)
            point = self._event_e11(match_id, frame, map_number, map_name, ours, theirs)
            if point is not None:
                events.append(point)

        series = self._series(match_id)
        self.storage.set_state(
            match_id, MatchState.LIVE, source=SOURCE,
            current_map_number=map_number, current_map_name=map_name,
            current_map_score=f"{ours}-{theirs}",
            series_score=f"{series[0]}-{series[1]}")
        # Written after set_state, which is what creates the row on the very
        # first frame. On every frame, warmup included: the page machine reads
        # it to decide whether "the match has started" is true yet, and a phase
        # that stops being refreshed goes stale on purpose.
        self.storage.set_live_phase(match_id, frame.round_state)
        if not self._warming_up(frame):
            # The memo is advanced only once the map is really being played —
            # see _is_new_map.
            self.storage.set_live_map(match_id, map_name)
        return events

    # ------------------------------------------------------------------

    def snapshot(self, match_id: int, frame: LiveFrame) -> Optional[dict]:
        """Data for the live score message.

        Separate from apply(): the live message is not an event. It has no
        idempotency key and does not need re-delivery after a restart, it
        simply needs redrawing with the current state.
        """
        team_id = self.storage.canonical_team(match_id) or self.config.team_id
        ours, theirs = frame.our_score(team_id)
        if ours is None or theirs is None:
            return None
        map_name = normalize_map_name(frame.map_name)
        if not map_name:
            return None
        recorded = self.storage.map_results(match_id)
        series = self._series(match_id)
        row = self.storage.get_match(match_id)
        return {
            "map_number": self._map_number(match_id, map_name, len(recorded)),
            "map_name": map_name,
            "score_team": ours,
            "score_opponent": theirs,
            "round": frame.current_round,
            "round_state": frame.round_state,
            "warmup": self._warming_up(frame),
            "in_play": frame.in_play,
            "series_team": series[0],
            "series_opponent": series[1],
            "opponent": frame.opponent_name(team_id)
                        or (row["opponent_name"] if row else ""),
            "team_name": self.storage.team_name(team_id, self.config.team_name),
            "team_id": team_id,
            "opponent_id": self._opponent_id(match_id, team_id),
            "event_name": row["event_name"] if row else "",
            "url": row["url"] if row else "",
        }

    def _multikill_events(self, match_id: int, frame: LiveFrame, map_number: int,
                          map_name: str) -> List[Event]:
        """A multikill by a player of OUR team — so a highlight can be clipped."""
        if self._threshold("multikill") <= 0:
            # Nobody is waiting for one. Not the same as a threshold of four
            # that no round reaches: this skips the work entirely.
            return []
        # Every tracked participant of the match, not only the canonical team:
        # if tracked teams play each other, a 4k by a player of either is its
        # own highlight.
        canonical = self.storage.canonical_team(match_id) or self.config.team_id
        tracked = self.storage.match_team_ids(match_id) or [canonical]
        events: List[Event] = []
        for tracked_team in tracked:
            events.extend(self._multikill_for_team(
                match_id, frame, map_number, map_name, tracked_team))
        return events

    def _multikill_for_team(self, match_id: int, frame: LiveFrame, map_number: int,
                            map_name: str, tracked_team: int) -> List[Event]:
        """The event is built entirely from the PLAYER'S TEAM's point of view.

        This is easy to get wrong: take the canonical team's context and swap
        only the name, and the opponent turns out to be that same team
        ("FORZE — FORZE") while the score stays theirs, i.e. mirrored. It
        cannot be turned around later: format.orient sees the recipient's
        team_id and concludes there is nothing to flip.
        """
        ours, theirs = frame.our_score(tracked_team)
        if ours is None or theirs is None:
            # This team is not in the frame — for example the frame arrived
            # before the feed filled the ids in. Its players' kills will not be
            # there either.
            return []
        taken = self._tracker(tracked_team).observe(
            map_name, frame.current_round, frame.round_state,
            frame.our_players(tracked_team))
        events: List[Event] = []
        for player, kills in taken:
            log.info("match %s: %s took %d kills in round %d on %s",
                     match_id, player.nick, kills, frame.current_round, map_name)
            events.append(Event(
                type="E9",
                idempotency_key=(f"E9:{match_id}:map:{map_number}"
                                 f":round:{frame.current_round}:{player.steam_id}:{kills}"),
                match_id=match_id,
                payload={
                    **self._context(match_id, frame, tracked_team),
                    "nick": player.nick,
                    "kills": kills,
                    "map_number": map_number,
                    "map_name": map_name,
                    "round": frame.current_round,
                    "score_team": ours,
                    "score_opponent": theirs,
                },
            ))
        return events

    def _opponent_id(self, match_id: int, team_id: int):
        """The opponent according to the match data: needed to turn the score
        around for a subscriber who follows precisely them."""
        row = self.storage.get_match(match_id)
        if row is None:
            return None
        others = [other for other in self.storage.match_team_ids(match_id)
                  if other != team_id]
        return others[0] if others else row["opponent_id"]

    def _map_number(self, match_id: int, map_name: str, recorded_count: int) -> int:
        """The map's number in the series.

        The feed only sends the name, it has no number. We take it from the map
        lineup read off the match page. Counting "however many maps are already
        recorded, plus one" is unreliable: the page updates late, and if the
        service connected to the feed mid-series the previous map might not be
        recorded yet — the second map would get the first one's number.
        """
        lineup = self.storage.map_lineup(match_id)
        for index, name in enumerate(lineup, start=1):
            if name and name.lower() == map_name.lower():
                return index
        return recorded_count + 1

    @staticmethod
    def _warming_up(frame: LiveFrame) -> bool:
        """Is the map still in its warmup.

        The feed says so explicitly through `currentRoundState`, and that is the
        only reliable signal. The `live` flag is NOT one: measured on a recorded
        map boundary, it only turns true once the first round has been PLAYED —
        warmup runs as `warmup/live=False` (177 frames), then the map really
        starts as `started/live=False` (64 frames), and only the end of round 1
        brings `live=True`. Gating on `live` would announce the map after its
        first round was already decided.
        """
        return frame.round_state == ROUND_WARMUP

    def _is_new_map(self, previous_map: Optional[str], map_name: str,
                    frame: LiveFrame) -> bool:
        """The map has started.

        The warmup does not count: it can run for twenty minutes, and "the map
        has started" during it is simply untrue — the score would sit at 0:0 all
        that time. Note that `live_map_name` is not advanced during the warmup
        either, otherwise this comparison would find nothing left to notice by
        the time the map really starts.

        If there is no previous map in the state, we have only just taken the
        match under observation. Announcing "the map has started" about a map
        twenty rounds in is too late — so in that case the event is only born
        if the map really is at its very beginning.
        """
        if self._warming_up(frame):
            return False
        if previous_map is None:
            return frame.current_round <= 1
        return previous_map != map_name

    def _series(self, match_id: int) -> Tuple[int, int]:
        ours = theirs = 0
        for row in self.storage.map_results(match_id):
            if row["score_team"] > row["score_opponent"]:
                ours += 1
            elif row["score_opponent"] > row["score_team"]:
                theirs += 1
        return ours, theirs

    def _url(self, match_id: int) -> str:
        row = self.storage.get_match(match_id)
        return row["url"] if row else ""

    def _context(self, match_id: int, frame: LiveFrame,
                 team_id: Optional[int] = None) -> dict:
        """The event's common header. `team_id` is whose point of view; by
        default the match's canonical team."""
        row = self.storage.get_match(match_id)
        if team_id is None:
            team_id = self.storage.canonical_team(match_id) or self.config.team_id
        return {
            "team_name": self.storage.team_name(team_id, self.config.team_name),
            "team_id": team_id,
            "opponent_id": self._opponent_id(match_id, team_id),
            "opponent": frame.opponent_name(team_id)
                        or (row["opponent_name"] if row else ""),
            "event_name": row["event_name"] if row else "",
            "url": row["url"] if row else "",
        }

    # ------------------------------------------------------------------

    def _event_e5(self, match_id: int, frame: LiveFrame, map_number: int,
                  map_name: str, decided_before: int) -> Event:
        series = self._series(match_id)
        return Event(
            type="E5",
            idempotency_key=f"E5:{match_id}:map:{map_number}:started:{map_name}",
            match_id=match_id,
            payload={
                **self._context(match_id, frame),
                "map_number": map_number,
                "map_name": map_name,
                "series_team": series[0],
                "series_opponent": series[1],
            },
        )

    def _event_e12(self, match_id: int, frame: LiveFrame, map_number: int,
                   map_name: str, ours: int, theirs: int) -> Optional[Event]:
        """The half, and the start of every overtime.

        Two moments where the map turns over: the sides swap after
        `regulation` rounds have been played (7-5, 6-6, 12-0 — the split does
        not matter), and a new overtime begins whenever the score is level at
        the end of the previous one: 12-12, 15-15, 18-18. The side swap inside
        an overtime is deliberately not reported — under MR3 that would be a
        message every three rounds.

        Off by default, and per subscriber (`/settings phase on`): the live
        card shows all of this already, so this is for someone who wants to be
        pulled back to the screen. Born whenever at least one person wants it;
        the queue then withholds it from the rest.
        """
        if self._threshold("phase") <= 0 or self._warming_up(frame):
            return None

        regulation, overtime = frame.regulation, frame.overtime
        if regulation < 1 or overtime < 1:
            return None

        if ours + theirs == regulation:
            key = f"E12:{match_id}:map:{map_number}:half"
            number = 0
        elif (ours == theirs and ours >= regulation
              and (ours - regulation) % overtime == 0):
            number = (ours - regulation) // overtime + 1
            key = f"E12:{match_id}:map:{map_number}:overtime:{number}"
        else:
            return None

        if key in self._announced:
            return None
        self._announced.add(key)
        log.info("match %s: %s on %s at %d:%d", match_id,
                 "half time" if not number else f"overtime {number} begins",
                 map_name, ours, theirs)
        return Event(
            type="E12",
            idempotency_key=key,
            match_id=match_id,
            payload={
                **self._context(match_id, frame),
                "map_number": map_number,
                "map_name": map_name,
                "score_team": ours,
                "score_opponent": theirs,
                "overtime": number,
            },
        )

    def _released_start_event(self, match_id: int,
                              frame: LiveFrame) -> Optional[Event]:
        """"The match has started", held back by the page until a round is played.

        The page raises its LIVE flag when the teams connect to the server, so
        it cannot tell a warmup from a game; the feed can. The message itself
        was written by the page machine and put aside — this only decides the
        moment. The key is the one the page would have used, so if the page
        gave up waiting and sent it after all, the unique index swallows this
        copy in silence.
        """
        if self._warming_up(frame):
            return None
        payload = self.storage.pending_start_event(match_id)
        if payload is None or self.storage.start_event_sent(match_id):
            return None
        self.storage.set_pending_start_event(match_id, None)
        log.info("match %s: the first round is being played, sending the start "
                 "message held back through the warmup", match_id)
        return Event(
            type="E4",
            idempotency_key=f"E4:{match_id}:started",
            match_id=match_id,
            payload=payload,
        )

    def _event_e11(self, match_id: int, frame: LiveFrame, map_number: int,
                   map_name: str, ours: int, theirs: int) -> Optional[Event]:
        """Map point: somebody is one round away from taking the map.

        The threshold is not hardcoded anywhere — it is the same
        hltv_notify.scoring that decides the map is over, so the warning cannot
        drift apart from the result it warns about. That matters most in
        overtime: every overtime moves the target three rounds up (13, then 16,
        then 19), so every one of them has its own map point, and every one of
        them is worth a warning of its own.

        Both teams get one — a map point AGAINST us is the more urgent of the
        two. Who it belongs to is not stored in the payload but read off the
        score at render time: the score is turned around for a subscriber who
        follows the opponent, and a separate "whose" field would not turn with
        it.
        """
        if self._warming_up(frame) or ours == theirs:
            return None
        if rounds_to_win(ours, theirs,
                         regulation=frame.regulation, overtime=frame.overtime) != 1:
            return None

        target = max(ours, theirs) + 1
        overtime_number = max(0, (target - 1 - frame.regulation + frame.overtime - 1)
                              // frame.overtime) if frame.overtime > 0 else 0
        # The target is in the key, so each overtime brings its own map point,
        # while the frames repeating the same score bring nothing. So is the
        # leader: at 11:12 they are one round away, at 12:12 the score is level
        # again, and at 12:11 it is us — two different warnings that must not
        # collapse into one.
        key = (f"E11:{match_id}:map:{map_number}:point"
               f":{'us' if ours > theirs else 'them'}:{target}")
        if key in self._announced:
            return None
        self._announced.add(key)
        log.info("match %s: map point on %s at %d:%d (target %d)",
                 match_id, map_name, ours, theirs, target)
        return Event(
            type="E11",
            idempotency_key=key,
            match_id=match_id,
            payload={
                **self._context(match_id, frame),
                "map_number": map_number,
                "map_name": map_name,
                "score_team": ours,
                "score_opponent": theirs,
                "round": frame.current_round,
                "overtime": overtime_number,
                "decides_match": self._would_decide(match_id, ours > theirs),
            },
        )

    def _would_decide(self, match_id: int, ours_leading: bool) -> bool:
        """Would taking this map end the whole match.

        That is the difference between "get ready" and "it is over in a
        minute", and it is what the warning is for. Symmetric between the two
        teams, so it needs no turning around at render time.
        """
        ours, theirs = self._series(match_id)
        if ours_leading:
            ours += 1
        else:
            theirs += 1
        return series_decided(ours, theirs, self.storage.best_of(match_id))

    def _event_e7(self, match_id: int, frame: LiveFrame,
                  team_id: int) -> Optional[Event]:
        """The match is over, judged by the map count.

        The page reports this too, but minutes later — it has to notice the
        status flip first. Here it is known the moment the last map ends.

        The key is the same one the page machine would produce, so if the two
        agree the unique index swallows the page's copy in silence. If they
        disagree, the key differs and the page's message goes out as a
        correction. That is deliberate: the feed gives the speed, the page stays
        the source of truth.

        With an unknown format we do not guess and stay silent — the page will
        report the end of the match as it did before.
        """
        ours, theirs = self._series(match_id)
        if not series_decided(ours, theirs, self.storage.best_of(match_id)):
            return None
        maps = [{"number": row["map_number"], "name": row["map_name"],
                 "score_team": row["score_team"], "score_opponent": row["score_opponent"],
                 "overtime": bool(row["overtime"])}
                for row in self.storage.map_results(match_id)]
        log.info("match %s: the series is decided %d-%d, reporting the finish "
                 "without waiting for the page", match_id, ours, theirs)
        return Event(
            type="E7",
            idempotency_key=f"E7:{match_id}:finished:{ours}-{theirs}",
            match_id=match_id,
            payload={
                **self._context(match_id, frame, team_id),
                "series_team": ours,
                "series_opponent": theirs,
                "won": None if ours == theirs else ours > theirs,
                "maps": maps,
                "corrected": False,
            },
        )

    def _event_e6(self, match_id: int, frame: LiveFrame, map_number: int, map_name: str,
                  ours: int, theirs: int, overtime: bool) -> Event:
        series = self._series(match_id)
        # The series score including the map just taken.
        if ours > theirs:
            series = (series[0] + 1, series[1])
        elif theirs > ours:
            series = (series[0], series[1] + 1)
        # A comeback is not a message of its own: it is one more line on the
        # map's result, where the score it talks about already is.
        comeback = self._comeback_tracker(map_name).verdict(
            ours, theirs, overtime=overtime) or {}
        if comeback:
            log.info("match %s: map %s was a comeback from %d:%d, swing %d, %s",
                     match_id, map_name, comeback["comeback_from_team"],
                     comeback["comeback_from_opponent"], comeback["comeback_swing"],
                     comeback["comeback_result"])
        return Event(
            type="E6",
            idempotency_key=f"E6:{match_id}:map:{map_number}:result:{ours}-{theirs}",
            match_id=match_id,
            payload={
                **self._context(match_id, frame),
                "map_number": map_number,
                "map_name": map_name,
                "score_team": ours,
                "score_opponent": theirs,
                "overtime": overtime,
                "series_team": series[0],
                "series_opponent": series[1],
                **comeback,
            },
        )
