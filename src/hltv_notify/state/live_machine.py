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
from typing import Dict, List, Optional, Sequence, Tuple

from .. import settings
from ..config import Config
from ..models import Event, MatchState
from ..scoring import map_completed, rounds_to_win, series_decided
from ..sources.scorebot import ROUND_WARMUP, KillEvent, LiveFrame
from . import bingo
from .bingo import BingoTracker
from .comeback import ComebackTracker
from .db import Storage, iso, utcnow
from .highlights import RoundTracker

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
        # A tracker per tracked team PER MAP: if tracked teams play each other,
        # a 4k by a player of either is its own highlight, and one must not be
        # muted for the sake of the other. They live in the worker's memory and
        # survive reconnects inside it.
        #
        # Keyed by the map as well as the team, exactly like `_comeback` below,
        # and for a reason worth keeping: the bars are read when the tracker is
        # built. Keyed by team alone it was built on the first frame of the
        # MATCH and kept those bars to the end of it, so `/settings clutch 2`
        # typed during a match did nothing for the rest of it — while
        # `_highlight_events` re-read the same setting on every frame and so
        # looked like it had taken effect.
        self._highlights: Dict[Tuple[int, str], RoundTracker] = {}
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
        # Maps on which an incoherent frame has already been reported. The
        # first one is a WARNING with the numbers; the hundreds that repeat
        # the same score are not worth a line each.
        self._incoherent: set = set()
        # The bingo card's bookkeeping, one per match. It holds the roster
        # mapping the feed's kills to teams, the knife-round hold and the
        # score-reset watch; the counts themselves are in the database,
        # because the summary is sent hours after the kills.
        self._bingo: Dict[int, BingoTracker] = {}
        # An ace is a five-kill round, so it is the SAME question the
        # multikill asks with the bar moved — and `RoundTracker` already
        # answers it correctly in the one place that is hard (a round is
        # credited only with what was seen inside it, because the feed skips
        # rounds). A tracker of its own per team per map, because the card's
        # five is fixed while the subscribers' multikill bar is not.
        self._aces: Dict[Tuple[int, str], RoundTracker] = {}
        # What the counts stood at when a map's score was last seen to drop to
        # 0:0 — the line a confirmed reset rolls back to. Per (match, map),
        # with the moment it was taken, because everything SENT about the map
        # before that moment has to be struck through too.
        self._bingo_cut: Dict[Tuple[int, str], Tuple[str, Dict[Tuple[int, str], int]]] = {}
        # Retractions the worker has to carry out: (match_id, map_number,
        # sent_before). Collected here rather than sent from the machine,
        # because a machine that wrote to Telegram would be a notification
        # around the state machine.
        self._retractions: List[Tuple[int, int, str]] = []
        # The map the kills arriving now belong to. A `Kill` carries no map and
        # no round of its own, so it is placed by the last frame — which is
        # also why the worker feeds frames and kills in the order the feed sent
        # them rather than all the frames first.
        self._where: Dict[int, Tuple[int, str, bool]] = {}
        # And the frame that placed them, kept for the header of an event born
        # from a kill. It has to be a REAL frame: `_context` reads the
        # opponent's name off it and falls back to `matches.opponent_name`,
        # which is the CANONICAL team's opponent — so a fabricated empty frame
        # tells a follower of the second tracked team that its opponent is
        # itself. Exactly the "FORZE — FORZE" trap `_highlights_for_team`
        # exists to avoid, reached through the back door.
        self._frames: Dict[int, LiveFrame] = {}

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

    def _tracker(self, team_id: int, map_name: str) -> RoundTracker:
        """One tracker per team per map: a new map re-reads the bars.

        Which is what makes "a threshold changed mid-map takes effect on the
        next map" true rather than merely documented.
        """
        key = (team_id, map_name)
        if key not in self._highlights:
            self._highlights[key] = RoundTracker(
                multikill=self._threshold("multikill"),
                clutch=self._threshold("clutch"))
        return self._highlights[key]

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
        if not self._coherent(match_id, frame, map_name):
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
        events.extend(self._highlight_events(match_id, frame, map_number, map_name))
        events.extend(self._bingo_events(match_id, frame, map_number, map_name,
                                         ours, theirs))
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
            # Before the summary: the last round of the map may still be
            # holding a knife kill, and the round it waits on never turns over.
            events.extend(self._closing_bingo(match_id, frame, map_number, map_name))
            events.extend(self._bingo_summary(match_id, frame,
                                              map_number=map_number, map_name=map_name))
            finished = self._event_e7(match_id, frame, team_id)
            if finished is not None:
                events.append(finished)
                self._record_bingo_win(match_id, finished)
                events.extend(self._bingo_summary(match_id, frame,
                                                  map_number=None, map_name=map_name))
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
        if not frame.coherent:
            # Already reported by apply(), which sees every frame first.
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

    def _coherent(self, match_id: int, frame: LiveFrame, map_name: str) -> bool:
        """Refuse a frame whose score the round count cannot hold.

        Whole, not partially. The frame seen live — round 1, 9:4, `ended` on
        the first non-warmup frame of a map — did not only open the card with
        a lie: apply() would have fed it to the comeback trajectory, taken the
        highlight baselines from it, tested it for a map point and a half, and
        advanced `live_map_name` so the real first round no longer looked like
        the start of the map. None of those may see it. `LiveFrame.coherent`
        has the measurement.
        """
        if frame.coherent:
            return True
        key = (match_id, map_name)
        if key not in self._incoherent:
            self._incoherent.add(key)
            log.warning("match %s: frame on %s claims %d:%d in round %d (%s) — "
                        "a score the round cannot hold, frame discarded",
                        match_id, map_name, frame.ct_score, frame.t_score,
                        frame.current_round, frame.round_state or "?")
        else:
            log.debug("match %s: incoherent frame on %s discarded (%d:%d, round %d)",
                      match_id, map_name, frame.ct_score, frame.t_score,
                      frame.current_round)
        return False

    def _highlight_events(self, match_id: int, frame: LiveFrame, map_number: int,
                          map_name: str) -> List[Event]:
        """A multikill or a clutch by a player of OUR team, so it can be clipped."""
        if self._threshold("multikill") <= 0 and self._threshold("clutch") <= 0:
            # Nobody is waiting for either. Not the same as a bar no round
            # reaches: this skips the work entirely. BOTH have to be off — one
            # person turning multikills off must not take everybody's clutches
            # with them, which a single-bar check here would do.
            return []
        # Every tracked participant of the match, not only the canonical team:
        # if tracked teams play each other, a 4k by a player of either is its
        # own highlight.
        canonical = self.storage.canonical_team(match_id) or self.config.team_id
        tracked = self.storage.match_team_ids(match_id) or [canonical]
        events: List[Event] = []
        for tracked_team in tracked:
            events.extend(self._highlights_for_team(
                match_id, frame, map_number, map_name, tracked_team))
        return events

    def _highlights_for_team(self, match_id: int, frame: LiveFrame, map_number: int,
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
        taken = self._tracker(tracked_team, map_name).observe(
            map_name, frame.current_round, frame.round_state,
            frame.our_players(tracked_team), frame.their_players(tracked_team),
            score=(ours, theirs))
        events: List[Event] = []
        # Read once for the whole frame, not per player: two highlights in one
        # round is rare but it happens, and the list is the same for both.
        #
        # The WHOLE list goes into the payload, unpicked. Which of them a
        # reader sees depends on their languages and their count, and an event
        # is born once for everybody — so the choosing belongs at render time,
        # exactly like the comeback line's threshold.
        streams = self.storage.match_streams(match_id) if taken else []
        for found in taken:
            player = found.player
            # The ROUND the highlight belongs to, never the frame's. A round
            # whose `ended` was lost is reported when the next one arrives, and
            # taking the round off the frame named the wrong one in both the
            # message and the key. Read straight off the highlight, with no
            # fallback: `Highlight` requires it, and a fallback keyed on
            # truthiness would fire on the one value it must not touch, round 0.
            # The map is the tracker's own by construction — there is one per
            # map — so `map_number` fits it.
            # A clutch takes the message over rather than adding one to it: the
            # round produced ONE moment, and the kills ride along inside E15.
            # Keeping E9's meaning intact is what lets the two be muted and
            # thresholded apart from each other.
            event_type = "E15" if found.clutch_against else "E9"
            log.info("match %s: %s took %d kills%s in round %d on %s",
                     match_id, player.nick, found.kills,
                     f" and a 1v{found.clutch_against} clutch" if found.clutch_against else "",
                     found.round_number, found.map_name)
            events.append(Event(
                type=event_type,
                # No kill count in the key. The round is reported once, so the
                # count adds nothing to identity — and it used to take some
                # away: a reconnect mid-round retakes the baseline, and the
                # smaller count that follows would have been a new key and a
                # second message about the same round.
                idempotency_key=(f"{event_type}:{match_id}:map:{map_number}"
                                 f":round:{found.round_number}:{player.steam_id}"),
                match_id=match_id,
                payload={
                    **self._context(match_id, frame, tracked_team),
                    "nick": player.nick,
                    "kills": found.kills,
                    "clutch_against": found.clutch_against,
                    "map_number": map_number,
                    "map_name": found.map_name,
                    "round": found.round_number,
                    # The score the round was played at, not the score now:
                    # they differ by a round whenever the report is late.
                    "score_team": found.score_team if found.score_team is not None else ours,
                    "score_opponent": (found.score_opponent
                                       if found.score_opponent is not None else theirs),
                    "streams": streams,
                },
            ))
        return events

    # ------------------------------------------------------------------
    # The bingo card.

    def _bingo_on(self) -> bool:
        """Is anybody keeping the card at all.

        With nobody keeping it the feed's log is not parsed, no kill is
        classified and no counter is written — which is the point of asking
        here rather than per reader: this is the only part of the service that
        reads the log, and it may cost nothing when it is off.

        One expression, in `bingo.enabled`, because the page machine and the
        card's own writers ask the same question and two spellings of it would
        let a machine count what no reader keeps.
        """
        return bingo.enabled(self.storage, self.config)

    def _bingo_tracker(self, match_id: int) -> BingoTracker:
        if match_id not in self._bingo:
            self._bingo[match_id] = BingoTracker()
        return self._bingo[match_id]

    def _ace_tracker(self, team_id: int, map_name: str) -> RoundTracker:
        key = (team_id, map_name)
        if key not in self._aces:
            # Clutches off: a clutch is its own event (E15) with its own bar,
            # and a tracker that reported them here would produce a second
            # message about a round that already has one. The ace bar is the
            # card's and does not move.
            self._aces[key] = RoundTracker(multikill=bingo.ACE_KILLS, clutch=0)
        return self._aces[key]

    def take_retractions(self) -> List[Tuple[int, int, str]]:
        """Messages a confirmed score reset has invalidated, for the worker.

        Handed over rather than acted on: writing to Telegram from the state
        machine is the one thing this project does not do, and the queue is
        where the Telegram budget is accounted for.
        """
        found, self._retractions = self._retractions, []
        return found

    def _bingo_events(self, match_id: int, frame: LiveFrame, map_number: int,
                      map_name: str, ours: int, theirs: int) -> List[Event]:
        """The card, driven by one frame.

        `in_play` is "not warming up" and deliberately NOT `frame.in_play`:
        the `live` flag only turns true once the first round has been played,
        so a card gated on it would miss the whole of round one. The knife
        round is not `warmup` either — nothing in the frame says it is — which
        is what the score-reset watch below exists for.
        """
        if not self._bingo_on():
            return []
        canonical = self.storage.canonical_team(match_id) or self.config.team_id
        tracked = self.storage.match_team_ids(match_id) or [canonical]
        tracker = self._bingo_tracker(match_id)
        in_play = not self._warming_up(frame)
        self._where[match_id] = (map_number, map_name, in_play)
        self._frames[match_id] = frame

        found, reset = tracker.observe_frame(
            map_name, frame, tracked, ours, theirs, in_play)
        if reset:
            self._rewind_bingo(match_id, map_number, map_name, tracked)
        else:
            self._watch_cut(match_id, map_number, map_name, tracked, tracker)
        if in_play:
            found.extend(self._ace_occurrences(match_id, frame, map_name, tracked))
        return self._bingo_occurrence_events(match_id, frame, map_number,
                                             map_name, found)

    def _ace_occurrences(self, match_id: int, frame: LiveFrame, map_name: str,
                         tracked: List[int]) -> List[bingo.Occurrence]:
        found: List[bingo.Occurrence] = []
        for team_id in tracked:
            ours, theirs = frame.our_score(team_id)
            if ours is None:
                continue
            for taken in self._ace_tracker(team_id, map_name).observe(
                    map_name, frame.current_round, frame.round_state,
                    frame.our_players(team_id), frame.their_players(team_id),
                    score=(ours, theirs)):
                found.append(bingo.Occurrence(
                    square="ace", team_id=team_id,
                    anchor=f"ace:{taken.round_number}:{taken.player.steam_id}",
                    nick=taken.player.nick, round_number=taken.round_number))
        return found

    def _watch_cut(self, match_id: int, map_number: int, map_name: str,
                   tracked: List[int], tracker: BingoTracker) -> None:
        """Keep a line to roll back to while a score reset is undecided.

        Taken the moment the score is first seen at 0:0 and thrown away if the
        old score comes back. It cannot be taken when the reset is CONFIRMED,
        three minutes later: by then the real map has been running and its
        kills are in the same counters, and rolling those back would punish
        the map for the warmup's sins.
        """
        key = (match_id, map_name)
        pending = tracker.reset_pending_since(map_name)
        if pending is None:
            self._bingo_cut.pop(key, None)
            return
        if key in self._bingo_cut:
            return
        self._bingo_cut[key] = (iso(utcnow()), {
            (team_id, square.key): self.storage.bingo_value(
                match_id, map_number, team_id, square.key)
            for team_id in tracked for square in bingo.SQUARES})

    def _rewind_bingo(self, match_id: int, map_number: int, map_name: str,
                      tracked: List[int]) -> None:
        """A confirmed reset: the map starts over and so does its card.

        Everything is rolled back, not just the knife kills — the rounds
        before the reset can carry an ace, a grenade kill, anything, and they
        all happened on a map the server threw away. A counted square loses
        exactly what it gained before the cut; a square that is recomputed
        from the frame's own history goes to zero, because that history has
        been emptied too and will fill back up on its own.
        """
        cut_utc, cut = self._bingo_cut.pop((match_id, map_name), (None, {}))
        for team_id in tracked:
            for square in bingo.SQUARES:
                if square.aggregate == bingo.MAX:
                    self.storage.set_bingo(match_id, map_number, team_id, square.key, 0)
                    continue
                current = self.storage.bingo_value(match_id, map_number, team_id,
                                                   square.key)
                self.storage.set_bingo(match_id, map_number, team_id, square.key,
                                       current - cut.get((team_id, square.key), 0))
        self._retractions.append((match_id, map_number, cut_utc or iso(utcnow())))
        log.warning("match %s: the score on %s reset — the bingo card for that "
                    "map rolled back and anything already sent about it retracted",
                    match_id, map_name)

    def _bingo_occurrence_events(self, match_id: int, frame: LiveFrame,
                                 map_number: int, map_name: str,
                                 found: List[bingo.Occurrence]) -> List[Event]:
        """Count what happened, and build a message for it where one is wanted.

        The counting happens whatever anybody's settings say — that is what
        the summary is built from. Only the MESSAGE is conditional, and it is
        born at the lowest bar in use like every other per-reader thing here:
        if nobody has the per-moment stream on, no event is written at all.
        """
        announce = self._threshold("bingo_live") > 0
        events: List[Event] = []
        for occurrence in found:
            square = bingo.BY_KEY[occurrence.square]
            before = bingo.totals(self.storage.bingo_rows(
                match_id, occurrence.team_id)).get(square.key, 0)
            if square.aggregate == bingo.MAX or occurrence.absolute:
                # A MAX square, or one READ off the frame's own history rather
                # than witnessed. Both carry what the square stands at for
                # this map, so both are written with MAX — the read ones
                # because the high-water mark that kept them from repeating
                # lives in memory, and a fresh worker (`reconcile`, a 403
                # cooldown, a restart) recomputes the whole value again. Added
                # there, two pistol rounds become four. See bingo.Occurrence.
                value = self.storage.raise_bingo(match_id, map_number,
                                                 occurrence.team_id, square.key,
                                                 occurrence.amount)
            else:
                value = self.storage.add_bingo(match_id, map_number,
                                               occurrence.team_id, square.key,
                                               occurrence.amount)
            total = bingo.totals(self.storage.bingo_rows(
                match_id, occurrence.team_id)).get(square.key, value)
            log.info("match %s: bingo %s for team %s — %d on %s, %d over the match",
                     match_id, square.key, occurrence.team_id, value, map_name, total)
            if not announce or not self._worth_announcing(square, before, total):
                continue
            events.append(Event(
                type="E16",
                # The anchor and nothing counted: a reconnect can recompute a
                # smaller number from a shorter history, and a key carrying
                # the count would then be a new key and a second message about
                # the same moment. Same reasoning as E9's key.
                idempotency_key=(f"E16:{match_id}:map:{map_number}"
                                 f":{occurrence.team_id}:{occurrence.anchor}"),
                match_id=match_id,
                payload={
                    **self._context(match_id, frame, occurrence.team_id),
                    "square": square.key,
                    "label": square.label,
                    "moment": square.moment,
                    "nick": occurrence.nick,
                    "weapon": occurrence.weapon,
                    "round": occurrence.round_number,
                    "map_number": map_number,
                    "map_name": map_name,
                    "count": total,
                    "target": square.target,
                    "closed": square.closed(total),
                },
            ))
        return events

    @staticmethod
    def _worth_announcing(square: bingo.Square, before: int, total: int) -> bool:
        """Is this occurrence a moment, or only a number moving.

        A counted square is a moment every time: every kill through smoke is
        its own thing that happened, and the card's reader asked for each of
        them.

        A square whose match answer is the BEST map's is not. "Two rounds in a
        row" is not news on the way to four, it is the same run still going —
        and neither is the fifth, which is the same run having already said
        what it had to say. So it speaks exactly once, on the step that takes
        the MATCH from open to closed. The match and not the map, or a second
        map with four in a row would announce a square that was ticked an hour
        ago.
        """
        if square.aggregate == bingo.MAX:
            return square.closed(total) and not square.closed(before)
        return True

    def _closing_bingo(self, match_id: int, frame: LiveFrame, map_number: int,
                       map_name: str) -> List[Event]:
        """Whatever the map's last round was still holding."""
        if not self._bingo_on():
            return []
        found = self._bingo_tracker(match_id).close_map(map_name)
        if not found:
            return []
        return self._bingo_occurrence_events(match_id, frame, map_number,
                                             map_name, found)

    def _record_bingo_win(self, match_id: int, finished: Event) -> None:
        """Tick "win the match" for whoever took it.

        The writing itself is shared with the page machine — see
        `bingo.record_win` — because both machines can be the one that reaches
        the end of the match first, and whichever does has to leave the card
        complete before the summary is built from it.
        """
        if not self._bingo_on():
            return
        bingo.record_win(self.storage, self.config, match_id, finished.payload)

    def _bingo_summary(self, match_id: int, frame: LiveFrame, *,
                       map_number: Optional[int], map_name: str) -> List[Event]:
        """The card after a map (E17) and after the match (E18).

        One per tracked team, each from that team's own side, for the reason
        every other event here is: a card built once for the match and shown
        to both sides credits a subscriber with the opponent's kills, and
        `format.orient` cannot turn a count around the way it turns a score.
        The building itself is shared with the page machine — see
        `bingo.summary_events`.
        """
        return bingo.summary_events(
            self.storage, self.config, match_id,
            context_for=lambda team_id: self._context(match_id, frame, team_id),
            map_number=map_number, map_name=map_name)

    def observe_kills(self, match_id: int, kills: Sequence[KillEvent]) -> List[Event]:
        """Kills out of the feed's log, in arrival order.

        The whole defence against the replayed backlog is here, and it is two
        rules. A match with no watermark is SEEDED — the first batch after
        connecting carries the series from its first map with nothing saying
        which kill belongs to which, so it sets the mark and counts none of
        it. After that, only kills past the mark are new, because `eventId`
        rises with time.
        """
        if not kills or not self._bingo_on():
            return []
        watermark = self.storage.bingo_watermark(match_id)
        newest = max(kill.event_id for kill in kills)
        if watermark is None:
            self.storage.set_bingo_watermark(match_id, newest)
            log.info("match %s: the feed's backlog of %d kills is the state "
                     "before we connected — recorded as seen, not counted",
                     match_id, len(kills))
            return []

        fresh = [kill for kill in kills if kill.event_id > watermark]
        self.storage.set_bingo_watermark(match_id, newest)
        if not fresh:
            return []
        where = self._where.get(match_id)
        if where is None:
            # No frame has been seen yet, so there is no map to put these on.
            # Dropping is the only honest read: placing them on the map the
            # next frame happens to show is how a kill from map one ends up
            # counted on map two.
            log.debug("match %s: %d kills arrived before any frame — dropped",
                      match_id, len(fresh))
            return []
        map_number, map_name, in_play = where
        canonical = self.storage.canonical_team(match_id) or self.config.team_id
        tracked = self.storage.match_team_ids(match_id) or [canonical]
        found = self._bingo_tracker(match_id).observe_kills(
            map_name, fresh, tracked, in_play)
        if not found:
            return []
        return self._bingo_occurrence_events(match_id, self._last_frame(match_id),
                                             map_number, map_name, found)

    def _last_frame(self, match_id: int) -> LiveFrame:
        """The frame that placed these kills, for the event's header.

        The real one, not a fabricated empty one. `_context` asks the frame
        for the opponent's NAME and falls back to `matches.opponent_name` when
        it cannot answer — and that row holds the opponent of the CANONICAL
        team, so for the second tracked team of a match the fallback names
        that team itself ("FORZE — FORZE"). Only a frame carries both ids and
        can be asked per side.

        The empty frame remains the last resort: `observe_kills` already drops
        kills that arrived before any frame, so this is reachable only if the
        card was switched on between the frame and the kills, and an empty
        header beats raising inside the feed loop.
        """
        frame = self._frames.get(match_id)
        if frame is not None:
            return frame
        return LiveFrame(map_name="", current_round=0, round_state="", live=False,
                         ct_team_id=None, ct_team_name="", ct_score=0,
                         t_team_id=None, t_team_name="", t_score=0,
                         regulation=12, overtime=3)

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
        """The half (E12) and the start of every overtime (E13).

        Two moments where the map turns over: the sides swap after
        `regulation` rounds have been played (7-5, 6-6, 12-0 — the split does
        not matter), and a new overtime begins whenever the score is level at
        the end of the previous one: 12-12, 15-15, 18-18. The side swap inside
        an overtime is deliberately not reported — under MR3 that would be a
        message every three rounds.

        Two separate types, switched on separately (`/settings half`,
        `/settings overtime`, and `/mute <team> E12,E13`). They are not the
        same kind of thing: a half comes on every map and is routine, an
        overtime usually does not come at all, and someone who wants to be
        pulled back to the screen for the second rarely wants the first.

        Both off by default: the live card shows all of this already. Born
        whenever at least one person wants that type; the queue then withholds
        it from the rest.

        An overtime carries the broadcasts, like a highlight does: it is a
        "come back to the screen" moment. A half is not — it is routine, and
        nobody needs a link to watch a break.
        """
        if self._warming_up(frame):
            return None

        regulation, overtime = frame.regulation, frame.overtime
        if regulation < 1 or overtime < 1:
            return None

        if ours + theirs == regulation:
            event_type, number = "E12", 0
            key = f"E12:{match_id}:map:{map_number}:half"
        elif (ours == theirs and ours >= regulation
              and (ours - regulation) % overtime == 0):
            number = (ours - regulation) // overtime + 1
            event_type = "E13"
            # The key keeps the shape it had under E12, so the migration that
            # renames it only has to change the prefix.
            key = f"E13:{match_id}:map:{map_number}:overtime:{number}"
        else:
            return None

        # Checked per type rather than once at the top: the two are switched on
        # separately, and the half being off must not silence the overtime.
        if self._threshold("half" if event_type == "E12" else "overtime") <= 0:
            return None

        if key in self._announced:
            return None
        self._announced.add(key)
        log.info("match %s: %s on %s at %d:%d", match_id,
                 "half time" if not number else f"overtime {number} begins",
                 map_name, ours, theirs)
        return Event(
            type=event_type,
            idempotency_key=key,
            match_id=match_id,
            payload={
                **self._context(match_id, frame),
                "map_number": map_number,
                "map_name": map_name,
                "score_team": ours,
                "score_opponent": theirs,
                "overtime": number,
                "streams": self.storage.match_streams(match_id) if number else [],
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

        The broadcasts ride along as they do on a highlight: the point of the
        warning is to be watching when the round is played, and the thing to
        tap should be in the message. The whole list, unpicked — which ones a
        reader sees is decided at render time from their own settings.
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
                "streams": self.storage.match_streams(match_id),
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
