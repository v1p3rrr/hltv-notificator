"""Несколько отслеживаемых команд.

Главная опасность — матч, в котором отслеживаемые команды играют ДРУГ ПРОТИВ
ДРУГА. Матч один, значит и уведомления о нём должны прийти по одному разу,
несмотря на то что каждая команда видит его со своей стороны. Исключение —
мультикиллы: четвёрка игрока каждой команды это отдельный хайлайт.
"""

from datetime import datetime, timedelta, timezone

import pytest

CHAT = "555"

from conftest import later
from hltv_notify.models import Event, ScheduleEntry
from hltv_notify.notify.outbox import Notifier
from hltv_notify.sources.scorebot import LiveFrame, PlayerLine
from hltv_notify.state.db import utcnow
from hltv_notify.state.live_machine import LiveMachine
from hltv_notify.state.machine import ScheduleMachine

ALPHA = 4494        # меньший id — станет канонической перспективой
BETA = 12857
MATCH = 500


@pytest.fixture()
def both(storage):
    storage.add_team(CHAT, ALPHA, "mouz", "MOUZ")
    storage.add_team(CHAT, BETA, "forze-reload", "FORZE Reload")
    return storage


def entry_for(team_id, *, start=None, match_id=MATCH):
    """Одна и та же встреча глазами каждой из команд."""
    opponent = BETA if team_id == ALPHA else ALPHA
    names = {ALPHA: "MOUZ", BETA: "FORZE Reload"}
    return ScheduleEntry(
        match_id=match_id, start_utc=start or later(600),
        opponent_id=opponent, opponent_name=names[opponent],
        event_name="Superduper Major", url=f"https://www.hltv.org/matches/{match_id}/x",
        finished=False)


def frame(*, alpha_score=0, beta_score=0, rnd=1, alpha_kills=(), beta_kills=()):
    def players(kills):
        return tuple(PlayerLine(steam_id=nick, nick=nick, kills=value)
                     for nick, value in kills)
    return LiveFrame(
        map_name="de_mirage", current_round=rnd, round_state="started", live=True,
        ct_team_id=ALPHA, ct_team_name="MOUZ", ct_score=alpha_score,
        t_team_id=BETA, t_team_name="FORZE Reload", t_score=beta_score,
        regulation=12, overtime=3,
        ct_players=players(alpha_kills), t_players=players(beta_kills))


# ----------------------------------------------------------------------


def test_match_of_two_tracked_teams_is_linked_to_both(both, config):
    m = ScheduleMachine(both, config)
    m.apply([entry_for(ALPHA)], ALPHA)
    m.apply([entry_for(BETA)], BETA)
    assert both.match_team_ids(MATCH) == [ALPHA, BETA]


def test_canonical_perspective_is_the_smaller_id(both, config):
    m = ScheduleMachine(both, config)
    m.apply([entry_for(ALPHA)], ALPHA)
    m.apply([entry_for(BETA)], BETA)
    assert both.canonical_team(MATCH) == ALPHA


def test_canonical_perspective_never_flips(both, config):
    """Даже если первой матч увидела команда с БОЛЬШИМ id, перспектива, раз
    выбранная, не меняется: иначе ключи идемпотентности стали бы зеркальными
    и всё уже отправленное разослалось бы заново.

    Проверять надо ИМЕННО canonical_team: по нему работают все машины. Пока
    здесь стоял только matches.team_id, тест был зелёным, а перспектива при
    этом переворачивалась.
    """
    m = ScheduleMachine(both, config)
    m.apply([entry_for(BETA)], BETA)          # первой пришла BETA
    assert both.get_match(MATCH)["team_id"] == BETA
    assert both.canonical_team(MATCH) == BETA

    m.apply([entry_for(ALPHA)], ALPHA)        # затем ALPHA с меньшим id
    assert both.get_match(MATCH)["team_id"] == BETA
    assert both.canonical_team(MATCH) == BETA


def test_adding_a_smaller_id_team_mid_match_keeps_the_score_orientation(both, config):
    """Команду добавляют через бота посреди идущего матча — и она оказывается
    соперником. Счёт по уже сыгранным картам не должен поменяться местами."""
    m = ScheduleMachine(both, config)
    m.apply([entry_for(BETA)], BETA)
    live = LiveMachine(both, config)
    live.apply(MATCH, frame(rnd=1))
    live.apply(MATCH, frame(alpha_score=7, beta_score=13, rnd=20))
    before = both.map_results(MATCH)[0]
    assert (before["score_team"], before["score_opponent"]) == (13, 7)   # глазами BETA

    m.apply([entry_for(ALPHA)], ALPHA)        # ALPHA с меньшим id — уже по ходу матча
    assert both.canonical_team(MATCH) == BETA
    after = both.map_results(MATCH)[0]
    assert (after["score_team"], after["score_opponent"]) == (13, 7)


