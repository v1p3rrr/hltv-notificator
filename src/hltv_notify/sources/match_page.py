"""Парсер страницы матча: состояние, формат, счёт по картам.

Разметка (проверена на трёх фикстурах — upcoming, live, finished):
    .timeAndEvent [data-unix]      время старта. ОБЯЗАТЕЛЬНО со скоупом:
                                   без него первым в DOM идёт виджет
                                   .fbw-vp-header-time с временем ЧУЖИХ матчей
    .countdown                     "4h : 57m : 28s" / "LIVE" / "Match over"
    .timeAndEvent .event a         турнир
    .preformatted-text             "Best of 3 (Online)" и примечания
    .team1-gradient / .team2-gradient
        a[href*="/team/"]          id команды
        .teamName                  имя
        .won / .lost               счёт СЕРИИ, но только у завершённого матча
    .mapholder
        .mapname                   имя карты, "TBA" до вето
        .results-team-score x2     счёт карты, "-" у несыгранной
        .results-center-half-score "( 5 : 7 ; 8 : 3 )"

Счёт серии во время игры на странице не выводится, поэтому считается по
числу решённых карт — это же и есть правило конца карты из решения D7.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

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
    """Разметка не та, что ожидалась — отказ источника, а не «данных нет»."""


@dataclass(frozen=True)
class MapLine:
    number: int
    name: str
    score_left: Optional[int]
    score_right: Optional[int]
    halves: Optional[str]

    @property
    def decided(self) -> bool:
        """Карта считается сыгранной, когда у неё появился числовой счёт.

        Не «13 раундов»: правило не должно ломаться на овертайме, на форматах
        с другим числом раундов и на технических поражениях.
        """
        return self.score_left is not None and self.score_right is not None


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

    def decided_maps(self) -> List[MapLine]:
        return [m for m in self.maps if m.decided]

    def map_score(self, line: MapLine, team_id: int) -> Tuple[Optional[int], Optional[int]]:
        """Счёт карты, ориентированный на нашу команду."""
        if self.our_side(team_id) == "right":
            return line.score_right, line.score_left
        return line.score_left, line.score_right

    def series_score(self, team_id: int) -> Tuple[int, int]:
        """Счёт серии по картам. Считается по решённым картам, потому что
        готовый счёт серии страница показывает только у завершённого матча."""
        ours = theirs = 0
        for line in self.decided_maps():
            our_score, their_score = self.map_score(line, team_id)
            if our_score is None or their_score is None:
                continue
            if our_score > their_score:
                ours += 1
            elif their_score > our_score:
                theirs += 1
        return ours, theirs

    def progress_signature(self, team_id: int) -> str:
        """Отпечаток продвижения матча: по нему видно, что матч «завис».

        Только то, что обязано меняться по ходу игры. Ничего волатильного
        вроде времени ответа сюда попадать не должно.
        """
        parts = [self.status]
        for line in self.maps:
            ours, theirs = self.map_score(line, team_id)
            parts.append(f"{line.number}:{line.name}:{ours}-{theirs}")
        return "|".join(parts)


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
    log.warning("незнакомое состояние матча на странице: %r", text)
    return STATUS_UNKNOWN


def parse(html: str, match_id: int) -> MatchObservation:
    soup = BeautifulSoup(html, "lxml")
    if soup.select_one(".teamsBox") is None:
        raise ParseError("на странице матча нет .teamsBox")

    # Скоуп обязателен: неквалифицированный [data-unix] берёт время чужого
    # матча из бокового виджета и порождает ложные E2.
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
        scores = [_int_or_none(e.get_text(strip=True))
                  for e in holder.select(".results-team-score")]
        halves_el = holder.select_one(".results-center-half-score")
        maps.append(MapLine(
            number=number,
            name=name_el.get_text(strip=True) if name_el else "TBA",
            score_left=scores[0] if len(scores) > 0 else None,
            score_right=scores[1] if len(scores) > 1 else None,
            halves=halves_el.get_text(" ", strip=True) if halves_el else None,
        ))

    scoreboard = soup.select_one("#scoreboardElement")
    scorebot_id = None
    if scoreboard is not None and scoreboard.get("data-scorebot-id"):
        scorebot_id = int(scoreboard["data-scorebot-id"])

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
    )
