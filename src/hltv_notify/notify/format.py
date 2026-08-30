"""Message rendering.

Every message stands on its own: which team, which opponent, which event and
which map are all clear from it without scrolling back through the chat. Times
are in the reader's timezone; everything is stored in UTC and converted only
here. Month names are spelled out locally so the container's locale cannot
change them.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from ..models import Event

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def to_local(value, tz_name: str) -> datetime:
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(tz_name))


def human_time(value, tz_name: str, *, with_date: bool = True) -> str:
    dt = to_local(value, tz_name)
    clock = f"{dt.hour:02d}:{dt.minute:02d}"
    if not with_date:
        return clock
    return f"{WEEKDAYS[dt.weekday()]} {dt.day} {MONTHS[dt.month - 1]}, {clock}"


def escape(text: Optional[str]) -> str:
    return html.escape(text or "", quote=False)


_esc = escape


def _link(url: str, title: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{_esc(title)}</a>'


SWAPPED_PAIRS = (
    ("team_name", "opponent"),
    ("team_id", "opponent_id"),
    ("score_team", "score_opponent"),
    ("series_team", "series_opponent"),
)


def orient(payload: dict, for_team_id: Optional[int]) -> dict:
    """Turn the event around to face the recipient's team.

    The score in an event is oriented on the match's canonical team. If the
    subscriber follows its opponent, they must be shown the mirrored score —
    otherwise they read "13:10" where for them it is "10:13".
    """
    if for_team_id is None or payload.get("team_id") in (None, for_team_id):
        return payload
    if payload.get("opponent_id") != for_team_id:
        return payload

    flipped = dict(payload)
    for left, right in SWAPPED_PAIRS:
        if left in payload or right in payload:
            flipped[left], flipped[right] = payload.get(right), payload.get(left)
    if "won" in payload and payload["won"] is not None:
        flipped["won"] = not payload["won"]
    if payload.get("picks"):
        # The pick turns around too: "ours" and "theirs" swap places.
        swap = {"team": "opponent", "opponent": "team", "decider": "decider"}
        flipped["picks"] = [{**item, "pick": swap.get(item.get("pick"), item.get("pick"))}
                            for item in payload["picks"]]
    if payload.get("maps"):
        flipped["maps"] = [
            {**item,
             "score_team": item.get("score_opponent"),
             "score_opponent": item.get("score_team")}
            for item in payload["maps"]
        ]
    return flipped


def _minutes(value) -> str:
    """15 -> "15 min", 60 -> "1 h"."""
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return str(value)
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h" if rest == 0 else f"{hours} h {rest} min"


def render(event: Event, *, team_name: str, tz_name: str,
           for_team_id: Optional[int] = None) -> str:
    """Event -> finished text in Telegram's HTML markup."""
    payload = orient(event.payload, for_team_id)
    opponent = _esc(payload.get("opponent") or "TBD")
    event_name = _esc(payload.get("event_name"))
    url = payload.get("url") or ""
    # The team name comes from the event: there can be several tracked teams,
    # each match having its own. The config value is only a fallback.
    team = _esc(payload.get("team_name") or team_name)

    if event.type == "E1":
        when = human_time(payload["start_utc"], tz_name)
        lines = ["🆕 <b>New match</b>", f"{team} — {opponent}"]
        if payload.get("placeholder"):
            lines.append("<i>opponent not decided yet</i>")
        lines += [event_name, f"🕒 {when}", _link(url, "Match page")]
        return "\n".join(lines)

    if event.type == "E2":
        was = human_time(payload["old_start_utc"], tz_name)
        now = human_time(payload["start_utc"], tz_name)
        return "\n".join([
            "🕐 <b>Match time changed</b>",
            f"{team} — {opponent}",
            event_name,
            f"Was: {was}",
            f"Now: {now}",
            _link(url, "Match page"),
        ])

    if event.type == "E3":
        when = human_time(payload["start_utc"], tz_name)
        return "\n".join([
            "❌ <b>Match cancelled or postponed</b>",
            f"{team} — {opponent}",
            event_name,
            f"Was scheduled for: {when}",
            _link(url, "Match page"),
        ])

    if event.type == "E10":
        when = human_time(payload["start_utc"], tz_name)
        left = payload.get("minutes_left") or payload.get("minutes_before")
        return "\n".join([
            f"⏰ <b>Match in {_esc(_minutes(left))}</b>",
            f"{team} — {opponent}",
            event_name,
            f"🕒 {when}",
            _link(url, "Match page"),
        ])

    if event.type == "E4":
        best_of = payload.get("best_of")
        suffix = f" · BO{best_of}" if best_of else ""
        lines = [
            "🔴 <b>Match started</b>",
            f"{team} — {opponent}",
            f"{event_name}{suffix}",
        ]
        picks = payload.get("picks") or []
        if picks:
            # A code block: Telegram shows a copy button on it, so the map
            # lineup can be grabbed with a single tap.
            width = max(len(item["name"]) for item in picks)
            rows = "\n".join(
                f"{item['name']:<{width}}  {PICK_LABELS.get(item['pick'], '')}"
                for item in picks)
            lines.append(f"<pre>{_esc(rows)}</pre>")
        lines.append(_link(url, "Match page"))
        return "\n".join(lines)

    if event.type == "E5":
        return "\n".join([
            "🗺 <b>Map started</b>",
            f"{team} — {opponent}",
            f"Map {payload.get('map_number')}: <b>{_esc(payload.get('map_name'))}</b>",
            f"Series score: {payload.get('series_team')}:{payload.get('series_opponent')}",
            event_name,
            _link(url, "Match page"),
        ])

    if event.type == "E11":
        ours = payload.get("score_team") or 0
        theirs = payload.get("score_opponent") or 0
        # Whose map point it is follows from the score, and the score has
        # already been turned around for this recipient. A stored "whose"
        # would not have turned with it.
        ours_leading = ours > theirs
        icon = "🏁" if ours_leading else "🚨"
        leader = team if ours_leading else opponent
        overtime = payload.get("overtime") or 0
        where = f" (overtime {overtime})" if overtime else ""
        return "\n".join([
            f"{icon} <b>Map point — {leader}</b>",
            f"{_esc(payload.get('map_name'))} — <b>{ours}:{theirs}</b>{where}",
            f"{team} — {opponent}",
            ("One round from taking the map — and the match"
             if payload.get("decides_match") else "One round from taking the map"),
            _link(url, "Watch the match"),
        ])

    if event.type == "E6":
        ours = payload.get("score_team")
        theirs = payload.get("score_opponent")
        icon = "✅" if (ours or 0) > (theirs or 0) else "❌"
        overtime = " (overtime)" if payload.get("overtime") else ""
        return "\n".join([
            f"{icon} <b>Map {payload.get('map_number')} finished</b>",
            f"{_esc(payload.get('map_name'))} — <b>{ours}:{theirs}</b>{overtime}",
            f"{team} — {opponent}",
            f"Series score: <b>{payload.get('series_team')}:{payload.get('series_opponent')}</b>",
            event_name,
            _link(url, "Match page"),
        ])

    if event.type == "E7":
        ours = payload.get("series_team", 0)
        theirs = payload.get("series_opponent", 0)
        won = payload.get("won")
        icon = "🤝" if won is None else ("🏆" if won else "💀")
        # A correction: the live feed already reported the match as finished
        # from the map count, and the page then disagreed about the score. Say
        # so, otherwise the second message just looks like a duplicate.
        headline = ("Match finished — corrected" if payload.get("corrected")
                    else "Match finished")
        lines = [
            f"{icon} <b>{headline}</b>",
            f"<b>{team} {ours}:{theirs} {opponent}</b>",
            event_name,
        ]
        for item in payload.get("maps", []):
            overtime = " (OT)" if item.get("overtime") else ""
            lines.append(f"   {_esc(item['name'])} — "
                         f"{item['score_team']}:{item['score_opponent']}{overtime}")
        lines.append(_link(url, "Match page"))
        return "\n".join(lines)

    if event.type == "E9":
        kills = payload.get("kills", 0)
        icon = "🔥" if kills < 5 else "💥"
        headline = "ACE" if kills >= 5 else f"{kills}k round"
        return "\n".join([
            f"{icon} <b>{_esc(payload.get('nick'))} — {headline}</b>",
            f"{_esc(payload.get('map_name'))}, round {payload.get('round')} · "
            f"score {payload.get('score_team')}:{payload.get('score_opponent')}",
            f"{team} — {opponent}",
            _link(url, "Watch the match"),
        ])

    if event.type == "E8R":
        return "\n".join([
            "✅ <b>Recovered</b>",
            _esc(payload.get("reason")),
            f"<i>{_esc(payload.get('detail'))}</i>",
        ])

    if event.type == "E8":
        lines = [
            "⚠️ <b>Service degraded</b>",
            _esc(payload.get("reason")),
            f"<i>{_esc(payload.get('detail'))}</i>",
        ]
        # A link to the match if the alert is about a specific one, so you can
        # go and look with your own eyes instead of hunting for it.
        if url:
            lines.append(_link(url, "Match page"))
        return "\n".join(lines)

    return f"{_esc(event.type)}: {_esc(str(payload))}"


