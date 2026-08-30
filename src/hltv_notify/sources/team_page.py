"""Parser for the team page — the schedule source.

Why this page and not /matches?team=<id>: the latter is disallowed by
robots.txt (see docs/recon/R5), while the same data sits here in the allowed
area.

Markup (verified against a saved fixture, docs/recon/R3):
    table.match-table
      tr.event-header-cell         tournament name, in force until the next one
      tr.team-row
        td.date-cell span[data-unix]      start time, epoch in MILLIseconds
        a.team-name.team-1 / .team-2      both teams, href /team/<id>/<slug>
        .score-cell .score  x2            "-" before the game, numbers after
        a.matchpage-button                an upcoming match
        a.stats-button                    a played match
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

from ..config import HLTV_BASE
from ..models import ScheduleEntry

log = logging.getLogger(__name__)

MATCH_ID_RE = re.compile(r"/matches/(\d+)/")
TEAM_ID_RE = re.compile(r"/team/(\d+)/")
# The tail of a match address. The character set is deliberately narrow: no
# slash, no dot, no at-sign — meaning there is no way out of a single path
# segment.
MATCH_SLUG_RE = re.compile(r"/matches/\d+/([A-Za-z0-9_-]{1,120})")


def match_url(match_id: int, href: str = "") -> str:
    """The address of a match page.

    Built FROM A VALIDATED NUMBER rather than by concatenating the base with
    whatever came off the page. The concatenation was a hole: `HLTV_BASE` does
    not end in a slash, so an href like `@10.0.0.1:8080/matches/1/x` produced
    `https://www.hltv.org@10.0.0.1:8080/matches/1/x`, where `www.hltv.org` is
    userinfo and the request went to `10.0.0.1`. Verified: libcurl goes exactly
    there. The variant `.evil.example/matches/1/x` did not even need the
    at-sign.

    The tail is taken from the href only for readability, and only if it
    consists of harmless characters; HLTV does not look at the tail itself.
    """
    found = MATCH_SLUG_RE.search(href or "")
    return f"{HLTV_BASE}/matches/{match_id}/{found.group(1) if found else '-'}"


class ParseError(RuntimeError):
    """The markup is not what was expected. Treated as a source failure rather
    than as "no matches" — otherwise a redesign would look like an empty
    schedule."""


def _team_ref(anchor) -> Tuple[Optional[int], str]:
    """The team id and name out of a link. A placeholder ("Winner of match X")
    has no link, so the id stays None — the match is still tracked."""
    if anchor is None:
        return None, ""
    name = anchor.get_text(strip=True)
    href = anchor.get("href") or ""
    found = TEAM_ID_RE.search(href)
    return (int(found.group(1)) if found else None), name


def _score_pair(row) -> Tuple[Optional[int], Optional[int]]:
    cells = row.select(".score-cell .score")
    if len(cells) < 2:
        return None, None
    values: List[Optional[int]] = []
    for cell in cells[:2]:
        text = cell.get_text(strip=True)
        values.append(int(text) if text.isdigit() else None)
    return values[0], values[1]


def parse(html: str, team_id: int) -> List[ScheduleEntry]:
    """Every match of the team from the page: upcoming and played alike."""
    soup = BeautifulSoup(html, "lxml")
    tables = soup.select("table.match-table")
    if not tables:
        raise ParseError("no table.match-table on the team page")

    entries: List[ScheduleEntry] = []
    for table in tables:
        event_name = ""
        for row in table.select("tr"):
            classes = row.get("class") or []
            if "event-header-cell" in classes:
                event_name = row.get_text(strip=True)
                continue
            if "team-row" not in classes:
                continue

            link = row.select_one('a[href*="/matches/"]')
            if link is None:
                continue
            found = MATCH_ID_RE.search(link["href"])
            if found is None:
                continue
            match_id = int(found.group(1))

            time_el = row.select_one("[data-unix]")
            if time_el is None:
                log.warning("match %s has no data-unix, skipping", match_id)
                continue
            start_utc = datetime.fromtimestamp(int(time_el["data-unix"]) / 1000, tz=timezone.utc)

            first_id, first_name = _team_ref(row.select_one("a.team-name.team-1"))
            second_id, second_name = _team_ref(row.select_one("a.team-name.team-2"))
            score_first, score_second = _score_pair(row)

            # Our team comes first on its own page, but we will not rely on the
            # ordering: match by id instead.
            if first_id == team_id:
                opponent_id, opponent_name = second_id, second_name
                score_team, score_opponent = score_first, score_second
            elif second_id == team_id:
                opponent_id, opponent_name = first_id, first_name
                score_team, score_opponent = score_second, score_first
            else:
                log.debug("match %s does not involve our team, skipping", match_id)
                continue

            entries.append(ScheduleEntry(
                match_id=match_id,
                start_utc=start_utc,
                opponent_id=opponent_id,
                opponent_name=opponent_name or "TBD",
                event_name=event_name,
                url=match_url(match_id, link["href"]),
                finished=score_team is not None and score_opponent is not None,
                score_team=score_team,
                score_opponent=score_opponent,
            ))

    if not entries:
        raise ParseError("not a single match row parsed — looks like a redesign")
    return entries


def parse_team_name(html: str) -> Optional[str]:
    """The canonical team name from the page.

    Needed when a team is added through the bot: a name derived from the slug
    differs from what HLTV shows ("forze-reload" -> "Forze Reload" instead of
    "FORZE Reload"), and the name goes into every notification.
    """
    soup = BeautifulSoup(html, "lxml")
    heading = soup.select_one(".profile-team-name") or soup.select_one("h1")
    name = heading.get_text(strip=True) if heading else ""
    return name or None


def upcoming(entries: List[ScheduleEntry]) -> List[ScheduleEntry]:
    return [e for e in entries if not e.finished]


def snapshot_of(entry: ScheduleEntry) -> Dict[str, Any]:
    """A snapshot of the meaningful fields. Nothing that changes on its own
    (response time and the like) may end up in the hash — that would break
    deduplication."""
    return {
        "match_id": entry.match_id,
        "start_utc": entry.start_utc.isoformat(),
        "opponent_id": entry.opponent_id,
        "opponent_name": entry.opponent_name,
        "event_name": entry.event_name,
        "finished": entry.finished,
        "score_team": entry.score_team,
        "score_opponent": entry.score_opponent,
    }


def hash_of(snapshot: Dict[str, Any]) -> str:
    payload = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
