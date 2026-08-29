"""Таймзоны на переходе DST и потолок частоты запросов."""

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from hltv_notify import http as http_module
from hltv_notify.config import HARD_MIN_REQUEST_INTERVAL_SECONDS, Config
from hltv_notify.http import HltvHttp
from hltv_notify.notify.format import human_time, to_local

RIGA = "Europe/Riga"


def test_dst_start_shifts_offset_by_an_hour():
    """Летнее время в ЕС наступает в последнее воскресенье марта в 01:00 UTC.
    До него Рига +2, после +3."""
    before = to_local(datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc), RIGA)
    after = to_local(datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc), RIGA)
    assert before.utcoffset() == timedelta(hours=2)
    assert after.utcoffset() == timedelta(hours=3)
    assert human_time(datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc), RIGA).endswith("02:30")
    assert human_time(datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc), RIGA).endswith("04:30")


def test_match_a_month_ahead_does_not_drift():
    """Матч, запланированный до перехода на летнее время, при наивной работе
    со смещением уехал бы на час. Храним UTC — не уезжает."""
    utc = datetime(2026, 4, 15, 16, 0, tzinfo=timezone.utc)
    assert human_time(utc, RIGA).endswith("19:00")   # +3, летнее время
    winter = datetime(2026, 2, 15, 16, 0, tzinfo=timezone.utc)
    assert human_time(winter, RIGA).endswith("18:00")  # +2, зимнее


def test_dst_end_is_handled():
    before = to_local(datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc), RIGA)
    after = to_local(datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc), RIGA)
    assert before.utcoffset() == timedelta(hours=3)
    assert after.utcoffset() == timedelta(hours=2)


def test_config_cannot_lower_the_ceiling(monkeypatch):
    """Потолок зашит в код: конфигом его не пробить."""
    monkeypatch.setenv("POLL_LIVE_SECONDS", "1")
    config = Config()
    assert config.poll_live == 1
    assert config.interval_for("live") == int(HARD_MIN_REQUEST_INTERVAL_SECONDS)


def test_requests_are_spaced_by_the_ceiling(monkeypatch):
    """Два запроса подряд не могут уйти чаще потолка."""
    monkeypatch.setattr(http_module, "HARD_MIN_REQUEST_INTERVAL_SECONDS", 0.3)

    class FakeResponse:
        status_code = 200
        text = "ok"

    class FakeSession:
        async def get(self, url, timeout=None):
            return FakeResponse()

    client = HltvHttp(Config())

    async def fake_session():
        return FakeSession()

    monkeypatch.setattr(client, "_ensure_session", fake_session)

    async def scenario():
        started = time.monotonic()
        await client.get_text("https://example.test/1")
        await client.get_text("https://example.test/2")
        return time.monotonic() - started

    assert asyncio.run(scenario()) >= 0.3


def test_long_poll_is_exempt_from_the_ceiling(monkeypatch):
    """Удерживаемое соединение живого фида — это не частый опрос, и под
    потолок оно попадать не должно."""
    monkeypatch.setattr(http_module, "HARD_MIN_REQUEST_INTERVAL_SECONDS", 5.0)

    class FakeResponse:
        status_code = 200
        text = "ok"

    class FakeSession:
        async def get(self, url, timeout=None):
            return FakeResponse()

    client = HltvHttp(Config())

    async def fake_session():
        return FakeSession()

    monkeypatch.setattr(client, "_ensure_session", fake_session)

    async def scenario():
        started = time.monotonic()
        await client.get_text("https://example.test/1", exempt_from_ceiling=True)
        await client.get_text("https://example.test/2", exempt_from_ceiling=True)
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 1.0


def test_jitter_stays_within_bounds():
    values = [http_module.jittered(100) for _ in range(200)]
    assert all(80 <= v <= 120 for v in values)
    assert len(set(values)) > 1  # запросы не идут ровно по сетке
