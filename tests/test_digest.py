"""The daily digest (E14): a list of what is on, at times the person chooses.

Everything here turns on two things being right at once — WHEN it fires, which
is wall-clock time in the subscriber's own zone, and WHAT it counts, which is a
rolling 24 hours rather than the rest of the calendar day. Both are easy to get
subtly wrong and impossible to notice from inside: a digest that fires an hour
late, or one that quietly drops tomorrow morning's match, still looks like a
working digest.
"""

from datetime import datetime, timedelta, timezone

import pytest

from hltv_notify import digest as dg
from hltv_notify.models import MatchState
from hltv_notify.notify import format as fmt
from hltv_notify.notify.outbox import Notifier

CHAT = "555"
OTHER = "777"
TEAM_ID = 12857
NINE = 9 * 60

# 09:00 in Moscow, 06:00 UTC. Every case below is written from the reader's
# clock, which is the whole point of the feature.
MOSCOW = "Europe/Moscow"
NOW = datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(storage):
    storage.add_subscriber(CHAT)
    storage.add_team(CHAT, TEAM_ID, "forze-reload", "FORZE Reload")
    storage.set_subscriber_timezone(CHAT, MOSCOW)
    storage.add_digest_time(CHAT, NINE)
    return storage


def add_match(storage, match_id, start, *, state=None, opponent="Color",
              opponent_id=13973, event="ESL Pro League", team_id=TEAM_ID,
              link=TEAM_ID):
    storage.upsert_match(
        match_id=match_id, team_id=team_id, opponent_id=opponent_id,
        opponent_name=opponent, event_name=event, start_utc=start,
        url=f"https://www.hltv.org/matches/{match_id}/x",
        snapshot={}, snapshot_hash="h")
    if link is not None:
        storage.link_match_team(match_id, link)
    if state:
        storage.set_state(match_id, state, source="match_page")


def due(storage, config, now=NOW):
    return dg.DigestScheduler(storage, config).due(now)


# ---------- when it fires ----------

def test_nothing_on_means_nothing_sent(store, config):
    """The strongest rule in the feature. A digest that arrives every morning
    to say "no matches" is one people mute, and then it is worth nothing on the
    morning something IS on."""
    assert due(store, config) == []


def test_a_match_today_fires_the_digest(store, config):
    add_match(store, 1, NOW + timedelta(hours=9))
    events = due(store, config)
    assert len(events) == 1
    assert events[0].type == "E14"
    assert events[0].payload["only_chat"] == CHAT
    assert events[0].payload["at"] == "09:00"


def test_it_fires_at_the_local_hour_and_not_the_utc_one(store, config):
    """09:00 Moscow is 06:00 UTC. Getting this wrong is a digest that arrives
    three hours out and looks like a scheduling bug in Telegram."""
    add_match(store, 1, NOW + timedelta(hours=9))
    assert due(store, config, NOW - timedelta(hours=1)) == []      # 08:00 local
    assert len(due(store, config, NOW)) == 1                       # 09:00 local


def test_two_people_in_different_zones_fire_at_different_moments(store, config):
    add_match(store, 1, NOW + timedelta(hours=9))
    store.add_subscriber(OTHER)
    store.add_team(OTHER, TEAM_ID, "forze-reload", "FORZE Reload")
    store.set_subscriber_timezone(OTHER, "Europe/Lisbon")   # two hours behind
    store.add_digest_time(OTHER, NINE)

    assert [e.payload["only_chat"] for e in due(store, config)] == [CHAT]
    later = due(store, config, NOW + timedelta(hours=2))
    assert [e.payload["only_chat"] for e in later] == [OTHER]


def test_several_times_a_day_are_each_their_own_digest(store, config):
    add_match(store, 1, NOW + timedelta(hours=9))
    store.add_digest_time(CHAT, 12 * 60)
    assert [e.payload["at"] for e in due(store, config)] == ["09:00"]
    noon = due(store, config, NOW + timedelta(hours=3))
    assert [e.payload["at"] for e in noon] == ["12:00"]
    # Different keys, so the journal lets both through on the same day.
    assert due(store, config)[0].idempotency_key != noon[0].idempotency_key


def test_a_missed_slot_is_caught_up_but_not_forever(store, config):
    """A restart must not cost the morning digest; a container that was down
    all day must not deliver it at bedtime."""
    add_match(store, 1, NOW + timedelta(hours=9))
    assert len(due(store, config, NOW + timedelta(minutes=45))) == 1
    assert due(store, config, NOW + timedelta(hours=2)) == []


def test_the_key_carries_the_local_day_and_the_slot(store, config):
    add_match(store, 1, NOW + timedelta(hours=9))
    add_match(store, 2, NOW + timedelta(days=1, hours=9))
    today = due(store, config)[0].idempotency_key
    tomorrow = due(store, config, NOW + timedelta(days=1))[0].idempotency_key
    assert today == "E14:2026-09-05:0540"
    assert tomorrow == "E14:2026-09-06:0540"


