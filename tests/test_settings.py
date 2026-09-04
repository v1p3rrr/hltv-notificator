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


# ---------- what a setting may be given ----------

def test_a_threshold_the_service_would_ignore_is_refused(config):
    """MultikillTracker floors its threshold at 2.

    Accepting 1 stored a number the alerts never use and reported it back as
    if it were in force — and the refusal message even suggested it.
    """
    from hltv_notify.state.multikill import MultikillTracker

    item = settings.get("multikill")
    assert settings.parse_value(item, "1") is None
    assert settings.parse_value(item, "2") == 2
    assert settings.parse_value(item, "off") == 0        # zero still means off
    assert item.smallest_on == MultikillTracker(1).threshold
    assert settings.range_hint(item) == "off, or 2-5"


def test_clamping_never_turns_off_into_a_threshold(config):
    """`clamp` has to keep zero, or a button press meaning "off" would land on
    the smallest working value instead."""
    item = settings.get("multikill")
    assert item.clamp(0) == 0
    assert item.clamp(1) == 2
    assert item.clamp(99) == 5


def test_the_umbrella_variable_is_not_a_field(monkeypatch):
    """PHASE_ALERTS is resolved from the environment and has no field.

    A field nothing reads is a trap: `Config(phase_alerts=True)` would look
    like it turned both alerts on and would do nothing at all.
    """
    from hltv_notify.config import Config

    assert not hasattr(Config(), "phase_alerts")
    monkeypatch.setenv("PHASE_ALERTS", "true")
    both = Config()
    assert both.half_alerts and both.overtime_alerts
    monkeypatch.setenv("OVERTIME_ALERTS", "false")
    assert Config().half_alerts and not Config().overtime_alerts


# ---------- a setting whose value is not a number ----------

def test_a_language_list_round_trips_through_its_own_column(storage):
    storage.add_subscriber(CHAT)
    assert storage.text_setting(CHAT, "streams_langs", "en,ru") == "en,ru"
    storage.set_text_setting(CHAT, "streams_langs", "ru,pt")
    assert storage.text_setting(CHAT, "streams_langs", "en,ru") == "ru,pt"


def test_an_empty_language_list_is_a_value_and_not_an_absence(storage, config):
    """"any language" is something a person chose. Storing it as a deleted row
    would hand them back the service default instead."""
    storage.add_subscriber(CHAT)
    storage.set_text_setting(CHAT, "streams_langs", "")
    assert storage.text_setting(CHAT, "streams_langs", "en,ru") == ""
    storage.clear_setting(CHAT, "streams_langs")
    assert storage.text_setting(CHAT, "streams_langs", "en,ru") == "en,ru"


def test_the_two_writers_do_not_leave_each_other_behind(storage):
    """A non-NULL text_value is what marks a row as textual, so a numeric write
    has to blank it — otherwise settings_for would read the number back as a
    language list, and the other way round."""
    storage.add_subscriber(CHAT)
    storage.set_text_setting(CHAT, "streams_langs", "ru")
    storage.set_setting(CHAT, "streams_langs", 2)
    assert storage.text_setting(CHAT, "streams_langs", "en") == "en"

    storage.set_setting(CHAT, "streams_count", 4)
    storage.set_text_setting(CHAT, "streams_count", "ru")
    assert storage.setting(CHAT, "streams_count", 3) == 0


def test_settings_for_hands_back_both_kinds(storage, config):
    storage.add_subscriber(CHAT)
    storage.set_setting(CHAT, "streams_count", 2)
    storage.set_text_setting(CHAT, "streams_langs", "ru")
    values = storage.settings_for(CHAT, settings.defaults(config))
    assert values["streams_count"] == 2
    assert values["streams_langs"] == "ru"
    assert values["multikill"] == settings.default_for(config, "multikill")


# ---------- zero does not always mean off ----------

def test_zero_reads_as_the_word_the_setting_gave_it():
    """`streams_count` uses zero for "all". Reporting "off" there would be the
    setting describing something the code does not do — and there is a real off
    switch (`streams`) beside it."""
    assert settings.get("streams_count").describe(0) == "all"
    assert settings.get("multikill").describe(0) == "off"
    assert settings.range_hint(settings.get("streams_count")) == "all, or 1-6"


