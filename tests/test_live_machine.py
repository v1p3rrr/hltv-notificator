"""Переходы по кадрам живого фида: E5 и мгновенный E6."""

import pytest

from hltv_notify.sources.scorebot import LiveFrame, parse_scoreboard
from hltv_notify.state.db import utcnow
from hltv_notify.state.live_machine import LiveMachine, normalize_map_name

MATCH_ID = 777
TEAM_ID = 12857
FOE_ID = 13973


def add_match(storage, lineup=("Mirage", "Dust2", "Ancient")):
    storage.upsert_match(
        match_id=MATCH_ID, opponent_id=FOE_ID, opponent_name="Color",
        event_name="Test Event", start_utc=utcnow(),
        url=f"https://www.hltv.org/matches/{MATCH_ID}/x",
        snapshot={}, snapshot_hash="x")
    if lineup:
        storage.set_map_lineup(MATCH_ID, list(lineup))


def frame(map_name="de_mirage", *, ours=0, theirs=0, rnd=1, state="started",
          live=True, we_are_ct=True, regulation=12, overtime=3) -> LiveFrame:
    """Наша команда по умолчанию за CT. Стороны в фиде меняются после
    перерыва, поэтому счёт привязан к id, а не к стороне."""
    if we_are_ct:
        ct_id, ct_score, t_id, t_score = TEAM_ID, ours, FOE_ID, theirs
    else:
        ct_id, ct_score, t_id, t_score = FOE_ID, theirs, TEAM_ID, ours
    return LiveFrame(
        map_name=map_name, current_round=rnd, round_state=state, live=live,
        ct_team_id=ct_id, ct_team_name="CT", ct_score=ct_score,
        t_team_id=t_id, t_team_name="T", t_score=t_score,
        regulation=regulation, overtime=overtime)


# ---------------------------------------------------------------- имена карт

@pytest.mark.parametrize("raw,expected", [
    ("de_mirage", "Mirage"), ("de_nuke", "Nuke"), ("cs_office", "Office"),
    ("Mirage", "Mirage"), ("", ""),
])
def test_map_name_normalisation(raw, expected):
    """Фид даёт de_mirage, страница — Mirage. Сравнивать надо в одном виде,
    иначе смена источника выглядела бы как смена карты."""
    assert normalize_map_name(raw) == expected


# ---------------------------------------------------------------- E5

def test_e5_when_map_changes(storage, config):
    add_match(storage)
    m = LiveMachine(storage, config)
    m.apply(MATCH_ID, frame("de_mirage", rnd=1))
    m.apply(MATCH_ID, frame("de_mirage", ours=13, theirs=5, rnd=18))  # карта взята
    events = m.apply(MATCH_ID, frame("de_dust2", rnd=1, state="warmup", live=False))
    assert [e.type for e in events] == ["E5"]
    assert events[0].idempotency_key == "E5:777:map:2:started:Dust2"
    assert events[0].payload["series_team"] == 1


def test_no_e5_when_connecting_in_the_middle_of_a_map(storage, config):
    """Подключились посреди карты — объявлять «карта началась» поздно."""
    add_match(storage)
    m = LiveMachine(storage, config)
    assert m.apply(MATCH_ID, frame("de_mirage", ours=7, theirs=5, rnd=13)) == []


def test_e5_on_the_very_first_map_if_caught_from_the_start(storage, config):
    add_match(storage)
    m = LiveMachine(storage, config)
    events = m.apply(MATCH_ID, frame("de_mirage", rnd=1, state="warmup", live=False))
    assert [e.type for e in events] == ["E5"]
    assert events[0].payload["map_number"] == 1


def test_e5_not_repeated_on_every_frame(storage, config):
    """Кадр scoreboard приходит по нескольку раз в секунду."""
    add_match(storage)
    m = LiveMachine(storage, config)
    m.apply(MATCH_ID, frame("de_mirage", rnd=1))
    for _ in range(20):
        assert m.apply(MATCH_ID, frame("de_mirage", ours=3, theirs=2, rnd=6)) == []


# ---------------------------------------------------------------- E6

