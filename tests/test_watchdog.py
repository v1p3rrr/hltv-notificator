"""Сторож: тревога о том, что уведомления перестали работать.

Смысл события — «я ослеп, сходи посмотри руками». Поэтому проверяется не
только факт тревоги, но и её СРОЧНОСТЬ: чем ближе развязка, тем меньше можно
молчать.
"""

from datetime import timedelta

import pytest

from hltv_notify.config import Config
from hltv_notify.models import MatchState
from hltv_notify.state.db import utcnow
from hltv_notify.watchdog import MAX_ALERT_SECONDS, URGENT_SECONDS, Watchdog

MATCH = 321


@pytest.fixture()
def dog(storage, config):
    return Watchdog(storage, config)


def add_match(storage, *, starts_in_minutes=60, state=None, score=None,
              regulation=12, overtime=3, match_id=MATCH):
    storage.upsert_match(
        match_id=match_id, opponent_id=1, opponent_name="Color", event_name="Test",
        start_utc=utcnow() + timedelta(minutes=starts_in_minutes),
        url=f"https://www.hltv.org/matches/{match_id}/x",
        snapshot={}, snapshot_hash="h")
    if state:
        storage.set_state(match_id, state, source="scorebot",
                          current_map_score=score)
        storage.set_map_format(match_id, regulation, overtime)


# ---------------------------------------------------------------- срочность


def test_default_is_the_configured_delay(dog, storage):
    delay, _ = dog.urgency()
    assert delay == dog.normal_delay == 300


def test_delay_is_clamped(storage):
    assert Watchdog(storage, Config(degraded_alert_seconds=5)).normal_delay == URGENT_SECONDS
    assert Watchdog(storage, Config(degraded_alert_seconds=9000)).normal_delay == MAX_ALERT_SECONDS


def test_minute_before_the_start_is_urgent(dog, storage):
    add_match(storage, starts_in_minutes=0.5)
    delay, reason = dog.urgency()
    assert delay == URGENT_SECONDS
    assert "меньше минуты" in reason


def test_match_that_should_have_started_is_urgent(dog, storage):
    """Самый неприятный случай: матч должен был начаться, а мы слепы и не
    знаем, идёт он или нет."""
    add_match(storage, starts_in_minutes=-5)
    delay, reason = dog.urgency()
    assert delay == URGENT_SECONDS
    assert "не видим" in reason


def test_long_past_start_is_no_longer_urgent(dog, storage):
    """Матч, который должен был начаться час назад и так и не начался, мог и
    не состояться — держать тревогу на минутном пороге бессмысленно."""
    add_match(storage, starts_in_minutes=-90)
    assert dog.urgency()[0] == 300


def test_five_minutes_before_the_start_is_not_urgent(dog, storage):
    add_match(storage, starts_in_minutes=5)
    delay, _ = dog.urgency()
    assert delay == 300


def test_three_rounds_to_win_is_urgent(dog, storage):
    """Развязка близко — молчать пять минут нельзя."""
    add_match(storage, starts_in_minutes=-30, state=MatchState.LIVE, score="10-5")
    delay, reason = dog.urgency()
    assert delay == URGENT_SECONDS
    assert "3 раунд" in reason


def test_comfortable_score_is_not_urgent(dog, storage):
    add_match(storage, starts_in_minutes=-30, state=MatchState.LIVE, score="5-4")
    delay, _ = dog.urgency()
    assert delay == 300


def test_overtime_is_urgent(dog, storage):
    add_match(storage, starts_in_minutes=-30, state=MatchState.LIVE, score="13-13")
    delay, reason = dog.urgency()
    assert delay == URGENT_SECONDS
    assert "овертайм" in reason


def test_urgency_respects_the_format_from_the_source(dog, storage):
    """Один и тот же счёт 10:5 при MR12 решающий (осталось 3 раунда), а при
    MR15 — ещё нет (осталось 6). Пороги берутся из формата, а не зашиты."""
    add_match(storage, starts_in_minutes=-30, state=MatchState.LIVE, score="10-5",
              regulation=12, match_id=901)
    assert dog.urgency()[0] == URGENT_SECONDS

    storage.set_state(901, MatchState.FINISHED, source="scorebot")
    add_match(storage, starts_in_minutes=-30, state=MatchState.LIVE, score="10-5",
              regulation=15, match_id=902)
    assert dog.urgency()[0] == 300


# ---------------------------------------------------------------- тревога


def test_short_outage_is_not_reported(dog):
    now = utcnow()
    assert dog.report_failure("schedule", "таймаут", now) == []
    assert dog.report_failure("schedule", "таймаут", now + timedelta(seconds=30)) == []


