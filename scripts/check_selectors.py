"""Проверка черновых селекторов по сохранённым фикстурам (серверный HTML)."""

import re
from pathlib import Path

from bs4 import BeautifulSoup

FIXTURES = Path(__file__).resolve().parent.parent / "docs" / "recon" / "fixtures"


def soup(name: str) -> BeautifulSoup:
    return BeautifulSoup((FIXTURES / name).read_text(encoding="utf-8"), "lxml")


def dump_team_page() -> None:
    s = soup("team-12857-forze-reload.html")
    print("=== TEAM PAGE ===")
    for table in s.select("table.match-table"):
        caption = table.find_previous(class_="standard-headline")
        print(f"-- {caption.get_text(strip=True) if caption else '?'}")
        event = "?"
        for row in table.select("tr"):
            if "event-header-cell" in (row.get("class") or []):
                event = row.get_text(strip=True)
                continue
            if "team-row" not in (row.get("class") or []):
                continue
            link = row.select_one('a[href*="/matches/"]')
            unix = row.select_one("[data-unix]")
            teams = [e.get_text(strip=True) for e in row.select(".team-name, .team")]
            score = row.select_one(".score-cell, .score")
            match_id = None
            if link:
                found = re.search(r"/matches/(\d+)/", link["href"])
                match_id = found.group(1) if found else None
            when = unix["data-unix"] if unix else None
            print(
                f"   unix={when} id={match_id} teams={teams[:2]}"
                f" score={score.get_text(strip=True) if score else None}"
                f" event={event[:40]}"
            )


def dump_match_page(name: str) -> None:
    s = soup(name)
    print(f"\n=== MATCH PAGE {name} ===")
    sb = s.select_one("#scoreboardElement")
    print("scorebot:", {k: v for k, v in sb.attrs.items() if k.startswith("data-scorebot") or "rounds" in k} if sb else None)
    unix = s.select_one(".timeAndEvent [data-unix], [data-unix]")
    print("start data-unix:", unix["data-unix"] if unix else None)
    print("countdown:", (s.select_one(".countdown").get_text(strip=True) if s.select_one(".countdown") else None))
    ev = s.select_one(".timeAndEvent .event a, .event a")
    print("event:", (ev.get_text(strip=True), ev.get("href")) if ev else None)
    fmt = s.select_one(".preformatted-text")
    print("format:", fmt.get_text(" ", strip=True)[:70] if fmt else None)
    for i, holder in enumerate(s.select(".mapholder"), start=1):
        mapname = holder.select_one(".mapname")
        results = [e.get_text(strip=True) for e in holder.select(".results-team-score")]
        halves = holder.select_one(".results-center-half-score")
        print(
            f"  map{i}: {mapname.get_text(strip=True) if mapname else '?':<10}"
            f" scores={results} halves={halves.get_text(' ', strip=True) if halves else None}"
        )
    winner = s.select_one(".team1-gradient .won, .team2-gradient .won")
    print("winner marker:", winner.get_text(strip=True) if winner else None)


dump_team_page()
for fixture in ("match-2397340-upcoming.html", "match-2397053-live.html", "match-2397047-finished.html"):
    dump_match_page(fixture)