def test_e6_at_the_winning_round(storage, config):
    """Ради этого всё и делается: событие в момент победного раунда, а не
    когда HLTV обновит секцию карт."""
    add_match(storage)
    m = LiveMachine(storage, config)
    m.apply(MATCH_ID, frame("de_mirage", rnd=1))
    assert m.apply(MATCH_ID, frame("de_mirage", ours=12, theirs=9, rnd=22)) == []
    events = m.apply(MATCH_ID, frame("de_mirage", ours=13, theirs=9, rnd=22, state="ended"))
    assert [e.type for e in events] == ["E6"]
    assert events[0].idempotency_key == "E6:777:map:1:result:13-9"
    assert events[0].payload["overtime"] is False
    assert (events[0].payload["series_team"], events[0].payload["series_opponent"]) == (1, 0)


def test_e6_not_repeated_while_feed_keeps_sending_final_score(storage, config):
    add_match(storage)
    m = LiveMachine(storage, config)
    m.apply(MATCH_ID, frame("de_mirage", rnd=1))
    final = frame("de_mirage", ours=13, theirs=9, rnd=22, state="ended")
    assert [e.type for e in m.apply(MATCH_ID, final)] == ["E6"]
    for _ in range(10):
        assert m.apply(MATCH_ID, final) == []


def test_e6_survives_mr3_overtime(storage, config):
    """12:12 картой не считается, 16:14 — считается."""
    add_match(storage)
    m = LiveMachine(storage, config)
    m.apply(MATCH_ID, frame("de_mirage", rnd=1))
    assert m.apply(MATCH_ID, frame("de_mirage", ours=12, theirs=12, rnd=24)) == []
    assert m.apply(MATCH_ID, frame("de_mirage", ours=15, theirs=14, rnd=29)) == []
    events = m.apply(MATCH_ID, frame("de_mirage", ours=16, theirs=14, rnd=30, state="ended"))
    assert [e.type for e in events] == ["E6"]
    assert events[0].payload["overtime"] is True
    assert events[0].idempotency_key == "E6:777:map:1:result:16-14"


def test_e6_survives_second_overtime(storage, config):
    add_match(storage)
    m = LiveMachine(storage, config)
    m.apply(MATCH_ID, frame("de_mirage", rnd=1))
    assert m.apply(MATCH_ID, frame("de_mirage", ours=15, theirs=15, rnd=30)) == []
    events = m.apply(MATCH_ID, frame("de_mirage", ours=19, theirs=17, rnd=36))
    assert [e.type for e in events] == ["E6"]


def test_score_follows_the_team_not_the_side(storage, config):
    """После перерыва стороны меняются местами. Привязка к ctTeamId/tTeamId,
    а не к стороне, иначе счёт перевернётся на половине карты."""
    add_match(storage)
    m = LiveMachine(storage, config)
    m.apply(MATCH_ID, frame("de_mirage", rnd=1))
    events = m.apply(MATCH_ID, frame("de_mirage", ours=13, theirs=4, rnd=17, we_are_ct=False))
    assert (events[0].payload["score_team"], events[0].payload["score_opponent"]) == (13, 4)


# ---------------------------------------------------------------- защита

def test_frame_without_our_team_is_dropped(storage, config):
    add_match(storage)
    m = LiveMachine(storage, config)
    alien = LiveFrame(map_name="de_mirage", current_round=1, round_state="started",
                      live=True, ct_team_id=1, ct_team_name="A", ct_score=13,
                      t_team_id=2, t_team_name="B", t_score=3,
                      regulation=12, overtime=3)
    assert m.apply(MATCH_ID, alien) == []
    assert storage.map_results(MATCH_ID) == []


def test_empty_map_name_frame_is_ignored():
    """В переходных кадрах mapName бывает пустым — по нему нельзя объявлять
    начало карты."""
    assert parse_scoreboard({"mapName": "", "currentRound": 1}) is None
    assert parse_scoreboard({"currentRound": 1}) is None


def test_map_number_comes_from_the_page_lineup(storage, config):
    """Фид знает только название карты. Если бы номер считался как «записано
    плюс один», подключение посреди серии дало бы второй карте номер первой —
    страница обновляется с задержкой и могла ещё не записать первую."""
    add_match(storage, lineup=("Mirage", "Dust2", "Ancient"))
    m = LiveMachine(storage, config)
    events = m.apply(MATCH_ID, frame("de_dust2", rnd=1))
    assert events[0].payload["map_number"] == 2


def test_without_lineup_numbering_falls_back_to_counting(storage, config):
    add_match(storage, lineup=None)
    m = LiveMachine(storage, config)
    events = m.apply(MATCH_ID, frame("de_dust2", rnd=1))
    assert events[0].payload["map_number"] == 1
