"""Parser for the match page: state, format, per-map scores.

Markup (verified against three fixtures — upcoming, live, finished):
    .timeAndEvent [data-unix]      start time. THE SCOPE IS MANDATORY: without
                                   it the first [data-unix] in the DOM belongs
                                   to the .fbw-vp-header-time widget, which
                                   carries OTHER matches' times
    .countdown                     "4h : 57m : 28s" / "LIVE" / "Match over"
    .timeAndEvent .event a         tournament
    .preformatted-text             "Best of 3 (Online)" and notes
    .team1-gradient / .team2-gradient
        a[href*="/team/"]          team id
        .teamName                  name
        .won / .lost               the SERIES score, but only once the match
                                   is finished
    .mapholder
        .mapname                   map name, "TBA" before the veto
        .results-left.pick         the side that picked the map (the decider
                                   has none)
        .results-team-score x2     the map score — but on a RUNNING map this is
                                   the current score, not the final one
        .results-center-half-score "( 5 : 7 ; 8 : 3 )"
        .results-stats             link to the map statistics. It appears
                                   exactly when the map ends

The map-completion rule (D7). The naive "there is a numeric score" is wrong:
a running map has a numeric score too. Observed on live match 2397091: while
Mirage was being played the score read 5:7 and there was no statistics link;
the moment the map ended the score became 11:13 and `.results-stats` appeared.
The `won`/`lost` classes do not help — on a running map HLTV puts them on the
current leader. Round arithmetic is no good either: it breaks on overtimes,
forfeits and non-standard formats.

The signal is the appearance of the map statistics record — HLTV creates it
when the map has been played. For a finished match the signal is backed up by
the page status: a forfeit may have no statistics at all.

The series score is not shown on the page during play and is computed from the
finished maps.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from urllib.parse import urlparse

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

TEAM_ID_RE = re.compile(r"/team/(\d+)/")
EVENT_ID_RE = re.compile(r"/events/(\d+)/")
BEST_OF_RE = re.compile(r"best of (\d+)", re.IGNORECASE)

STATUS_UPCOMING = "upcoming"
STATUS_LIVE = "live"
STATUS_OVER = "over"
STATUS_UNKNOWN = "unknown"


class ParseError(RuntimeError):
    """The markup is not what was expected — a source failure, not "no data"."""


@dataclass(frozen=True)
class MapLine:
    number: int
    name: str
    score_left: Optional[int]
    score_right: Optional[int]
    halves: Optional[str]
    has_stats: bool = False
    # Whose pick this is: "left"/"right" by page side, None for the decider,
    # the map left after the veto that nobody picked.
    picked_by: Optional[str] = None

    @property
    def has_score(self) -> bool:
        """The map has a numeric score.

        CAREFUL: that alone is not enough to call the map played. A running map
        has a numeric score too, it is simply the current one. For the
        completion signal see MatchObservation.is_final().
        """
        return self.score_left is not None and self.score_right is not None


@dataclass(frozen=True)
class StreamLink:
    """One broadcast of the match, as listed on the page.

    `flag` is the two-letter code off the flag image (`RU`, `AU`) or `WORLD`.
    HLTV uses a country to mean a language, so that is what it is read as.
    """

    provider: str          # "twitch" | "kick"
    name: str              # the caster, as shown
    flag: str              # "RU", "WORLD", ...
    viewers: int
    url: str


@dataclass(frozen=True)
class MatchObservation:
    match_id: int
    status: str
    start_utc: Optional[datetime]
    event_name: str
    event_id: Optional[int]
    best_of: Optional[int]
    team1_id: Optional[int]
    team1_name: str
    team2_id: Optional[int]
    team2_name: str
    maps: List[MapLine]
    scorebot_id: Optional[int]
    # The map format from the page: how many rounds are in a half of regulation
    # and of overtime. Needed to work out "how many rounds are left to win".
    max_rounds_regulation: Optional[int] = None
    max_rounds_overtime: Optional[int] = None
    # Broadcasts, most watched first. Empty is normal: before the veto, and on
    # a match nobody is casting.
    streams: Tuple[StreamLink, ...] = ()

    # ------------------------------------------------------------------

    def our_side(self, team_id: int) -> Optional[str]:
        if self.team1_id == team_id:
            return "left"
        if self.team2_id == team_id:
            return "right"
        return None

    def opponent(self, team_id: int) -> Tuple[Optional[int], str]:
        side = self.our_side(team_id)
        if side == "left":
            return self.team2_id, self.team2_name
        if side == "right":
            return self.team1_id, self.team1_name
        return None, ""

    def is_final(self, line: MapLine) -> bool:
        """The map is played: there is a score AND a statistics record.

        The `over` status serves as the backup signal: if the match is
        finished, everything with a score has been played — including maps
        given away by forfeit, which may have no statistics.
        """
        if not line.has_score:
            return False
        return line.has_stats or self.status == STATUS_OVER

    def final_maps(self) -> List[MapLine]:
        return [m for m in self.maps if self.is_final(m)]

    def picks(self, team_id: int) -> List[dict]:
        """The map lineup with whose pick each one is.

        The decider is the map left after the veto: nobody picked it, so it has
        no `pick` class on either side.
        """
        side = self.our_side(team_id)
        result = []
        for line in self.maps:
            if not line.name or line.name.upper() == "TBA":
                continue
            if line.picked_by is None:
                owner = "decider"
            elif line.picked_by == side:
                owner = "team"
            else:
                owner = "opponent"
            result.append({"number": line.number, "name": line.name, "pick": owner})
        return result

    def live_map(self) -> Optional[MapLine]:
        """The map being played right now: it has a score but no statistics yet."""
        if self.status != STATUS_LIVE:
            return None
        for line in self.maps:
            if line.has_score and not line.has_stats:
                return line
        return None

    def map_score(self, line: MapLine, team_id: int) -> Tuple[Optional[int], Optional[int]]:
        """The map score, oriented on our team."""
        if self.our_side(team_id) == "right":
            return line.score_right, line.score_left
        return line.score_left, line.score_right

    def series_score(self, team_id: int) -> Tuple[int, int]:
        """The series score in maps. Computed from the decided maps, because
        the page only shows a ready-made series score once the match is
        finished."""
        ours = theirs = 0
        for line in self.final_maps():
            our_score, their_score = self.map_score(line, team_id)
            if our_score is None or their_score is None:
                continue
            if our_score > their_score:
                ours += 1
            elif their_score > our_score:
                theirs += 1
        return ours, theirs

    def series_after(self, map_number: int, team_id: int) -> Tuple[int, int]:
        """The series score as of the end of the given map.

        Needed because several maps can finish between two polls: taking the
        final series score for each of them would be a lie in the message about
        the earlier one.
        """
        ours = theirs = 0
        for line in self.final_maps():
            if line.number > map_number:
                continue
            our_score, their_score = self.map_score(line, team_id)
            if our_score is None or their_score is None:
                continue
            if our_score > their_score:
                ours += 1
            elif their_score > our_score:
                theirs += 1
        return ours, theirs

    def progress_signature(self, team_id: int) -> str:
        """A fingerprint of the match moving forward: it is how a "stalled"
        match is spotted.

        Only what is bound to change as the game goes on. Nothing volatile such
        as response time may end up here.
        """
        parts = [self.status]
        for line in self.maps:
            ours, theirs = self.map_score(line, team_id)
            parts.append(f"{line.number}:{line.name}:{ours}-{theirs}")
        return "|".join(parts)


# The only platforms worth listing. The point of the block is to open a stream
# and clip the moment by hand, and YouTube — which HLTV also lists — has no
# clip button, so a link there is a dead end dressed up as a choice. Anything
# not on this list is dropped rather than shown.
#
# Two lists and not one: the provider says what HLTV CALLS it, the hosts say
# where the link may actually lead. The second is the one that matters — the
# href comes off a web page, and this project has already been burned once by
# trusting one (`HLTV_BASE + href`, a real SSRF). We only put these in a
# message rather than requesting them, but an attacker-chosen host in a link
# the owner is invited to tap is not something to wave through.
STREAM_PROVIDERS = {"twitch": {"twitch.tv", "www.twitch.tv"},
                    "kick": {"kick.com", "www.kick.com"}}

FLAG_FILE_RE = re.compile(r"/flags/\d+x\d+/([A-Za-z]{2,8})\.")


def _viewers(text: str) -> int:
    """The viewer count, however the page chose to write it.

    Every fixture we have shows a plain number (51, 155) — a tier-one match with
    tens of thousands of viewers is not among them, so whether HLTV abbreviates
    at that size is UNKNOWN. This tolerates the shapes it might use rather than
    asserting one: "1,234", "1.2k", "3M". Getting it wrong is not cosmetic —
    the whole list is ordered by this number, so an unreadable count on the
    biggest broadcast would sort it to the BOTTOM, which is the opposite of
    what the block is for.

    Zero on anything unrecognised, with a line in the log: a stream that sorts
    last still works, a crash in the parser loses the match page entirely.
    """
    raw = (text or "").strip().lower().replace(",", "").replace(" ", "")
    if not raw:
        return 0
    multiplier = 1
    if raw[-1] in "km":
        multiplier = 1000 if raw[-1] == "k" else 1000000
        raw = raw[:-1]
    try:
        return max(0, int(float(raw) * multiplier))
    except ValueError:
        log.warning("could not read a viewer count from %r", text)
        return 0


def _stream(box) -> Optional[StreamLink]:
    """One `.stream-box`, or None when it is not a platform we can use."""
    provider = (box.get("data-stream-provider") or "").strip().lower()
    hosts = STREAM_PROVIDERS.get(provider)
    if hosts is None:
        return None

    link = box.select_one(".external-stream a[href]")
    if link is None:
        return None
    url = link["href"].strip()
    try:
        parts = urlparse(url)
    except ValueError:
        return None
    if parts.scheme != "https" or (parts.hostname or "").lower() not in hosts:
        log.warning("stream link of provider %s points at %s — dropped",
                    provider, parts.hostname)
        return None

    embed = box.select_one("[data-stream-embed]")
    flag_el = box.select_one("img.stream-flag")
    found = FLAG_FILE_RE.search(flag_el.get("src") or "") if flag_el else None
    viewers_el = box.select_one(".viewers")
    return StreamLink(
        provider=provider,
        # The caster's name is the anchor text of the link in the message, so
        # an empty one would render as a bare underline.
        name=(embed.get_text(strip=True) if embed else "") or parts.path.strip("/") or provider,
        flag=(found.group(1).upper() if found else "WORLD"),
        viewers=_viewers(viewers_el.get_text(strip=True)) if viewers_el else 0,
        url=url,
    )


def _streams(soup) -> Tuple[StreamLink, ...]:
    """Every usable broadcast, most watched first.

    Sorted here rather than at render time so that everything downstream — the
    stored list, the payload, the selection — agrees on what "the top three"
    means without each re-deriving it.
    """
    found = []
    for box in soup.select("div.stream-box"):
        one = _stream(box)
        if one is not None:
            found.append(one)
    found.sort(key=lambda item: item.viewers, reverse=True)
    return tuple(found)


def _int_or_none(text: str) -> Optional[int]:
    text = text.strip()
    return int(text) if text.lstrip("-").isdigit() and text != "-" else None


def _team(soup, selector: str) -> Tuple[Optional[int], str]:
    box = soup.select_one(selector)
    if box is None:
        return None, ""
    link = box.select_one('a[href*="/team/"]')
    found = TEAM_ID_RE.search(link["href"]) if link else None
    name_el = box.select_one(".teamName")
    return (int(found.group(1)) if found else None), (name_el.get_text(strip=True) if name_el else "")


def _status(soup) -> str:
    countdown = soup.select_one(".countdown")
    if countdown is None:
        return STATUS_UNKNOWN
    text = countdown.get_text(" ", strip=True)
    lowered = text.lower()
    if "live" in lowered:
        return STATUS_LIVE
    if "over" in lowered:
        return STATUS_OVER
    if re.search(r"\d+\s*[hmsd]", lowered) or ":" in text:
        return STATUS_UPCOMING
    log.warning("unfamiliar match state on the page: %r", text)
    return STATUS_UNKNOWN


def parse(html: str, match_id: int) -> MatchObservation:
    soup = BeautifulSoup(html, "lxml")
    if soup.select_one(".teamsBox") is None:
        raise ParseError("no .teamsBox on the match page")

    # The scope is mandatory: an unqualified [data-unix] picks up another
    # match's time from the sidebar widget and produces false E2 events.
    time_el = soup.select_one(".timeAndEvent [data-unix]")
    start_utc = (datetime.fromtimestamp(int(time_el["data-unix"]) / 1000, tz=timezone.utc)
                 if time_el else None)

    event_link = soup.select_one(".timeAndEvent .event a") or soup.select_one(".event a")
    event_name = event_link.get_text(strip=True) if event_link else ""
    event_found = EVENT_ID_RE.search(event_link["href"]) if event_link else None

    fmt_el = soup.select_one(".preformatted-text")
    best_of = None
    if fmt_el:
        found = BEST_OF_RE.search(fmt_el.get_text(" ", strip=True))
        best_of = int(found.group(1)) if found else None

    team1_id, team1_name = _team(soup, ".team1-gradient")
    team2_id, team2_name = _team(soup, ".team2-gradient")

    maps: List[MapLine] = []
    for number, holder in enumerate(soup.select(".mapholder"), start=1):
        name_el = holder.select_one(".mapname")
        # Scoped by side rather than holder.select(".results-team-score"): the
        # halves live separately and must not be mixed into the map score.
        left_el = holder.select_one(".results-left .results-team-score")
        right_el = holder.select_one(".results-right .results-team-score")
        halves_el = holder.select_one(".results-center-half-score")
        left_side = holder.select_one(".results-left")
        right_side = holder.select_one(".results-right")
        picked_by = None
        if left_side is not None and "pick" in (left_side.get("class") or []):
            picked_by = "left"
        elif right_side is not None and "pick" in (right_side.get("class") or []):
            picked_by = "right"

        maps.append(MapLine(
            number=number,
            name=name_el.get_text(strip=True) if name_el else "TBA",
            score_left=_int_or_none(left_el.get_text(strip=True)) if left_el else None,
            score_right=_int_or_none(right_el.get_text(strip=True)) if right_el else None,
            halves=halves_el.get_text(" ", strip=True) if halves_el else None,
            has_stats=holder.select_one(".results-stats") is not None,
            picked_by=picked_by,
        ))

    scoreboard = soup.select_one("#scoreboardElement")
    scorebot_id = None
    regulation = overtime = None
    if scoreboard is not None:
        if scoreboard.get("data-scorebot-id"):
            scorebot_id = int(scoreboard["data-scorebot-id"])
        if scoreboard.get("data-max-rounds-regulation"):
            regulation = int(scoreboard["data-max-rounds-regulation"])
        if scoreboard.get("data-max-rounds-overtime"):
            overtime = int(scoreboard["data-max-rounds-overtime"])

    return MatchObservation(
        match_id=match_id,
        status=_status(soup),
        start_utc=start_utc,
        event_name=event_name,
        event_id=int(event_found.group(1)) if event_found else None,
        best_of=best_of,
        team1_id=team1_id,
        team1_name=team1_name,
        team2_id=team2_id,
        team2_name=team2_name,
        maps=maps,
        scorebot_id=scorebot_id,
        max_rounds_regulation=regulation,
        max_rounds_overtime=overtime,
        streams=_streams(soup),
    )
