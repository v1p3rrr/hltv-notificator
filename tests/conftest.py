import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

FIXTURES = ROOT / "docs" / "recon" / "fixtures"

from hltv_notify.config import Config  # noqa: E402
from hltv_notify.models import ScheduleEntry  # noqa: E402
from hltv_notify.state.db import Storage  # noqa: E402

TEAM_ID = 12857


@pytest.fixture()
def storage(tmp_path) -> Storage:
    store = Storage(tmp_path / "test.db")
    yield store
    store.close()


@pytest.fixture()
def config(monkeypatch) -> Config:
    for name in list(__import__("os").environ):
        if name.startswith(("TEAM_", "POLL_", "E2_", "TELEGRAM_", "DRY_RUN")):
            monkeypatch.delenv(name, raising=False)
    return Config()


@pytest.fixture()
def team_page_html() -> str:
    return (FIXTURES / "team-12857-forze-reload.html").read_text(encoding="utf-8")


def entry(match_id: int = 111, *, start: datetime = None, opponent_id=13973,
          opponent_name="Color", finished=False, score=(None, None)) -> ScheduleEntry:
    start = start or datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)
    return ScheduleEntry(
        match_id=match_id,
        start_utc=start,
        opponent_id=opponent_id,
        opponent_name=opponent_name,
        event_name="Test Event",
        url=f"https://www.hltv.org/matches/{match_id}/test",
        finished=finished,
        score_team=score[0],
        score_opponent=score[1],
    )


def later(minutes: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)
