"""Per-subscriber settings: the thresholds that used to live only in `.env`.

Two things are being pinned down here. The easy one is that the bot can read
and write them. The hard one is the seam: an event is born ONCE for everybody
(the architecture's rule), so a threshold that differs per person has to be
applied at the moment the event is addressed, not where it is created — and
the machine has to build at the LOWEST bar in use, or the person who asked for
less would never get anything.
"""

import asyncio

from conftest import entry, later  # noqa: F401
from hltv_notify import menu, settings
from hltv_notify.models import Event
from hltv_notify.notify import format as fmt
from hltv_notify.notify.outbox import Notifier

CHAT = "555"
OTHER = "777"
TEAM_ID = 12857


def notifier(storage, config) -> Notifier:
    return Notifier(storage, config, telegram=None)


def make_match(storage) -> None:
    """A match linked to the tracked team — the audience is worked out from
    match_teams, so a match nobody is linked to reaches everybody."""
    storage.upsert_match(
        match_id=42, team_id=TEAM_ID, opponent_id=13973, opponent_name="Color",
        event_name="Test", start_utc=later(10),
        url="https://www.hltv.org/matches/42/x", snapshot={}, snapshot_hash="h")
    storage.link_match_team(42, TEAM_ID)


def multikill(kills: int = 4) -> Event:
    return Event(
        type="E9", idempotency_key=f"E9:42:r5:{kills}", match_id=42,
        payload={"kills": kills, "player": "sh1ro", "team_id": TEAM_ID,
                 "team_name": "FORZE Reload", "opponent": "Color",
                 "map_name": "Dust2", "round": 5,
                 "score_team": 3, "score_opponent": 2,
                 "url": "https://www.hltv.org/matches/42/x"})


# ---------- the registry ----------

def test_every_setting_takes_its_default_from_the_environment(config):
    values = settings.defaults(config)
    assert values["multikill"] == config.multikill_threshold
    assert values["comeback"] == config.comeback_rounds
    assert values["card"] == (1 if config.live_message else 0)
    assert set(values) == {item.name for item in settings.SETTINGS}


def test_turning_the_alerts_off_in_the_environment_reads_as_zero(monkeypatch):
    """MULTIKILL_ALERTS and MULTIKILL_THRESHOLD collapse into one number.

    Two knobs where a switch silently overrides a number is how someone ends
    up staring at a threshold of 4 wondering why nothing arrives.
    """
    from hltv_notify.config import Config

    monkeypatch.setenv("MULTIKILL_ALERTS", "false")
    monkeypatch.setenv("MULTIKILL_THRESHOLD", "4")
    assert settings.default_for(Config(), "multikill") == 0


def test_words_are_accepted_wherever_a_number_is(config):
    item = settings.get("multikill")
    assert settings.parse_value(item, "off") == 0
    assert settings.parse_value(item, "3") == 3
    assert settings.parse_value(item, "9") is None      # above the maximum
    assert settings.parse_value(item, "banana") is None
    half = settings.get("half")
    assert settings.parse_value(half, "on") == 1
    assert settings.parse_value(half, "off") == 0


# ---------- storage ----------

def test_absence_means_the_environment_still_decides(storage, config):
    """A row is written only once somebody changes something.

    Storing today's default instead would freeze it: raising the default in
    `.env` would then never reach anyone who had ever pressed "reset".
    """
    assert storage.setting(CHAT, "comeback", 9) == 9
    storage.set_setting(CHAT, "comeback", 12)
    assert storage.setting(CHAT, "comeback", 9) == 12
    storage.clear_setting(CHAT, "comeback")
    assert storage.setting(CHAT, "comeback", 6) == 6


def test_the_threshold_in_use_is_the_lowest_anybody_wants(storage, config):
    storage.add_subscriber(CHAT)
    storage.add_subscriber(OTHER)
    storage.set_setting(CHAT, "multikill", 5)
    storage.set_setting(OTHER, "multikill", 3)
    # Built at 3, or the person who asked for 3 would never get a 3k.
    assert storage.threshold_in_use("multikill", 4) == 3


def test_one_person_turning_it_off_does_not_switch_it_off_for_everybody(storage, config):
    storage.add_subscriber(CHAT)
    storage.add_subscriber(OTHER)
    storage.set_setting(CHAT, "multikill", 0)
    storage.set_setting(OTHER, "multikill", 4)
    assert storage.threshold_in_use("multikill", 4) == 4


def test_everybody_off_means_the_work_is_skipped(storage, config):
    storage.add_subscriber(CHAT)
    storage.set_setting(CHAT, "multikill", 0)
    assert storage.threshold_in_use("multikill", 4) == 0


def test_with_no_subscribers_at_all_the_environment_is_the_answer(storage, config):
    """Single-user mode: there is nobody to ask, so the config stands."""
    assert storage.threshold_in_use("multikill", 4) == 4


# ---------- the seam: one event, different thresholds ----------

def test_a_4k_reaches_the_person_who_asked_for_3_and_not_the_one_who_asked_for_5(
        storage, config):
    for chat in (CHAT, OTHER):
        storage.add_subscriber(chat)
        storage.add_team(chat, TEAM_ID, "forze-reload", "FORZE Reload")
    make_match(storage)
    storage.set_setting(CHAT, "multikill", 3)
    storage.set_setting(OTHER, "multikill", 5)

    assert notifier(storage, config).enqueue(multikill(4)) is True
    rows = [row["chat_id"] for row in storage.due_outbox(limit=10)]
    assert rows == [CHAT]


