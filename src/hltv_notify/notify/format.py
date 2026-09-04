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

from .. import streams as st
from ..models import Event
from ..streams import StreamPreference

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
    # The comeback's start and peak. They turn around with everything else,
    # and whose comeback it was is read back off them at render time — a
    # stored "whose" would not have turned.
    ("comeback_from_team", "comeback_from_opponent"),
    ("comeback_to_team", "comeback_to_opponent"),
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


def comeback_line(payload: dict, *, team: str, opponent: str,
                  threshold: Optional[int] = None) -> str:
    """The comeback line on a finished map, or "" when there was none.

    Whose comeback it was is worked out from the score, not taken from the
    payload: the score has already been turned around for this recipient, and
    a stored "whose" would still be pointing the other way. The team behind at
    the low point is the one that came back — and the deficit is written from
    THAT team's side, so "Color came back from 2:10" reads the way a person
    would say it.

    `threshold` is the RECIPIENT's bar. The swing was measured against the
    lowest bar anybody set, because the map is watched once and E6 is one
    event; deciding whether it is worth saying is per person and therefore
    happens here, where the message is already being written for one reader.
    None means no bar — the payload speaks for itself.
    """
    if "comeback_swing" not in payload:
        return ""
    low = (payload.get("comeback_from_team") or 0,
           payload.get("comeback_from_opponent") or 0)
    ours = low[0] < low[1]
    who = team if ours else opponent
    # Every score in the line is written from the comeback team's own side.
    behind, ahead = low if ours else low[::-1]
    if threshold is not None:
        # The deficit floor is derived from the bar rather than being a second
        # setting (see state/comeback.py), so it has to move with the reader's
        # bar too — otherwise someone asking for 12 would still be told about
        # a 13:1 win "from 0:1".
        if threshold <= 0 or int(payload.get("comeback_swing") or 0) < threshold:
            return ""
        if ahead - behind < max(2, threshold // 2):
            return ""
    overtime = payload.get("comeback_overtime")

    if payload.get("comeback_result") == "won":
        through = " through overtime" if overtime else ""
        return (f"🔥 <b>Comeback</b>: {who} turned {behind}:{ahead} around"
                f"{through} — a swing of {payload.get('comeback_swing')} rounds")

    peak = (payload.get("comeback_to_team") or 0,
            payload.get("comeback_to_opponent") or 0)
    peak = peak if ours else peak[::-1]
    reached = "to force overtime" if overtime else f"to {peak[0]}:{peak[1]}"
    return (f"🧱 <b>Comeback denied</b>: {who} came back from {behind}:{ahead} "
            f"{reached} and still lost the map")


def stream_block(streams, prefs: Optional[StreamPreference]) -> str:
    """The broadcasts worth a tap, as a quoted block. "" when there are none.

    A quote and not plain lines: it is a sidebar to the message rather than
    part of what happened, and Telegram lets it be collapsed.
    """
    if prefs is None or not streams:
        # None is "this reader switched the block off". An empty list is a
        # match whose page has not been read yet, or one nobody is casting.
        return ""
    chosen = st.pick(streams, primary=prefs.languages, limit=prefs.limit,
                     aliases=prefs.aliases)
    if not chosen:
        return ""
    lines = []
    for one in chosen:
        icon = st.PROVIDER_ICON.get(one.get("provider") or "", "")
        # The caster's name is the anchor text, so the line reads as a person
        # rather than as a URL. `_link` escapes both halves — the name comes
        # off a web page and goes into HTML.
        lines.append(f"{icon} {st.flag_emoji(one.get('flag'))} "
                     f"{_link(one.get('url') or '', one.get('name') or 'stream')}")
    return "<blockquote>" + "\n".join(lines) + "</blockquote>"


def render(event: Event, *, team_name: str, tz_name: str,
           for_team_id: Optional[int] = None,
           comeback_threshold: Optional[int] = None,
           stream_prefs: Optional[StreamPreference] = None) -> str:
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

    if event.type in ("E12", "E13"):
        # Which of the two it is comes from the payload, not from the type:
        # the number is what the sentence needs anyway, and reading it twice
        # would be two places to keep in step.
        overtime = payload.get("overtime") or 0
        headline = f"Overtime {overtime} begins" if overtime else "Half time"
        # The round count is taken from the score, not written down: the half
        # is at 12 rounds under MR12 and somewhere else under any other format.
        played = (payload.get("score_team") or 0) + (payload.get("score_opponent") or 0)
        note = ("Level after the previous one" if overtime
                else f"{played} rounds played, sides swap")
        return "\n".join([
            f"{'🕗' if overtime else '🔄'} <b>{headline}</b>",
            f"{_esc(payload.get('map_name'))} — "
            f"<b>{payload.get('score_team')}:{payload.get('score_opponent')}</b>",
            f"{team} — {opponent}",
            note,
            _link(url, "Watch the match"),
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
        lines = [
            f"{icon} <b>Map {payload.get('map_number')} finished</b>",
            f"{_esc(payload.get('map_name'))} — <b>{ours}:{theirs}</b>{overtime}",
            f"{team} — {opponent}",
            f"Series score: <b>{payload.get('series_team')}:{payload.get('series_opponent')}</b>",
        ]
        comeback = comeback_line(payload, team=team, opponent=opponent,
                                 threshold=comeback_threshold)
        if comeback:
            lines.append(comeback)
        lines += [event_name, _link(url, "Match page")]
        return "\n".join(lines)

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
        lines = [
            f"{icon} <b>{_esc(payload.get('nick'))} — {headline}</b>",
            f"{_esc(payload.get('map_name'))}, round {payload.get('round')} · "
            f"score {payload.get('score_team')}:{payload.get('score_opponent')}",
            f"{team} — {opponent}",
        ]
        # Above the match link and not below it: the whole point is to be on a
        # broadcast within seconds, and the thing to tap should be the thing
        # the eye lands on.
        block = stream_block(payload.get("streams"), stream_prefs)
        if block:
            lines.append(block)
        lines.append(_link(url, "Watch the match"))
        return "\n".join(lines)

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