def test_outage_past_the_threshold_is_reported(dog):
    now = utcnow()
    dog.report_failure("schedule", "таймаут", now)
    events = dog.report_failure("schedule", "таймаут", now + timedelta(seconds=400))
    assert [e.type for e in events] == ["E8"]
    assert "Расписание не читается" in events[0].payload["reason"]


def test_one_alert_per_outage(dog, storage, config):
    """Ключ содержит момент начала сбоя, поэтому повторные проверки того же
    сбоя дают тот же ключ и уведомление не задваивается."""
    from hltv_notify.notify.outbox import Notifier

    n = Notifier(storage, config, telegram=None)
    now = utcnow()
    dog.report_failure("schedule", "таймаут", now)
    for minutes in (7, 8, 9):
        for event in dog.report_failure("schedule", "таймаут", now + timedelta(minutes=minutes)):
            n.enqueue(event)
    assert storage.sent_event_count() == 1


def test_urgent_situation_alerts_after_a_minute(dog, storage):
    add_match(storage, starts_in_minutes=-30, state=MatchState.LIVE, score="12-9")
    now = utcnow()
    dog.report_failure("live_feed", "нет связи", now)
    assert dog.report_failure("live_feed", "нет связи", now + timedelta(seconds=45)) == []
    events = dog.report_failure("live_feed", "нет связи", now + timedelta(seconds=70))
    assert [e.type for e in events] == ["E8"]


def test_alert_carries_a_match_link(dog, storage):
    """Из тревоги надо иметь возможность сразу пойти и посмотреть глазами."""
    add_match(storage, starts_in_minutes=-30, state=MatchState.LIVE, score="10-5")
    now = utcnow()
    dog.report_failure("live_feed", "нет связи", now)
    events = dog.report_failure("live_feed", "нет связи", now + timedelta(seconds=70))
    assert events[0].payload["url"].endswith("/x")


# ---------------------------------------------------------------- восстановление


def test_recovery_is_reported_after_a_real_outage(dog):
    now = utcnow()
    dog.report_failure("schedule", "таймаут", now)
    dog.report_failure("schedule", "таймаут", now + timedelta(seconds=400))
    events = dog.report_success("schedule", now + timedelta(seconds=500))
    assert [e.type for e in events] == ["E8R"]
    assert "Снова работает" in events[0].payload["detail"]


def test_blink_is_not_reported(dog):
    """Сбой, о котором никто не узнал, не должен порождать «восстановилось»."""
    now = utcnow()
    dog.report_failure("schedule", "таймаут", now)
    assert dog.report_success("schedule", now + timedelta(seconds=10)) == []


def test_success_without_a_prior_failure_is_silent(dog):
    assert dog.report_success("schedule") == []


# ---------------------------------------------------------------- очередь и фид


def test_stuck_outbox_is_reported(dog, storage, config):
    from hltv_notify.models import Event
    from hltv_notify.notify.outbox import Notifier

    Notifier(storage, config, telegram=None).enqueue(
        Event(type="E1", idempotency_key="k", match_id=1,
              payload={"opponent": "X", "event_name": "E",
                       "start_utc": utcnow().isoformat(), "url": "u"}))
    now = utcnow()
    dog.check_outbox(now)
    events = dog.check_outbox(now + timedelta(minutes=10))
    assert [e.type for e in events] == ["E8"]
    assert "Telegram" in events[0].payload["reason"]


def test_empty_outbox_clears_the_alarm(dog):
    assert dog.check_outbox() == []
    assert "outbox" not in dog.degraded_subsystems()


def test_live_match_without_feed_is_reported(dog, storage):
    add_match(storage, starts_in_minutes=-30, state=MatchState.LIVE, score="5-4")
    now = utcnow()
    dog.check_live_feed({}, now)
    events = dog.check_live_feed({}, now + timedelta(minutes=10))
    assert [e.type for e in events] == ["E8"]
    assert "фид" in events[0].payload["reason"].lower()


def test_connected_feed_clears_the_alarm(dog, storage):
    add_match(storage, starts_in_minutes=-30, state=MatchState.LIVE, score="5-4")
    now = utcnow()
    dog.check_live_feed({}, now)
    dog.check_live_feed({MATCH: True}, now + timedelta(seconds=30))
    assert "live_feed" not in dog.degraded_subsystems()


def test_no_live_match_means_no_feed_alarm(dog, storage):
    add_match(storage, starts_in_minutes=60)
    assert dog.check_live_feed({}) == []