def test_the_word_off_is_refused_where_zero_does_not_mean_off():
    assert settings.parse_value(settings.get("streams_count"), "off") is None
    assert settings.parse_value(settings.get("streams_count"), "all") == 0
    assert settings.parse_value(settings.get("streams"), "off") == 0


# ---------- the on/off vocabulary is a vocabulary, not data ----------

def test_an_on_word_is_never_stored_as_a_language():
    """Two letters of the alphabet is all a language code has to look like, so
    "on" passes every shape check below. Stored as one it matches no flag, the
    filter falls back to "every broadcast in every language", and the reply
    reads like a confirmation that something was turned on."""
    item = settings.get("streams_langs")
    for word in ("on", "true", "yes"):
        assert settings.parse_value(item, word) is None
        assert settings.is_reset(item, word)


def test_an_off_word_for_a_list_is_the_empty_list():
    """With no language preferred the filter is simply off — which is a value,
    not an absence. "no" and "false" used to be stored as language codes."""
    item = settings.get("streams_langs")
    for word in ("off", "no", "none", "false", "any", "all"):
        assert settings.parse_value(item, word) == ""
        assert not settings.is_reset(item, word)


def test_a_number_still_takes_on_down_its_own_path():
    """`is_reset` must not swallow "on" for a number: there it goes through the
    -1 signal, which is what knows how to say "the default is itself off, so
    yours stays" instead of quietly deleting it."""
    item = settings.get("multikill")
    assert not settings.is_reset(item, "on")
    assert settings.parse_value(item, "on") == -1
    assert settings.is_reset(item, "default")
    assert settings.is_reset(settings.get("streams_langs"), "reset")


def test_the_words_come_from_one_list():
    """Same reasoning as menu.MUTABLE and bot.COMMANDS, one level down. A
    second copy drifts, and it drifts silently: a word that stops being
    recognised as a word is not refused, it is taken as data."""
    card = settings.get("card")
    for word in settings.ON_WORDS:
        assert settings.parse_value(card, word) == 1
    for word in settings.OFF_WORDS:
        assert settings.parse_value(card, word) == 0
    assert settings.ON_WORDS.isdisjoint(settings.OFF_WORDS)


def test_there_is_exactly_one_type_tag():
    """`kind` replaced a `boolean` flag rather than joining it: two flags could
    both be true, and every consumer would have to decide which wins."""
    for item in settings.SETTINGS:
        assert not hasattr(item, "boolean")
        assert item.kind in (settings.NUMBER, settings.BOOLEAN, settings.LANGUAGES)
        assert item.textual == (item.kind == settings.LANGUAGES)


def test_a_language_list_is_normalised_however_it_is_typed():
    item = settings.get("streams_langs")
    assert settings.parse_value(item, "EN , ru,en") == "en,ru"
    assert settings.parse_value(item, "any") == ""
    assert settings.parse_value(item, "3") is None
    assert item.describe("") == "any language"


# ---------- the buttons follow the registry ----------

def test_the_language_row_is_toggles_and_carries_the_person_s_own_codes(config):
    values = settings.defaults(config)
    values["streams_langs"] = "ru,kz"
    screen = menu.settings_screen(values)
    payloads = [button["callback_data"]
                for row in screen["inline_keyboard"] for button in row]
    assert "s:streams_langs:ru" in payloads
    assert "s:streams_langs:kz" in payloads      # not in the built-in row
    assert "s:streams_langs:en" in payloads
    # Telegram caps callback_data at 64 bytes.
    assert all(len(one.encode("utf-8")) <= 64 for one in payloads)


def test_every_setting_reaches_the_buttons(config):
    screen = menu.settings_screen(settings.defaults(config))
    payloads = [button["callback_data"]
                for row in screen["inline_keyboard"] for button in row]
    for item in settings.SETTINGS:
        assert any(one.startswith(f"s:{item.name}") for one in payloads), item.name