def test_no_times_set_means_the_feature_is_off(storage, config):
    storage.add_subscriber(CHAT)
    storage.add_team(CHAT, TEAM_ID, "forze-reload", "FORZE Reload")
    add_match(storage, 1, NOW + timedelta(hours=9))
    assert dg.DigestScheduler(storage, config).due(NOW) == []


def test_an_unusable_timezone_skips_rather_than_guessing(store, config, caplog):
    """`/tz` validates what it stores, so this is the environment default being
    wrong. Falling back to UTC would send the digest at an hour nobody chose."""
    add_match(store, 1, NOW + timedelta(hours=9))
    store.set_subscriber_timezone(CHAT, "Mars/Olympus")
    assert due(store, config) == []


# ---------- what it counts ----------

def test_tomorrow_morning_is_inside_the_window(store, config):
    """The owner's own example: at nine in the morning, a match at seven
    tomorrow is 22 hours away and worth knowing about."""
    add_match(store, 1, NOW + timedelta(hours=22))
    assert len(due(store, config)) == 1


def test_beyond_twenty_four_hours_is_not(store, config):
    add_match(store, 1, NOW + timedelta(hours=25))
    assert due(store, config) == []


def test_a_match_being_played_is_left_out(store, config):
    """It is the one thing the owner cannot have missed: it was announced when
    it started, and its live card is at the bottom of the chat."""
    add_match(store, 1, NOW - timedelta(hours=1), state=MatchState.MAP_LIVE)
    assert due(store, config) == []


def test_a_finished_match_does_not(store, config):
    add_match(store, 1, NOW - timedelta(hours=3), state=MatchState.FINISHED)
    assert due(store, config) == []


def test_a_cancelled_match_does_not(store, config):
    add_match(store, 1, NOW + timedelta(hours=3), state=MatchState.CANCELLED)
    assert due(store, config) == []


def test_a_match_cancelled_but_still_dated_tomorrow_is_left_out(store, config):
    """The reason this is not `upcoming_matches` bounded at 24 h: that query
    judges by the time alone, and a cancelled match keeps its date."""
    add_match(store, 1, NOW + timedelta(hours=5), state=MatchState.CANCELLED)
    assert due(store, config) == []


def test_a_match_that_has_already_started_is_left_out_whatever_its_state(store, config):
    add_match(store, 1, NOW - timedelta(minutes=5))
    assert due(store, config) == []


def test_only_the_reader_s_own_teams(store, config):
    store.add_subscriber(OTHER)
    store.add_team(OTHER, 4608, "fnatic", "Fnatic")
    add_match(store, 1, NOW + timedelta(hours=5), team_id=4608, link=4608)
    store.add_digest_time(OTHER, NINE)
    events = due(store, config)
    assert [e.payload["only_chat"] for e in events] == [OTHER]


def test_the_reader_s_team_leads_the_line(store, config):
    """The match is stored from whoever saw it first; a digest is written for
    one chat, so it can be turned around here rather than at render time."""
    store.add_team(CHAT, 4608, "fnatic", "Fnatic")
    # Somebody else follows the team that saw the match first, so its name is
    # in the table without this reader following it.
    store.add_subscriber(OTHER)
    store.add_team(OTHER, 9999, "spirit", "Team Spirit")
    add_match(store, 1, NOW + timedelta(hours=5), team_id=9999,
              opponent_id=4608, opponent="Fnatic", link=9999)
    # Both sides are linked, which is what the schedule path always produces:
    # `canonical_team` refuses a perspective that is not among the players.
    store.link_match_team(1, 4608)
    one = due(store, config)[0].payload["matches"][0]
    assert one["team_name"] == "Fnatic"
    assert one["opponent"] == "Team Spirit"


# ---------- through the queue, not just out of the scheduler ----------

def test_the_digest_reaches_the_chat_it_was_built_for_and_no_other(store, config):
    """The seam the unit tests miss. E14 carries `match_id=None`, so
    `_recipients` takes the SERVICE audience — everyone listening — and narrows
    it by `only_chat`. Get that wrong and the digest either reaches nobody or
    reaches everybody; both look fine from inside the scheduler."""
    store.add_subscriber(OTHER)
    store.add_team(OTHER, TEAM_ID, "forze-reload", "FORZE Reload")
    add_match(store, 1, NOW + timedelta(hours=5))

    notifier = Notifier(store, config, telegram=None)
    assert notifier.enqueue(due(store, config)[0]) is True
    queued = [(row["chat_id"], row["event_type"]) for row in store.due_outbox(10)]
    assert queued == [(CHAT, "E14")]


