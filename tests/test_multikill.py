"""Алерты о мультикиллах игроков отслеживаемой команды."""

import pytest

from hltv_notify.sources.scorebot import PlayerLine
from hltv_notify.state.multikill import MultikillTracker

MAP = "Mirage"


def players(**kills) -> list:
    return [PlayerLine(steam_id=nick, nick=nick, kills=value)
            for nick, value in kills.items()]


def tracker(threshold=4) -> MultikillTracker:
    return MultikillTracker(threshold)


def test_new_round_only_sets_the_baseline():
    t = tracker()
    assert t.observe(MAP, 5, "started", players(ropz=10, ZywOo=8)) == []


def test_four_kills_in_a_round_alert():
    t = tracker()
    t.observe(MAP, 5, "started", players(ropz=10, ZywOo=8))
    found = t.observe(MAP, 5, "started", players(ropz=14, ZywOo=8))
    assert [(p.nick, kills) for p, kills in found] == [("ropz", 4)]


def test_alert_fires_once_per_round():
    """Кадр приходит по нескольку раз в секунду — сообщать надо один раз."""
    t = tracker()
    t.observe(MAP, 5, "started", players(ropz=10))
    assert len(t.observe(MAP, 5, "started", players(ropz=14))) == 1
    for _ in range(10):
        assert t.observe(MAP, 5, "started", players(ropz=14)) == []


def test_ace_gets_its_own_alert():
    t = tracker()
    t.observe(MAP, 5, "started", players(ropz=10))
    assert len(t.observe(MAP, 5, "started", players(ropz=14))) == 1
    found = t.observe(MAP, 5, "started", players(ropz=15))
    assert [(p.nick, kills) for p, kills in found] == [("ropz", 5)]


def test_jump_straight_to_ace_alerts_once():
    """Между двумя кадрами игрок мог взять сразу пять — сообщение одно."""
    t = tracker()
    t.observe(MAP, 5, "started", players(ropz=10))
    found = t.observe(MAP, 5, "started", players(ropz=15))
    assert [(p.nick, kills) for p, kills in found] == [("ropz", 5)]


def test_three_kills_are_not_reported():
    t = tracker()
    t.observe(MAP, 5, "started", players(ropz=10))
    assert t.observe(MAP, 5, "started", players(ropz=13)) == []


def test_kills_do_not_leak_between_rounds():
    """Фраги в кадре накоплены ЗА КАРТУ, поэтому без сброса базы каждый
    следующий раунд выглядел бы как мультикилл."""
    t = tracker()
    t.observe(MAP, 5, "started", players(ropz=10))
    t.observe(MAP, 5, "started", players(ropz=14))
    t.observe(MAP, 6, "freezePeriod", players(ropz=14))     # новый раунд
    assert t.observe(MAP, 6, "started", players(ropz=16)) == []
    assert len(t.observe(MAP, 6, "started", players(ropz=18))) == 1


def test_new_map_resets_everything():
    t = tracker()
    t.observe(MAP, 5, "started", players(ropz=10))
    t.observe("Nuke", 1, "started", players(ropz=0))
    assert t.observe("Nuke", 1, "started", players(ropz=3)) == []


def test_warmup_kills_are_ignored():
    """В разминке идёт дезматч, и фраги оттуда к раунду отношения не имеют."""
    t = tracker()
    t.observe("Nuke", 1, "warmup", players(ropz=0))
    assert t.observe("Nuke", 1, "warmup", players(ropz=25)) == []
    # после разминки счёт мультикиллов идёт от актуальной базы
    assert t.observe("Nuke", 1, "started", players(ropz=27)) == []


def test_player_appearing_mid_round_is_not_credited():
    """Замена или переподключение не должны выглядеть как мультикилл."""
    t = tracker()
    t.observe(MAP, 5, "started", players(ropz=10))
    assert t.observe(MAP, 5, "started", players(ropz=10, newcomer=30)) == []
    assert t.observe(MAP, 5, "started", players(ropz=10, newcomer=34))[0][0].nick == "newcomer"


def test_threshold_is_configurable():
    t = tracker(threshold=3)
    t.observe(MAP, 5, "started", players(ropz=10))
    assert len(t.observe(MAP, 5, "started", players(ropz=13))) == 1


def test_threshold_has_a_sane_floor():
    """Порог 1 превратил бы алерт в поток."""
    assert tracker(threshold=1).threshold == 2
    assert tracker(threshold=0).levels == [2, 5]


def test_reconnect_can_miss_but_never_invents():
    """После реконнекта посреди раунда база берётся заново. Мультикилл может
    быть пропущен — это осознанный размен, зато ложных алертов не бывает."""
    t = tracker()
    t.observe(MAP, 5, "started", players(ropz=10))
    t.observe(MAP, 5, "started", players(ropz=13))
    fresh = tracker()                       # как будто воркер пересоздали
    fresh.observe(MAP, 5, "started", players(ropz=13))
    assert fresh.observe(MAP, 5, "started", players(ropz=14)) == []
