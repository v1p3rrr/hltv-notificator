"""The watchdog: the alarm that notifications have stopped working.

The point of the event is "I have gone blind, go and look by hand". So what is
checked is not only that an alarm happens but also its URGENCY: the closer the
decisive moment, the less silence is acceptable.
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


# ---------------------------------------------------------------- urgency


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
    assert "less than a minute" in reason


def test_match_that_should_have_started_is_urgent(dog, storage):
    """The nastiest case: the match should have started and we are blind, with
    no idea whether it is running."""
    add_match(storage, starts_in_minutes=-5)
    delay, reason = dog.urgency()
    assert delay == URGENT_SECONDS
    assert "cannot see it" in reason


def test_long_past_start_is_no_longer_urgent(dog, storage):
    """A match that should have started an hour ago and never did may simply
    not have happened — holding the alarm at the one-minute threshold is
    pointless."""
    add_match(storage, starts_in_minutes=-90)
    assert dog.urgency()[0] == 300


def test_five_minutes_before_the_start_is_not_urgent(dog, storage):
    add_match(storage, starts_in_minutes=5)
    delay, _ = dog.urgency()
    assert delay == 300


def test_three_rounds_to_win_is_urgent(dog, storage):
    """The decisive moment is close — five minutes of silence is not on."""
    add_match(storage, starts_in_minutes=-30, state=MatchState.LIVE, score="10-5")
    delay, reason = dog.urgency()
    assert delay == URGENT_SECONDS
    assert "3 round" in reason


def test_comfortable_score_is_not_urgent(dog, storage):
    add_match(storage, starts_in_minutes=-30, state=MatchState.LIVE, score="5-4")
    delay, _ = dog.urgency()
    assert delay == 300


def test_overtime_is_urgent(dog, storage):
    add_match(storage, starts_in_minutes=-30, state=MatchState.LIVE, score="13-13")
    delay, reason = dog.urgency()
    assert delay == URGENT_SECONDS
    assert "overtime" in reason


def test_urgency_respects_the_format_from_the_source(dog, storage):
    """The very same 10:5 is decisive under MR12 (3 rounds left) but not yet
    under MR15 (6 left). The thresholds come from the format, they are not
    hardcoded."""
    add_match(storage, starts_in_minutes=-30, state=MatchState.LIVE, score="10-5",
              regulation=12, match_id=901)
    assert dog.urgency()[0] == URGENT_SECONDS

    storage.set_state(901, MatchState.FINISHED, source="scorebot")
    add_match(storage, starts_in_minutes=-30, state=MatchState.LIVE, score="10-5",
              regulation=15, match_id=902)
    assert dog.urgency()[0] == 300


# ---------------------------------------------------------------- the alarm


def test_short_outage_is_not_reported(dog):
    now = utcnow()
    assert dog.report_failure("schedule", "timeout", now) == []
    assert dog.report_failure("schedule", "timeout", now + timedelta(seconds=30)) == []


def test_outage_past_the_threshold_is_reported(dog):
    now = utcnow()
    dog.report_failure("schedule", "timeout", now)
    events = dog.report_failure("schedule", "timeout", now + timedelta(seconds=400))
    assert [e.type for e in events] == ["E8"]
    assert "schedule cannot be read" in events[0].payload["reason"]


def test_one_alert_per_outage(dog, storage, config):
    """The key contains the moment the failure started, so repeated checks of
    the same failure give the same key and the notification is not doubled."""
    from hltv_notify.notify.outbox import Notifier

    n = Notifier(storage, config, telegram=None)
    now = utcnow()
    dog.report_failure("schedule", "timeout", now)
    for minutes in (7, 8, 9):
        for event in dog.report_failure("schedule", "timeout", now + timedelta(minutes=minutes)):
            n.enqueue(event)
    assert storage.sent_event_count() == 1


def test_urgent_situation_alerts_after_a_minute(dog, storage):
    add_match(storage, starts_in_minutes=-30, state=MatchState.LIVE, score="12-9")
    now = utcnow()
    dog.report_failure("live_feed", "no connection", now)
    assert dog.report_failure("live_feed", "no connection", now + timedelta(seconds=45)) == []
    events = dog.report_failure("live_feed", "no connection", now + timedelta(seconds=70))
    assert [e.type for e in events] == ["E8"]


def test_alert_carries_a_match_link(dog, storage):
    """From the alarm you must be able to go and look with your own eyes."""
    add_match(storage, starts_in_minutes=-30, state=MatchState.LIVE, score="10-5")
    now = utcnow()
    dog.report_failure("live_feed", "no connection", now)
    events = dog.report_failure("live_feed", "no connection", now + timedelta(seconds=70))
    assert events[0].payload["url"].endswith("/x")


# ---------------------------------------------------------------- recovery


def test_recovery_is_reported_after_a_real_outage(dog):
    now = utcnow()
    dog.report_failure("schedule", "timeout", now)
    dog.report_failure("schedule", "timeout", now + timedelta(seconds=400))
    events = dog.report_success("schedule", now + timedelta(seconds=500))
    assert [e.type for e in events] == ["E8R"]
    assert "Working again" in events[0].payload["detail"]


def test_recovery_needs_an_alarm_that_was_actually_sent(dog, storage):
    """Seen in production: the live feed connected 0.7 s after the countdown
    began, yet a minute later "Recovered — the live feed will not come up, the
    outage lasted 1 min" arrived. Nobody had ever been told about an outage.

    Elapsed time is the wrong test. A subsystem is only re-checked on its
    poller's own cycle, so an outage of a second can look like a minute — and
    an outage of 35 minutes can pass without a single alarm, because the next
    attempt succeeded.
    """
    now = utcnow()
    dog.report_failure("live_feed", "no connection", now)          # countdown starts
    assert dog.report_success("live_feed", now + timedelta(minutes=1)) == []


def test_a_long_outage_nobody_was_told_about_stays_quiet(dog):
    """The schedule case from the same logs: it failed at 04:21 and the next
    attempt was only due 35 minutes later, by which time it worked. No alarm
    was ever sent, so there is nothing to recover from."""
    now = utcnow()
    dog.report_failure("schedule", "timeout", now)
    assert dog.report_success("schedule", now + timedelta(minutes=35)) == []


def test_recovery_still_arrives_after_a_real_alarm(dog):
    now = utcnow()
    dog.report_failure("schedule", "timeout", now)
    assert [e.type for e in dog.report_failure(
        "schedule", "timeout", now + timedelta(seconds=400))] == ["E8"]
    events = dog.report_success("schedule", now + timedelta(seconds=500))
    assert [e.type for e in events] == ["E8R"]


def test_a_new_outage_after_a_recovery_starts_clean(dog):
    """The alarm flag must be cleared, otherwise the next short blip would
    produce a "Recovered" of its own."""
    now = utcnow()
    dog.report_failure("schedule", "timeout", now)
    dog.report_failure("schedule", "timeout", now + timedelta(seconds=400))
    dog.report_success("schedule", now + timedelta(seconds=500))

    later = now + timedelta(hours=1)
    dog.report_failure("schedule", "timeout", later)
    assert dog.report_success("schedule", later + timedelta(seconds=90)) == []


def test_blink_is_not_reported(dog):
    """A failure nobody learned about must not produce a "recovered"."""
    now = utcnow()
    dog.report_failure("schedule", "timeout", now)
    assert dog.report_success("schedule", now + timedelta(seconds=10)) == []


def test_success_without_a_prior_failure_is_silent(dog):
    assert dog.report_success("schedule") == []


# ---------------------------------------------------------------- queue and feed


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
    assert "feed" in events[0].payload["reason"].lower()


def test_connected_feed_clears_the_alarm(dog, storage):
    add_match(storage, starts_in_minutes=-30, state=MatchState.LIVE, score="5-4")
    now = utcnow()
    dog.check_live_feed({}, now)
    dog.check_live_feed({MATCH: True}, now + timedelta(seconds=30))
    assert "live_feed" not in dog.degraded_subsystems()


def test_no_live_match_means_no_feed_alarm(dog, storage):
    add_match(storage, starts_in_minutes=60)
    assert dog.check_live_feed({}) == []