def test_the_same_slot_is_not_queued_twice(store, config):
    """`due` keeps offering the slot for the whole catch-up hour — 120 ticks at
    30 seconds — and the journal is what makes that harmless."""
    add_match(store, 1, NOW + timedelta(hours=5))
    notifier = Notifier(store, config, telegram=None)
    event = due(store, config)[0]
    assert notifier.enqueue(event) is True
    assert notifier.enqueue(event) is False
    assert len(store.due_outbox(10)) == 1


def test_the_pause_silences_it_without_spending_the_key(store, config):
    """A pause means silence, not accumulation — but it must not burn the
    journal key either, or resuming inside the hour would leave the person with
    nothing where the digest should have been."""
    add_match(store, 1, NOW + timedelta(hours=5))
    notifier = Notifier(store, config, telegram=None)
    event = due(store, config)[0]

    store.set_subscriber_paused(CHAT, True)
    assert notifier.enqueue(event) is False
    assert store.due_outbox(10) == []

    store.set_subscriber_paused(CHAT, False)
    assert notifier.enqueue(event) is True


# ---------- reading the time a person typed ----------

@pytest.mark.parametrize("text,expected", [
    ("9", 540), ("09", 540), ("9:00", 540), ("09:00", 540),
    ("9:30", 570), ("21:30", 21 * 60 + 30), ("21.30", 21 * 60 + 30),
    ("0:00", 0), ("23:59", 23 * 60 + 59),
    ("24:00", None), ("9:60", None), ("nine", None), ("", None), ("-5", None),
])
def test_parse_time(text, expected):
    assert dg.parse_time(text) == expected


def test_clock_is_the_inverse():
    for minute in (0, 540, 570, 23 * 60 + 59):
        assert dg.parse_time(dg.clock(minute)) == minute


# ---------- how it reads ----------

def test_the_message_groups_by_the_reader_s_day(store, config):
    add_match(store, 1, NOW + timedelta(hours=2), opponent="Natus Vincere")
    add_match(store, 2, NOW + timedelta(hours=9), opponent="Fnatic",
              opponent_id=4608, event="BLAST Premier")
    add_match(store, 3, NOW + timedelta(hours=22), opponent="G2",
              opponent_id=6667, event="IEM Katowice")
    body = fmt.render(due(store, config)[0], team_name="FORZE Reload",
                      tz_name=MOSCOW)

    assert "3 matches in the next 24 hours" in body
    assert "<b>Today</b>" in body and "<b>Tomorrow</b>" in body
    assert "🕒 11:00" in body and "🕒 18:00" in body and "🕒 07:00" in body
    assert "FORZE Reload — Natus Vincere" in body
    assert "https://www.hltv.org/matches/2/x" in body


def test_one_match_is_not_pluralised(store, config):
    add_match(store, 1, NOW + timedelta(hours=9))
    body = fmt.render(due(store, config)[0], team_name="FORZE Reload",
                      tz_name=MOSCOW)
    assert "1 match in the next" in body


def test_the_days_are_anchored_to_when_the_digest_was_built(store, config):
    """The queue retries, and a 23:50 digest delivered after midnight must not
    relabel every "Today" in it as yesterday's date."""
    add_match(store, 1, NOW + timedelta(hours=9))
    event = due(store, config)[0]
    assert event.payload["now_utc"]
    body = fmt.render(event, team_name="FORZE Reload", tz_name=MOSCOW)
    assert "<b>Today</b>" in body


def test_a_tournament_name_cannot_smuggle_markup_into_the_message(store, config):
    add_match(store, 1, NOW + timedelta(hours=9), event="<b>ESL</b> & co")
    body = fmt.render(due(store, config)[0], team_name="FORZE Reload",
                      tz_name=MOSCOW)
    assert "&lt;b&gt;ESL&lt;/b&gt; &amp; co" in body


def test_the_team_comes_from_the_canonical_perspective_not_the_column(store, config):
    """`matches.team_id` is NULL for anything that entered outside the schedule
    path, and `team_name(None, ...)` falls back to the CONFIG's team — so the
    line would name the first seed for somebody else's match. Every machine
    here reads `canonical_team()` instead, and so must this."""
    store.upsert_match(
        match_id=1, opponent_id=13973, opponent_name="Color",
        event_name="ESL Pro League", start_utc=NOW + timedelta(hours=5),
        url="https://www.hltv.org/matches/1/x", snapshot={}, snapshot_hash="h")
    store.link_match_team(1, TEAM_ID)
    assert store.get_match(1)["team_id"] is None      # the column really is empty
    one = due(store, config)[0].payload["matches"][0]
    assert one["team_id"] == TEAM_ID
    assert one["team_name"] == "FORZE Reload"