PICK_LABELS = {
    "team": "our pick",
    "opponent": "their pick",
    "decider": "decider",
}

ROUND_STATE_LABELS = {
    "warmup": "warmup",
    "freezePeriod": "freeze time",
    "started": "round live",
    "ended": "round over",
}


def render_live(snapshot: dict, *, team_name: str,
                announces_start: bool = False) -> str:
    """The live message for one map, updated as the game goes on.

    It is deliberately short: it is redrawn every few seconds, and a long text
    turns the chat history into a wall.

    `announces_start` turns this message into the map's card: it then also
    carries what E5 used to say on its own, and no separate "map started"
    message is sent. The two used to be separate, and the live message always
    won the race to the chat — it goes straight to Telegram while events wait
    in the queue, so "the map has started" landed seconds AFTER the score for
    that map. The heading is written to read correctly both at 0:0 in round 1
    and at 13:5 in round 18.
    """
    team = escape(snapshot.get("team_name") or team_name)
    opponent = escape(snapshot.get("opponent") or "TBD")
    map_name = escape(snapshot.get("map_name"))
    state = ROUND_STATE_LABELS.get(snapshot.get("round_state"), "")
    tail = f" · {state}" if state else ""
    score = f"{snapshot['score_team']}:{snapshot['score_opponent']}"
    if announces_start:
        lines = [
            f"🗺 <b>Map {snapshot.get('map_number')}: {map_name}</b>",
            f"{team} <b>{score}</b> {opponent} · round {snapshot.get('round')}{tail}",
        ]
    else:
        lines = [
            f"🎯 <b>{team} {score} {opponent}</b>",
            f"Map {snapshot.get('map_number')}: {map_name} · round {snapshot.get('round')}{tail}",
        ]
    lines.append(
        f"Series score: {snapshot.get('series_team')}:{snapshot.get('series_opponent')}")
    if announces_start and snapshot.get("event_name"):
        lines.append(escape(snapshot["event_name"]))
    lines.append(_link(snapshot.get("url") or "", "Match page"))
    return "\n".join(lines)