def test_multikill_of_the_second_team_is_shown_from_its_own_side(both, config):
    """Событие про игрока BETA обязано быть целиком от лица BETA.

    Если взять контекст канонической команды и подменить только имя, соперником
    окажется она же сама («FORZE — FORZE»), а счёт останется перевёрнутым.
    """
    m = ScheduleMachine(both, config)
    m.apply([entry_for(ALPHA)], ALPHA)
    m.apply([entry_for(BETA)], BETA)
    assert both.canonical_team(MATCH) == ALPHA

    live = LiveMachine(both, config)
    live.apply(MATCH, frame(rnd=9, alpha_score=7, beta_score=4, beta_kills=[("Kaide", 2)]))
    events = live.apply(MATCH, frame(rnd=9, alpha_score=7, beta_score=4,
                                     beta_kills=[("Kaide", 6)]))
    e9 = next(e for e in events if e.type == "E9")
    assert e9.payload["team_id"] == BETA
    assert e9.payload["opponent_id"] == ALPHA
    assert e9.payload["opponent"] == "MOUZ"
    # счёт глазами BETA: она проигрывает 4:7, а не ведёт 7:4
    assert (e9.payload["score_team"], e9.payload["score_opponent"]) == (4, 7)


def test_one_new_match_notification_for_both_teams(both, config):
    """E1 про общий матч приходит один раз, а не по разу на команду."""
    m = ScheduleMachine(both, config)
    n = Notifier(both, config, telegram=None)

    # обе команды уже под наблюдением, обе видят НОВЫЙ матч
    m.apply([], ALPHA)
    m.apply([], BETA)
    for team in (ALPHA, BETA):
        for event in m.apply([entry_for(team)], team):
            n.enqueue(event)

    assert both.sent_event_count() == 1
    assert both.pending_count() == 1


def test_adding_a_team_later_is_silent(both, config):
    """Команду добавляют через бота посреди работы, и у неё найдётся полтора
    десятка матчей. Прилететь E1 по каждому из них не должно."""
    m = ScheduleMachine(both, config)
    m.apply([], ALPHA)                              # ALPHA уже наблюдается

    schedule = [entry_for(BETA, match_id=600 + i) for i in range(5)]
    assert m.apply(schedule, BETA) == []             # первый прогон новой команды
    assert len(both.all_matches()) == 5


def test_one_team_page_does_not_cancel_another_teams_match(both, config):
    """Матч команды ALPHA не должен считаться исчезнувшим лишь потому, что
    его нет на странице команды BETA."""
    m = ScheduleMachine(both, config)
    m.apply([entry_for(ALPHA, match_id=700)], ALPHA)
    m.apply([], BETA)

    events = m.apply([], BETA)                       # у BETA пусто — и это норма
    assert events == []
    assert both.get_match(700)["missing_since_utc"] is None


# ----------------------------------------------------------------------
# Мультикиллы — наоборот, от обеих команд
# ----------------------------------------------------------------------


def test_multikill_alerts_come_from_both_tracked_teams(both, config):
    m = ScheduleMachine(both, config)
    m.apply([entry_for(ALPHA)], ALPHA)
    m.apply([entry_for(BETA)], BETA)

    live = LiveMachine(both, config)
    live.apply(MATCH, frame(rnd=5, alpha_kills=[("Spinx", 10)], beta_kills=[("Kaide", 8)]))
    events = live.apply(MATCH, frame(rnd=5, alpha_kills=[("Spinx", 14)],
                                     beta_kills=[("Kaide", 12)]))

    nicks = sorted(e.payload["nick"] for e in events if e.type == "E9")
    assert nicks == ["Kaide", "Spinx"]
    teams = {e.payload["nick"]: e.payload["team_name"] for e in events if e.type == "E9"}
    assert teams == {"Spinx": "MOUZ", "Kaide": "FORZE Reload"}


def test_multikill_keys_do_not_collide_between_teams(both, config):
    m = ScheduleMachine(both, config)
    m.apply([entry_for(ALPHA)], ALPHA)
    m.apply([entry_for(BETA)], BETA)

    live = LiveMachine(both, config)
    n = Notifier(both, config, telegram=None)
    live.apply(MATCH, frame(rnd=5, alpha_kills=[("Spinx", 10)], beta_kills=[("Kaide", 8)]))
    for event in live.apply(MATCH, frame(rnd=5, alpha_kills=[("Spinx", 14)],
                                         beta_kills=[("Kaide", 12)])):
        n.enqueue(event)

    assert both.sent_event_count() == 2      # два игрока, два разных ключа


def test_map_end_of_a_shared_match_notifies_once(both, config):
    """Конец карты в матче двух отслеживаемых команд — одно уведомление."""
    m = ScheduleMachine(both, config)
    m.apply([entry_for(ALPHA)], ALPHA)
    m.apply([entry_for(BETA)], BETA)

    live = LiveMachine(both, config)
    n = Notifier(both, config, telegram=None)
    live.apply(MATCH, frame(rnd=1))
    for event in live.apply(MATCH, frame(alpha_score=13, beta_score=7, rnd=20)):
        n.enqueue(event)

    keys = [row["idempotency_key"] for row in both.due_outbox(limit=50)]
    e6 = [k for k in keys if k.startswith("E6:")]
    assert len(e6) == 1
    # счёт ориентирован на каноническую команду (ALPHA), а не на обе сразу
    assert e6[0] == "E6:500:map:1:result:13-7"
