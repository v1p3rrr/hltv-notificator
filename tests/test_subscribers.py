"""Несколько подписчиков: свои команды, свои глушения, свой разворот счёта."""

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


# ---------------------------------------------------------------- адресация


def test_event_reaches_only_those_who_track_a_participant(store, config):
    store.add_team(ILYA, MOUZ, "mouz", "MOUZ")
    store.link_match_team(MATCH, MOUZ)
    store.add_team(FRIEND, 1, "other", "Other")     # чужая команда

    Notifier(store, config, telegram=None).enqueue(e6())
    assert set(bodies(store)) == {ILYA}


def test_both_subscribers_of_the_same_team_get_it(store, config):
    for chat in (ILYA, FRIEND):
        store.add_team(chat, MOUZ, "mouz", "MOUZ")
    store.link_match_team(MATCH, MOUZ)

    Notifier(store, config, telegram=None).enqueue(e6())
    assert set(bodies(store)) == {ILYA, FRIEND}
    assert store.sent_event_count() == 2       # по одному ключу на адресата


def test_the_same_event_is_not_sent_twice_to_the_same_chat(store, config):
    store.add_team(ILYA, MOUZ, "mouz", "MOUZ")
    store.link_match_team(MATCH, MOUZ)

    n = Notifier(store, config, telegram=None)
    assert n.enqueue(e6()) is True
    assert n.enqueue(e6()) is False
    assert store.sent_event_count() == 1


# ---------------------------------------------------------------- разворот счёта


def test_opponent_follower_sees_the_score_flipped(store, config):
    """Событие ориентировано на каноническую команду. Тому, кто следит за её
    соперником, надо показать зеркальный счёт — иначе он увидит 13:10 там,
    где для него это 10:13."""
    store.add_team(ILYA, MOUZ, "mouz", "MOUZ")
    store.add_team(FRIEND, FORZE, "forze-reload", "FORZE Reload")
    store.link_match_team(MATCH, MOUZ)
    store.link_match_team(MATCH, FORZE)

    Notifier(store, config, telegram=None).enqueue(e6())
    texts = bodies(store)
    assert "13:10" in texts[ILYA] and "MOUZ" in texts[ILYA]
    assert "10:13" in texts[FRIEND] and "FORZE Reload" in texts[FRIEND]


# ---------------------------------------------------------------- глушение


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
    """Матч двух отслеживаемых команд: подписчик заглушил одну из них, но
    следит и за второй. Событие про матч он получить должен — иначе одна
    команда молча глушила бы уведомления про другую."""
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


# ---------------------------------------------------------------- мультикилл и служебное


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
    """«Сервис ослеп» касается всех, независимо от того, за кем они следят."""
    store.add_team(ILYA, MOUZ, "mouz", "MOUZ")
    store.add_team(FRIEND, 1, "other", "Other")

    event = Event(type="E8", idempotency_key="E8:schedule:down:x", match_id=None,
                  payload={"reason": "Расписание не читается", "detail": "таймаут"})
    Notifier(store, config, telegram=None).enqueue(event)
    assert set(bodies(store)) == {ILYA, FRIEND}


def test_disabled_subscriber_gets_nothing(store, config):
    store.add_team(ILYA, MOUZ, "mouz", "MOUZ")
    store.add_team(FRIEND, MOUZ, "mouz", "MOUZ")
    store.link_match_team(MATCH, MOUZ)
    store.set_subscriber_enabled(FRIEND, False)

    Notifier(store, config, telegram=None).enqueue(e6())
    assert set(bodies(store)) == {ILYA}


# ---------------------------------------------------------------- белый список


def test_whitelist_blocks_unknown_chats(config):
    assert config.chat_allowed(ILYA) is True
    assert config.chat_allowed(FRIEND) is True
    assert config.chat_allowed(STRANGER) is False


def test_whitelist_can_be_switched_off():
    open_config = Config(chat_id=ILYA, whitelist_only=False)
    assert open_config.chat_allowed(STRANGER) is True


def test_a_single_id_is_a_valid_list():
    """Один аккаунт — это просто список из одного id."""
    assert Config(chat_id=ILYA).chat_allowed(ILYA) is True
    assert Config(chat_id=ILYA).chat_allowed(FRIEND) is False


def test_single_user_mode_still_works(tmp_path):
    """Подписчиков нет вовсе — шлём в чат из конфига, как раньше."""
    storage = Storage(tmp_path / "single.db")
    storage.upsert_match(match_id=MATCH, opponent_id=1, opponent_name="X",
                         event_name="E", start_utc=utcnow(), url="u",
                         snapshot={}, snapshot_hash="h")
    cfg = Config(chat_id=ILYA)
    Notifier(storage, cfg, telegram=None).enqueue(e6())
    assert set(bodies(storage)) == {ILYA}
    storage.close()


# ---------------------------------------------------------------- один список чатов


def test_chat_ids_are_listed_through_commas():
    """Одна переменная, id через запятую. Точка с запятой и пробелы тоже."""
    cfg = Config(chat_id="111, 222;333")
    assert cfg.allowed_chat_ids() == ["111", "222", "333"]
    assert cfg.chat_allowed("333") is True


def test_main_chat_is_the_first_in_the_list():
    """Первый — основной: посев команды и одиночный режим адресуются ему."""
    assert Config(chat_id="111,222").main_chat_id == "111"


def test_empty_list_means_telegram_is_not_configured():
    assert Config(chat_id="", bot_token="t").telegram_enabled() is False
    assert Config(chat_id="111", bot_token="t").telegram_enabled() is True
    assert Config(chat_id="", bot_token="t").main_chat_id == ""


def test_duplicates_and_junk_are_dropped():
    cfg = Config(chat_id="111,111, @vasya ,-1001234567890")
    assert cfg.allowed_chat_ids() == ["111", "-1001234567890"]


# ---------------------------------------------------------------- пауза


def test_pause_covers_a_match_without_team_links(store, config):
    """Матч ещё не связан с командами — так выглядит база сразу после
    обновления. Раньше такое событие уходило владельцу из конфига напрямую,
    мимо и списка подписчиков, и паузы."""
    assert store.match_team_ids(MATCH) == []
    store.set_subscriber_paused(ILYA, True)
    Notifier(store, config, telegram=None).enqueue(e6())
    assert set(bodies(store)) == {FRIEND}
