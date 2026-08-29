"""Страница матча и живой фид на ОДНОМ матче.

Обе машины по отдельности покрыты тестами, и обе по отдельности вели себя
правильно. Два дефекта жили именно на стыке: машины делили одно поле состояния
и делали по нему выводы о собственной истории. Этот файл закрывает стык.
"""

from datetime import timedelta

import pytest

from conftest import FIXTURES, TEAM_ID
from hltv_notify.sources import match_page
from hltv_notify.sources.scorebot import LiveFrame
from hltv_notify.state.db import Storage, utcnow
from hltv_notify.state.live_machine import LiveMachine
from hltv_notify.state.match_machine import MatchMachine

MATCH_ID = 2397053
FOE_ID = 13973


@pytest.fixture()
def match(storage):
    storage.upsert_match(
        match_id=MATCH_ID, opponent_id=FOE_ID, opponent_name="Color",
        event_name="GLuck Qualifier", start_utc=utcnow() - timedelta(minutes=30),
        url="https://www.hltv.org/matches/2397053/x", snapshot={}, snapshot_hash="h")
    storage.set_map_lineup(MATCH_ID, ["Mirage", "Dust2", "Ancient"])
    return storage


def page(name: str):
    return match_page.parse((FIXTURES / name).read_text(encoding="utf-8"), MATCH_ID)


def frame(map_name, *, ours=0, theirs=0, rnd=1, state="started", live=True):
    return LiveFrame(
        map_name=map_name, current_round=rnd, round_state=state, live=live,
        ct_team_id=TEAM_ID, ct_team_name="FORZE Reload", ct_score=ours,
        t_team_id=FOE_ID, t_team_name="Color", t_score=theirs,
        regulation=12, overtime=3)


# ----------------------------------------------------------------------
# E5: страница не должна «съедать» начало карты
# ----------------------------------------------------------------------


def test_page_poll_does_not_swallow_map_start(match, config):
    """Опрос страницы кладёт в состояние ПРЕДСТОЯЩУЮ карту (первую
    несыгранную). Живая машина не должна принимать это за «карта уже была»."""
    MatchMachine(match, config).apply(page("match-2397053-live.html"))
    assert match.get_state(MATCH_ID)["current_map_name"] == "Dust2"

    events = LiveMachine(match, config).apply(MATCH_ID, frame("de_dust2", rnd=1))
    assert [e.type for e in events] == ["E5"]
    assert events[0].payload["map_name"] == "Dust2"


def test_map_start_survives_repeated_page_polls(match, config):
    """Опрос страницы идёт каждую минуту всё время, пока идёт карта, и не
    должен ломать признак начала следующей."""
    page_machine = MatchMachine(match, config)
    live_machine = LiveMachine(match, config)
    live = page("match-2397053-live.html")

    page_machine.apply(live)
    assert [e.type for e in live_machine.apply(MATCH_ID, frame("de_dust2", rnd=1))] == ["E5"]

    page_machine.apply(live)          # страница снова говорит про Dust2
    later = live_machine.apply(MATCH_ID, frame("de_dust2", ours=5, theirs=3, rnd=9))
    assert [e.type for e in later] == []      # второго E5 быть не должно


def test_feed_writes_do_not_reset_what_the_page_already_saw(match, config):
    """Живой фид переписывает состояние по нескольку раз в секунду, и это не
    должно возвращать страницу в положение «я этот матч впервые вижу».

    Порядок здесь именно такой, как в бою: воркер живого фида поднимается
    только для матча, уже помеченного LIVE, а помечает его страница — она же
    в этот момент и выдаёт E4. Опередить её фид не может.
    """
    page_machine = MatchMachine(match, config)
    live = page("match-2397053-live.html")

    assert [e.type for e in page_machine.apply(live)] == ["E4"]
    seen_at = match.get_state(MATCH_ID)["page_seen_utc"]
    assert seen_at is not None

    live_machine = LiveMachine(match, config)
    for _ in range(5):
        live_machine.apply(MATCH_ID, frame("de_dust2", ours=3, theirs=2, rnd=6))

    # Отметка пережила запись фида и не переставилась на новое время.
    assert match.get_state(MATCH_ID)["page_seen_utc"] == seen_at
    # Повторный опрос страницы не выдаёт E4 второй раз.
    assert page_machine.apply(live) == []


# ----------------------------------------------------------------------
# E6: страница обязана подстраховывать фид
# ----------------------------------------------------------------------


def test_page_reports_map_end_that_the_feed_missed(match, config):
    """Главное свойство схемы «фид решает, страница подтверждает»: если фид
    пропустил конец карты (реконнект, пауза после 403), сообщить обязана
    страница. Раньше она молчала всё время, пока фид на связи."""
    page_machine = MatchMachine(match, config)

    # страница уже наблюдала матч, карта ещё идёт
    page_machine.apply(page("match-2397053-live.html"), feed_connected=True)

    # фид работает и переписывает состояние на каждом кадре
    live_machine = LiveMachine(match, config)
    for _ in range(3):
        live_machine.apply(MATCH_ID, frame("de_dust2", ours=4, theirs=6, rnd=11))
    assert match.get_state(MATCH_ID)["last_source"] == "scorebot"

    # ...и пропустил окончание карты. Страница видит её сыгранной.
    events = page_machine.apply(page("match-2397047-finished.html"), feed_connected=True)
    assert "E6" in [e.type for e in events]


def test_first_page_observation_is_still_silent(match, config):
    """Обратная сторона: если страница видит матч ВПЕРВЫЕ и карта уже сыграна,
    это не переход, а состояние на момент знакомства."""
    events = MatchMachine(match, config).apply(page("match-2397047-finished.html"))
    assert [e.type for e in events] == ["E7"]


def test_both_sources_on_the_same_map_end_give_one_event(match, config):
    """Фид и страница приносят один и тот же конец карты. Уведомление одно."""
    from hltv_notify.notify.outbox import Notifier

    notifier = Notifier(match, config, telegram=None)
    page_machine = MatchMachine(match, config)
    live_machine = LiveMachine(match, config)

    page_machine.apply(page("match-2397053-live.html"), feed_connected=True)
    live_machine.apply(MATCH_ID, frame("de_dust2", rnd=1))

    # фид фиксирует конец карты по счёту
    for event in live_machine.apply(MATCH_ID, frame("de_dust2", ours=13, theirs=10, rnd=23)):
        notifier.enqueue(event)
    # затем страница видит ту же карту сыгранной
    for event in page_machine.apply(page("match-2397047-finished.html"), feed_connected=True):
        notifier.enqueue(event)

    keys = [row["idempotency_key"] for row in match.due_outbox(limit=50)]
    assert len([k for k in keys if k.startswith("E6:2397053:map:2")]) == 1