def test_an_event_below_every_threshold_goes_to_nobody(storage, config):
    storage.add_subscriber(CHAT)
    storage.add_team(CHAT, TEAM_ID, "forze-reload", "FORZE Reload")
    make_match(storage)
    storage.set_setting(CHAT, "multikill", 5)
    assert notifier(storage, config).enqueue(multikill(4)) is False
    assert storage.pending_count() == 0


def test_the_half_message_only_reaches_whoever_turned_it_on(storage, config):
    for chat in (CHAT, OTHER):
        storage.add_subscriber(chat)
        storage.add_team(chat, TEAM_ID, "forze-reload", "FORZE Reload")
    make_match(storage)
    storage.set_setting(CHAT, "half", 1)
    storage.set_setting(OTHER, "half", 0)

    event = Event(type="E12", idempotency_key="E12:42:map:1:half", match_id=42,
                  payload={"map_name": "Dust2", "map_number": 1, "overtime": 0,
                           "score_team": 6, "score_opponent": 6,
                           "opponent": "Color", "team_id": TEAM_ID,
                           "url": "https://www.hltv.org/matches/42/x"})
    assert notifier(storage, config).enqueue(event) is True
    assert [row["chat_id"] for row in storage.due_outbox(limit=10)] == [CHAT]


# ---------- the comeback line is decided per reader ----------

COMEBACK = {
    "map_name": "Dust2", "map_number": 1,
    "score_team": 13, "score_opponent": 11,
    "series_team": 1, "series_opponent": 0,
    "opponent": "Color", "team_id": TEAM_ID,
    "url": "https://www.hltv.org/matches/42/x", "event_name": "Test",
    "comeback_from_team": 3, "comeback_from_opponent": 11,
    "comeback_to_team": 13, "comeback_to_opponent": 11,
    "comeback_swing": 10, "comeback_result": "won", "comeback_overtime": False,
}


def test_the_same_map_says_comeback_to_one_reader_and_not_the_other():
    low = fmt.comeback_line(COMEBACK, team="FORZE", opponent="Color", threshold=9)
    high = fmt.comeback_line(COMEBACK, team="FORZE", opponent="Color", threshold=12)
    assert "Comeback" in low
    assert high == ""


def test_the_deficit_floor_moves_with_the_reader_s_bar():
    """A 13:1 win is a swing of twelve out of no hole at all.

    The floor is derived from the bar rather than being a second setting, so
    it has to be recomputed for the reader — otherwise someone who raised
    their bar would still be told about a "comeback from 0:1".
    """
    flat = dict(COMEBACK, comeback_from_team=0, comeback_from_opponent=1,
                comeback_to_team=13, comeback_to_opponent=1, comeback_swing=12)
    assert fmt.comeback_line(flat, team="FORZE", opponent="Color", threshold=9) == ""
    # A bar of 2 has a floor of 2, and 0:1 is a hole of one.
    assert fmt.comeback_line(flat, team="FORZE", opponent="Color", threshold=2) == ""


def test_no_bar_at_all_prints_whatever_the_payload_holds():
    """The renderer is also used where there is no recipient — the replay
    tool, the tests. None means "do not second-guess the payload"."""
    assert "Comeback" in fmt.comeback_line(COMEBACK, team="F", opponent="C")


def test_turning_comebacks_off_removes_the_line_but_keeps_the_map_message(
        storage, config):
    storage.add_subscriber(CHAT)
    storage.add_team(CHAT, TEAM_ID, "forze-reload", "FORZE Reload")
    make_match(storage)
    storage.set_setting(CHAT, "comeback", 0)
    event = Event(type="E6", idempotency_key="E6:42:map:1", match_id=42,
                  payload=dict(COMEBACK))
    assert notifier(storage, config).enqueue(event) is True
    body = storage.due_outbox(limit=1)[0]["body"]
    assert "Map 1 finished" in body
    assert "Comeback" not in body


# ---------- the live card ----------

def test_the_card_can_be_switched_off_for_one_person_only(storage, config):
    from hltv_notify.notify.live_message import LiveMessenger

    for chat in (CHAT, OTHER):
        storage.add_subscriber(chat)
        storage.add_team(chat, TEAM_ID, "forze-reload", "FORZE Reload")
    make_match(storage)
    storage.set_setting(OTHER, "card", 0)

    messenger = LiveMessenger(storage, config, telegram=None)
    assert [chat for chat, _ in messenger._recipients(42)] == [CHAT]


def test_the_environment_is_a_default_and_not_an_override(storage, monkeypatch):
    """With LIVE_MESSAGE=false a person who turns the card ON must get it.

    This was the trap in the original wiring: the module checked the config
    first and returned early, so no per-person value could ever win.
    """
    from hltv_notify.config import Config
    from hltv_notify.notify.live_message import LiveMessenger

    monkeypatch.setenv("LIVE_MESSAGE", "false")
    config = Config()
    storage.add_subscriber(CHAT)
    storage.add_team(CHAT, TEAM_ID, "forze-reload", "FORZE Reload")
    make_match(storage)
    storage.set_setting(CHAT, "card", 1)

    messenger = LiveMessenger(storage, config, telegram=None)
    assert [chat for chat, _ in messenger._recipients(42)] == [CHAT]


# ---------- the bot ----------

def test_the_buttons_offer_every_setting_in_the_registry(config):
    """The same rule as menu.MUTABLE: one list, or the buttons and the command
    drift apart."""
    keyboard = menu.settings_screen(settings.defaults(config))
    payloads = [button["callback_data"]
                for row in keyboard["inline_keyboard"] for button in row]
    for item in settings.SETTINGS:
        assert f"s:{item.name}" in payloads
        for preset in item.presets:
            assert f"s:{item.name}:{preset}" in payloads
