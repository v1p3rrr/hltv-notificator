"""Persist observed round winners, including overtime absent from HLTV's history.

Regulation can be recovered from a scoreboard's history. Overtime is recorded
from one-round score increments; a jump over several rounds leaves question
marks. The final score alone cannot tell which order those rounds were won in.
"""

from __future__ import annotations

import json

from ..scoring import series_decided
from ..sources.scorebot import ROUND_STARTED, ROUND_WARMUP


def _key(match_id: int, map_number: int) -> str:
    return f"round_history:{match_id}:{map_number}"


def observe(storage, match_id: int, map_number: int, frame, team_id: int) -> None:
    """Update one map, writing only when its score, history or sides change."""
    if frame.round_state == ROUND_WARMUP:
        return
    ours, theirs = frame.our_score(team_id)
    if ours is None or theirs is None:
        return
    key = _key(match_id, map_number)
    previous = storage.get_meta(key)
    data = json.loads(previous) if previous else {}
    score = [ours, theirs]
    old_score = data.get("score")
    if old_score and any(new < old for new, old in zip(score, old_score)):
        # A knife round or server restart can reset the score on the same map.
        # Its abandoned rounds must not become the real map's history.
        data = {}
        old_score = None
    total = ours + theirs
    rounds = list(data.get("rounds", ""))[:total]
    rounds.extend("?" for _ in range(total - len(rounds)))
    if old_score is not None:
        delta = (ours - old_score[0], theirs - old_score[1])
        if delta in ((1, 0), (0, 1)):
            rounds[total - 1] = "W" if delta[0] else "L"
    elif total == 1:
        rounds[0] = "W" if ours else "L"

    # The source supplies only regulation history. Ignore anything beyond it,
    # and ignore a transient history that disagrees with the scoreboard score.
    history = frame.our_history(team_id)
    candidates = list(rounds)
    for outcome in history:
        if 1 <= outcome.ordinal <= min(total, 2 * frame.regulation):
            candidates[outcome.ordinal - 1] = "W" if outcome.won else "L"
    if candidates.count("W") <= ours and candidates.count("L") <= theirs:
        rounds = candidates

    sides = dict(data.get("sides", {}))
    if frame.starting_ct == team_id:
        sides.update({"0": "CT", "1": "T"})
    elif frame.starting_t == team_id:
        sides.update({"0": "T", "1": "CT"})
    if frame.round_state == ROUND_STARTED and (
            frame.current_round == 0 or frame.current_round == total + 1):
        # Read actual sides during play. At ended/freeze the arrays can already
        # have swapped. Do not guess which side an unseen overtime began on.
        phase = (0 if total < frame.regulation else 1
                 if total < 2 * frame.regulation else
                 2 + (total - 2 * frame.regulation) // frame.overtime)
        sides[str(phase)] = "CT" if frame.ct_team_id == team_id else "T"
    data.update(score=score, rounds="".join(rounds),
                regulation=frame.regulation, overtime=frame.overtime, sides=sides)
    encoded = json.dumps(data, sort_keys=True)
    if encoded != previous:
        storage.set_meta(key, encoded)


def summary(storage, match_id: int, map_number: int,
            score_team: int, score_opponent: int) -> dict:
    """Freeze a map's history in an event payload, from its canonical side."""
    raw = storage.get_meta(_key(match_id, map_number))
    if raw is None or score_team is None or score_opponent is None:
        return {}
    data = json.loads(raw)
    rounds = data["rounds"][:score_team + score_opponent]
    rounds += "?" * (score_team + score_opponent - len(rounds))
    if rounds.count("W") > score_team or rounds.count("L") > score_opponent:
        # The page corrected a result the feed reported. Keep the gaps honest.
        rounds = "?" * (score_team + score_opponent)
    return {"rounds": rounds, "regulation": data["regulation"],
            "overtime": data["overtime"], "sides": data["sides"]}


def set_picks(storage, match_id: int, picks: list) -> None:
    """Keep the latest actual veto; an unfilled page must not erase it."""
    key = f"map_picks:{match_id}"
    previous = storage.get_meta(key)
    known = {item["number"]: item for item in (json.loads(previous) if previous else [])}
    for item in picks:
        if item.get("name", "").upper() not in ("", "TBA"):
            known[item["number"]] = item
    if known:
        encoded = json.dumps([known[number] for number in sorted(known)])
        if encoded != previous:
            storage.set_meta(key, encoded)


def next_map(storage, match_id: int, map_number: int,
             series_team: int, series_opponent: int) -> dict:
    """The next map only if it is still needed, with the page's pick owner."""
    if series_decided(series_team, series_opponent, storage.best_of(match_id)):
        return {}
    raw = storage.get_meta(f"map_picks:{match_id}")
    for item in json.loads(raw) if raw else []:
        if item["number"] == map_number + 1 and item["name"].upper() not in ("", "TBA"):
            return dict(item)
    return {}
