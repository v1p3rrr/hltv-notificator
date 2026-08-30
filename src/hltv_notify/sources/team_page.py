"""Парсер страницы команды — источник расписания.

Почему именно эта страница, а не /matches?team=<id>: последнее запрещено
robots.txt (см. docs/recon/R5), а здесь лежат те же данные в разрешённой зоне.

Разметка (проверена на фикстуре, docs/recon/R3):
    table.match-table
      tr.event-header-cell         название турнира, действует до следующего
      tr.team-row
        td.date-cell span[data-unix]      время старта, epoch в МИЛЛИсекундах
        a.team-name.team-1 / .team-2      обе команды, href /team/<id>/<slug>
        .score-cell .score  x2            "-" до игры, числа после
        a.matchpage-button                предстоящий матч
        a.stats-button                    сыгранный матч
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
# Хвост адреса матча. Набор символов узкий намеренно: ни слэша, ни точки,
# ни собаки — то есть из него нельзя выбраться за пределы одного сегмента пути.
MATCH_SLUG_RE = re.compile(r"/matches/\d+/([A-Za-z0-9_-]{1,120})")


def match_url(match_id: int, href: str = "") -> str:
    """Адрес страницы матча.

    Собирается ИЗ ПРОВЕРЕННОГО ЧИСЛА, а не склейкой базы с тем, что пришло со
    страницы. Склейка была дырой: `HLTV_BASE` не заканчивается слэшем, поэтому
    href вида `@10.0.0.1:8080/matches/1/x` давал
    `https://www.hltv.org@10.0.0.1:8080/matches/1/x`, где `www.hltv.org` — это
    userinfo, а запрос уходил на `10.0.0.1`. Проверено: libcurl идёт именно
    туда. Вариант `.evil.example/matches/1/x` не требовал даже собаки.

    Хвост из href берётся только ради читаемости ссылки и только если он
    состоит из безобидных символов; HLTV на сам хвост не смотрит.
    """
    found = MATCH_SLUG_RE.search(href or "")
    return f"{HLTV_BASE}/matches/{match_id}/{found.group(1) if found else '-'}"


class ParseError(RuntimeError):
    """Разметка не та, что ожидалась. Трактуется как отказ источника, а не
    как «матчей нет» — иначе редизайн выглядел бы как пустое расписание."""


def _team_ref(anchor) -> Tuple[Optional[int], str]:
    """id и имя команды из ссылки. У плейсхолдера («Winner of match X») ссылки
    нет, id остаётся None — матч всё равно отслеживается."""
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
    """Все матчи команды со страницы: и предстоящие, и сыгранные."""
    soup = BeautifulSoup(html, "lxml")
    tables = soup.select("table.match-table")
    if not tables:
        raise ParseError("на странице команды нет table.match-table")

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
                log.warning("матч %s без data-unix, пропускаем", match_id)
                continue
            start_utc = datetime.fromtimestamp(int(time_el["data-unix"]) / 1000, tz=timezone.utc)

            first_id, first_name = _team_ref(row.select_one("a.team-name.team-1"))
            second_id, second_name = _team_ref(row.select_one("a.team-name.team-2"))
            score_first, score_second = _score_pair(row)

            # Своя команда на странице команды идёт первой, но полагаться на
            # порядок не будем: сверяемся по id.
            if first_id == team_id:
                opponent_id, opponent_name = second_id, second_name
                score_team, score_opponent = score_first, score_second
            elif second_id == team_id:
                opponent_id, opponent_name = first_id, first_name
                score_team, score_opponent = score_second, score_first
            else:
                log.debug("матч %s без нашей команды, пропускаем", match_id)
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
        raise ParseError("ни одной строки матча не разобрано — похоже на смену вёрстки")
    return entries


def parse_team_name(html: str) -> Optional[str]:
    """Каноничное имя команды со страницы.

    Нужно при добавлении команды через бота: имя, выведенное из slug, будет
    отличаться от того, что показывает HLTV («forze-reload» → «Forze Reload»
    вместо «FORZE Reload»), а имя попадает в каждое уведомление.
    """
    soup = BeautifulSoup(html, "lxml")
    heading = soup.select_one(".profile-team-name") or soup.select_one("h1")
    name = heading.get_text(strip=True) if heading else ""
    return name or None


def upcoming(entries: List[ScheduleEntry]) -> List[ScheduleEntry]:
    return [e for e in entries if not e.finished]


def snapshot_of(entry: ScheduleEntry) -> Dict[str, Any]:
    """Снимок значимых полей. В хеш не должно попадать ничего, что меняется
    само по себе (время получения ответа и подобное) — иначе дедупликация
    сломается."""
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
