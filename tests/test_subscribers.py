"""Several subscribers: own teams, own mutes, own orientation of the score."""

import pytest

from hltv_notify.config import Config
from hltv_notify.models import Event
from hltv_notify.notify.outbox import Notifier
from hltv_notify.state.db import Storage, utcnow

ILYA = "111"
FRIEND = "222"
STRANGER = "999"

MOUZ = 4494
FORZE = 12857
MATCH = 700


@pytest.fixture()
def config():
    return Config(chat_id=f"{ILYA},{FRIEND}", bot_token="t")


@pytest.fixture()
def store(tmp_path, config):
    storage = Storage(tmp_path / "subs.db")
    storage.add_subscriber(ILYA)
    storage.add_subscriber(FRIEND)
    storage.upsert_match(
        match_id=MATCH, team_id=MOUZ, opponent_id=FORZE, opponent_name="FORZE Reload",
        event_name="Major", start_utc=utcnow(), url="https://www.hltv.org/matches/700/x",
        snapshot={}, snapshot_hash="h")
    yield storage
    storage.close()


def e6(score=(13, 10)) -> Event:
    return Event(
        type="E6", idempotency_key=f"E6:{MATCH}:map:1:result:{score[0]}-{score[1]}",
        match_id=MATCH,
        payload={"team_id": MOUZ, "team_name": "MOUZ",
                 "opponent_id": FORZE, "opponent": "FORZE Reload",
                 "map_number": 1, "map_name": "Nuke",
                 "score_team": score[0], "score_opponent": score[1],
                 "series_team": 1, "series_opponent": 0,
                 "event_name": "Major", "url": "u"})


def bodies(storage):
    return {row["chat_id"]: row["body"] for row in storage.due_outbox(limit=50)}


# ---------------------------------------------------------------- addressing


def test_event_reaches_only_those_who_track_a_participant(store, config):
    store.add_team(ILYA, MOUZ, "mouz", "MOUZ")
    store.link_match_team(MATCH, MOUZ)
    store.add_team(FRIEND, 1, "other", "Other")     # someone else's team

    Notifier(store, config, telegram=None).enqueue(e6())
    assert set(bodies(store)) == {ILYA}


def test_both_subscribers_of_the_same_team_get_it(store, config):
    for chat in (ILYA, FRIEND):
        store.add_team(chat, MOUZ, "mouz", "MOUZ")
    store.link_match_team(MATCH, MOUZ)

    Notifier(store, config, telegram=None).enqueue(e6())
    assert set(bodies(store)) == {ILYA, FRIEND}
    assert store.sent_event_count() == 2       # one key per recipient


def test_the_same_event_is_not_sent_twice_to_the_same_chat(store, config):
    store.add_team(ILYA, MOUZ, "mouz", "MOUZ")
    store.link_match_team(MATCH, MOUZ)

    n = Notifier(store, config, telegram=None)
    assert n.enqueue(e6()) is True
    assert n.enqueue(e6()) is False
    assert store.sent_event_count() == 1


# ---------------------------------------------------------------- score orientation


def test_opponent_follower_sees_the_score_flipped(store, config):
    """The event is oriented on the canonical team. Someone following its
    opponent must be shown the mirrored score — otherwise they read 13:10
    where for them it is 10:13."""
    store.add_team(ILYA, MOUZ, "mouz", "MOUZ")
    store.add_team(FRIEND, FORZE, "forze-reload", "FORZE Reload")
    store.link_match_team(MATCH, MOUZ)
    store.link_match_team(MATCH, FORZE)

    Notifier(store, config, telegram=None).enqueue(e6())
    texts = bodies(store)
    assert "13:10" in texts[ILYA] and "MOUZ" in texts[ILYA]
    assert "10:13" in texts[FRIEND] and "FORZE Reload" in texts[FRIEND]


# ---------------------------------------------------------------- muting


def test_muted_type_does_not_reach_that_subscriber(store, config):
    for chat in (ILYA, FRIEND):
        store.add_team(chat, MOUZ, "mouz", "MOUZ")
    store.link_match_team(MATCH, MOUZ)
    store.set_team_mutes(FRIEND, MOUZ, ["E6"])

    Notifier(store, config, telegram=None).enqueue(e6())
    assert set(bodies(store)) == {ILYA}


def test_other_types_still_reach_a_partially_muted_subscriber(store, config):
    store.add_team(FRIEND, MOUZ, "mouz", "MOUZ")
    store.link_match_team(MATCH, MOUZ)
    store.set_team_mutes(FRIEND, MOUZ, ["E9", "E5"])

    Notifier(store, config, telegram=None).enqueue(e6())
    assert set(bodies(store)) == {FRIEND}


def test_one_muted_team_does_not_silence_the_other(store, config):
    """A match between two tracked teams: the subscriber muted one of them but
    follows the second as well. They must still receive the event — otherwise
    one team would silently mute notifications about the other."""
    store.add_team(ILYA, MOUZ, "mouz", "MOUZ")
    store.add_team(ILYA, FORZE, "forze-reload", "FORZE Reload")
    store.link_match_team(MATCH, MOUZ)
    store.link_match_team(MATCH, FORZE)
    store.set_team_mutes(ILYA, MOUZ, ["E6"])

    Notifier(store, config, telegram=None).enqueue(e6())
    assert set(bodies(store)) == {ILYA}


def test_muting_both_teams_does_silence_the_match(store, config):
    store.add_team(ILYA, MOUZ, "mouz", "MOUZ")
    store.add_team(ILYA, FORZE, "forze-reload", "FORZE Reload")
    store.link_match_team(MATCH, MOUZ)
    store.link_match_team(MATCH, FORZE)
    for team in (MOUZ, FORZE):
        store.set_team_mutes(ILYA, team, ["E6"])

    assert Notifier(store, config, telegram=None).enqueue(e6()) is False
    assert store.pending_count() == 0


# ---------------------------------------------------------------- multikill and service


def test_multikill_reaches_only_that_players_team(store, config):
    store.add_team(ILYA, MOUZ, "mouz", "MOUZ")
    store.add_team(FRIEND, FORZE, "forze-reload", "FORZE Reload")
    store.link_match_team(MATCH, MOUZ)
    store.link_match_team(MATCH, FORZE)

    event = Event(type="E9", idempotency_key="E9:700:map:1:round:5:sid:4", match_id=MATCH,
                  payload={"team_id": MOUZ, "team_name": "MOUZ", "opponent": "FORZE Reload",
                           "nick": "Spinx", "kills": 4, "map_name": "Nuke", "round": 5,
                           "score_team": 5, "score_opponent": 3, "url": "u"})
    Notifier(store, config, telegram=None).enqueue(event)
    assert set(bodies(store)) == {ILYA}


def test_service_alerts_go_to_everyone(store, config):
    """"The service has gone blind" concerns everyone, whoever they follow."""
    store.add_team(ILYA, MOUZ, "mouz", "MOUZ")
    store.add_team(FRIEND, 1, "other", "Other")

    event = Event(type="E8", idempotency_key="E8:schedule:down:x", match_id=None,
                  payload={"reason": "The schedule cannot be read", "detail": "timeout"})
    Notifier(store, config, telegram=None).enqueue(event)
    assert set(bodies(store)) == {ILYA, FRIEND}


def test_disabled_subscriber_gets_nothing(store, config):
    store.add_team(ILYA, MOUZ, "mouz", "MOUZ")
    store.add_team(FRIEND, MOUZ, "mouz", "MOUZ")
    store.link_match_team(MATCH, MOUZ)
    store.set_subscriber_enabled(FRIEND, False)

    Notifier(store, config, telegram=None).enqueue(e6())
    assert set(bodies(store)) == {ILYA}


# ---------------------------------------------------------------- the whitelist


def test_whitelist_blocks_unknown_chats(config):
    assert config.chat_allowed(ILYA) is True
    assert config.chat_allowed(FRIEND) is True
    assert config.chat_allowed(STRANGER) is False


def test_whitelist_can_be_switched_off():
    open_config = Config(chat_id=ILYA, whitelist_only=False)
    assert open_config.chat_allowed(STRANGER) is True


def test_a_single_id_is_a_valid_list():
    """A single account is simply a list of one id."""
    assert Config(chat_id=ILYA).chat_allowed(ILYA) is True
    assert Config(chat_id=ILYA).chat_allowed(FRIEND) is False


def test_single_user_mode_still_works(tmp_path):
    """There are no subscribers at all — send to the chat from the config, as
    before."""
    storage = Storage(tmp_path / "single.db")
    storage.upsert_match(match_id=MATCH, opponent_id=1, opponent_name="X",
                         event_name="E", start_utc=utcnow(), url="u",
                         snapshot={}, snapshot_hash="h")
    cfg = Config(chat_id=ILYA)
    Notifier(storage, cfg, telegram=None).enqueue(e6())
    assert set(bodies(storage)) == {ILYA}
    storage.close()


# ---------------------------------------------------------------- one chat list


def test_chat_ids_are_listed_through_commas():
    """One variable, ids separated by commas. Semicolons and spaces too."""
    cfg = Config(chat_id="111, 222;333")
    assert cfg.allowed_chat_ids() == ["111", "222", "333"]
    assert cfg.chat_allowed("333") is True


def test_main_chat_is_the_first_in_the_list():
    """The first is the main one: the team seed and single-user mode go there."""
    assert Config(chat_id="111,222").main_chat_id == "111"


def test_empty_list_means_telegram_is_not_configured():
    assert Config(chat_id="", bot_token="t").telegram_enabled() is False
    assert Config(chat_id="111", bot_token="t").telegram_enabled() is True
    assert Config(chat_id="", bot_token="t").main_chat_id == ""


def test_duplicates_and_junk_are_dropped():
    cfg = Config(chat_id="111,111, @vasya ,-1001234567890")
    assert cfg.allowed_chat_ids() == ["111", "-1001234567890"]


# ---------------------------------------------------------------- pause


def test_pause_covers_a_match_without_team_links(store, config):
    """The match is not linked to any team yet — that is what the database
    looks like right after an upgrade. Such an event used to go to the config
    owner directly, past both the subscriber list and the pause."""
    assert store.match_team_ids(MATCH) == []
    store.set_subscriber_paused(ILYA, True)
    Notifier(store, config, telegram=None).enqueue(e6())
    assert set(bodies(store)) == {FRIEND}


# ---------------------------------------------------------------- revoking access


def test_removing_a_chat_from_the_whitelist_stops_delivery(tmp_path):
    """The whitelist only closed the way in. Removing a chat from
    TELEGRAM_CHAT_ID took away control of the bot but did not unsubscribe it
    from notifications: delivery went by the subscribers table, which knows
    nothing about the config."""
    from hltv_notify.__main__ import _revoke_removed_subscribers

    storage = Storage(tmp_path / "revoke.db")
    storage.add_subscriber(ILYA)
    storage.add_subscriber(FRIEND)

    _revoke_removed_subscribers(storage, Config(chat_id=ILYA))   # FRIEND removed

    assert storage.subscriber_ids() == [ILYA]
    assert storage.get_subscriber(FRIEND)["enabled"] == 0
    storage.close()


def test_open_mode_and_empty_list_revoke_nobody(tmp_path):
    """In open mode there is no list at all, and an empty list is almost
    certainly an unfinished .env rather than an intent to unsubscribe everyone."""
    from hltv_notify.__main__ import _revoke_removed_subscribers

    storage = Storage(tmp_path / "revoke2.db")
    storage.add_subscriber(ILYA)
    storage.add_subscriber(FRIEND)

    _revoke_removed_subscribers(storage, Config(chat_id="", whitelist_only=False))
    _revoke_removed_subscribers(storage, Config(chat_id=""))
    assert sorted(storage.subscriber_ids()) == [ILYA, FRIEND]
    storage.close()


def test_returning_a_chat_to_the_whitelist_restores_it(tmp_path):
    from hltv_notify.__main__ import _revoke_removed_subscribers

    storage = Storage(tmp_path / "revoke3.db")
    storage.add_subscriber(ILYA)
    storage.add_subscriber(FRIEND)
    _revoke_removed_subscribers(storage, Config(chat_id=ILYA))
    assert storage.subscriber_ids() == [ILYA]

    storage.add_subscriber(FRIEND)          # added back — add_subscriber re-enables
    assert sorted(storage.subscriber_ids()) == [ILYA, FRIEND]
    storage.close()
