"""Переходы по странице матча: E4, E7, детект зависания и дедупликация."""

from datetime import datetime, timedelta, timezone

from conftest import FIXTURES, TEAM_ID
from hltv_notify.models import Event, MatchState
from hltv_notify.sources import match_page
from hltv_notify.sources.match_page import MapLine, MatchObservation
from hltv_notify.state.db import utcnow
from hltv_notify.state.match_machine import MatchMachine

MATCH_ID = 555


def add_match(storage, match_id=MATCH_ID):
    storage.upsert_match(
        match_id=match_id, opponent_id=13973, opponent_name="Color",
        event_name="Test Event", start_utc=utcnow(),
        url=f"https://www.hltv.org/matches/{match_id}/x",
        snapshot={}, snapshot_hash="x",
    )


def observe(status, maps, *, match_id=MATCH_ID):
    """Наша команда — team2 (справа), как в реальном матче 2397053."""
    return MatchObservation(
        match_id=match_id, status=status,
        start_utc=datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc),
        event_name="Test Event", event_id=1, best_of=3,
        team1_id=13973, team1_name="Color",
        team2_id=TEAM_ID, team2_name="FORZE Reload",
        maps=maps, scorebot_id=match_id if status == "live" else None,
    )


def maps(*scores):
    """scores — тройки (левый, правый, половины) или None для несыгранной карты.

    Счёт означает СЫГРАННУЮ карту, поэтому has_stats=True: у HLTV запись
    статистики появляется в момент её завершения. Для карты, которая идёт
    прямо сейчас, есть отдельный хелпер live_map_line().
    """
    lines = []
    for number, score in enumerate(scores, start=1):
        left, right, halves = (None, None, None) if score is None else score
        lines.append(MapLine(number=number, name=f"Map{number}",
                             score_left=left, score_right=right, halves=halves,
                             has_stats=left is not None))
    return lines


def live_map_line(number, left, right):
    """Карта, которая идёт: счёт есть, записи статистики ещё нет."""
    return MapLine(number=number, name=f"Map{number}", score_left=left,
                   score_right=right, halves=None, has_stats=False)


def test_e4_on_transition_to_live(storage, config):
    add_match(storage)
    m = MatchMachine(storage, config)
    events = m.apply(observe("live", maps(None, None, None)))
    assert [e.type for e in events] == ["E4"]
    assert events[0].idempotency_key == "E4:555:started"
    assert storage.get_state(MATCH_ID)["state"] == MatchState.LIVE


def test_e4_not_repeated_on_every_poll(storage, config):
    """Страница опрашивается раз в минуту и всё это время говорит LIVE.
    Карта при этом успела завершиться — про неё E6, но повторного E4 нет."""
    add_match(storage)
    m = MatchMachine(storage, config)
    m.apply(observe("live", maps(None, None, None)))
    again = [e.type for e in m.apply(observe("live", maps((13, 10, "( 8 : 4 ; 5 : 6 )"), None, None)))]
    assert "E4" not in again
    assert again == ["E6"]


def test_e7_with_series_score(storage, config):
    add_match(storage)
    m = MatchMachine(storage, config)
    m.apply(observe("live", maps(None, None, None)))
    events = m.apply(observe("over", maps(
        (10, 13, "( 5 : 7 ; 8 : 3 )"), (8, 13, "( 4 : 8 ; 4 : 5 )"), None)))
    # Обе карты доигрались между опросами: сначала о каждой, затем итог.
    assert [e.type for e in events] == ["E6", "E6", "E7"]
    e7 = events[-1]
    # мы справа: 13 и 13 наши
    assert (e7.payload["series_team"], e7.payload["series_opponent"]) == (2, 0)
    assert e7.payload["won"] is True
    assert e7.idempotency_key == "E7:555:finished:2-0"
    assert len(e7.payload["maps"]) == 2
    assert storage.get_state(MATCH_ID)["state"] == MatchState.FINISHED


def test_e7_not_repeated(storage, config):
    add_match(storage)
    m = MatchMachine(storage, config)
    final = observe("over", maps((10, 13, None), (8, 13, None), None))
    m.apply(final)
    assert m.apply(final) == []


def test_no_e4_if_match_discovered_already_over(storage, config):
    """Слать «матч начался» про доигранный матч бессмысленно — сразу итог."""
    add_match(storage)
    m = MatchMachine(storage, config)
    events = m.apply(observe("over", maps((10, 13, None), (8, 13, None), None)))
    assert [e.type for e in events] == ["E7"]


def test_decider_not_played_is_not_counted(storage, config):
    """BO3 закончился 2:0 — третья карта осталась с прочерком."""
    add_match(storage)
    m = MatchMachine(storage, config)
    events = m.apply(observe("over", maps((10, 13, None), (8, 13, None), None)))
    assert len(storage.map_results(MATCH_ID)) == 2


def test_overtime_detected_by_halves_not_by_score(storage, config):
    """Регламент овертаймов различается между турнирами, поэтому считаем
    половины, а не раунды."""
    add_match(storage)
    m = MatchMachine(storage, config)
    m.apply(observe("over", maps((16, 19, "( 5 : 7 ; 7 : 5 ; 4 : 7 )"), None, None)))
    row = storage.map_results(MATCH_ID)[0]
    assert row["overtime"] == 1
    assert (row["score_team"], row["score_opponent"]) == (19, 16)


def test_stall_reported_only_after_threshold(storage, config):
    """Зависание ПОСРЕДИ карты: карта идёт, но счёт не двигается."""
    add_match(storage)
    m = MatchMachine(storage, config)
    frozen = observe("live", [live_map_line(1, 5, 7)] + maps(None, None)[1:])
    now = utcnow()

    assert [e.type for e in m.apply(frozen, now=now)] == ["E4"]
    assert m.apply(frozen, now=now + timedelta(minutes=5)) == []

    late = now + timedelta(minutes=config.stale_minutes + 1)
    events = m.apply(frozen, now=late)
    assert [e.type for e in events] == ["E8"]
    assert "stale" in events[0].idempotency_key


def test_progress_resets_the_stall_timer(storage, config):
    """Технические паузы бывают долгими, но пока счёт идёт — это не зависание."""
    add_match(storage)
    m = MatchMachine(storage, config)
    now = utcnow()
    m.apply(observe("live", [live_map_line(1, 5, 7)] + maps(None, None)[1:]), now=now)
    moved = now + timedelta(minutes=config.stale_minutes - 1)
    m.apply(observe("live", [live_map_line(1, 6, 7)] + maps(None, None)[1:]), now=moved)
    events = m.apply(observe("live", [live_map_line(1, 6, 7)] + maps(None, None)[1:]),
                     now=moved + timedelta(minutes=config.stale_minutes - 1))
    assert events == []


def test_break_between_maps_is_not_a_stall(storage, config):
    """Реальный случай с матча BLAST: карта закончилась, следующая ещё не
    началась, и через 20 минут прилетело ложное «матч завис». Между картами
    порог растягивается."""
    add_match(storage)
    m = MatchMachine(storage, config)
    between = observe("live", maps((13, 4, "( 8 : 4 ; 5 : 0 )"), None, None))
    now = utcnow()
    m.apply(between, now=now)

    normal = now + timedelta(minutes=config.stale_minutes + 1)
    assert m.apply(between, now=normal) == []

    very_long = now + timedelta(minutes=config.stale_minutes * 3 + 1)
    assert [e.type for e in m.apply(between, now=very_long)] == ["E8"]


def test_no_stall_alert_while_the_live_feed_is_connected(storage, config):
    """Смысл события — «я ослеп». Пока фид на связи, мы видим матч, и его
    молчание в паузе слепотой не является."""
    add_match(storage)
    m = MatchMachine(storage, config)
    frozen = observe("live", [live_map_line(1, 5, 7)] + maps(None, None)[1:])
    now = utcnow()
    m.apply(frozen, now=now, feed_connected=True)
    late = now + timedelta(minutes=config.stale_minutes * 5)
    assert m.apply(frozen, now=late, feed_connected=True) == []


def test_stall_timer_does_not_accumulate_under_a_working_feed(storage, config):
    """После отключения фида тревога не должна прилететь мгновенно за всё
    время, что он работал."""
    add_match(storage)
    m = MatchMachine(storage, config)
    frozen = observe("live", [live_map_line(1, 5, 7)] + maps(None, None)[1:])
    now = utcnow()
    m.apply(frozen, now=now, feed_connected=True)
    later = now + timedelta(hours=2)
    assert m.apply(frozen, now=later, feed_connected=True) == []
    # фид отвалился — отсчёт начинается заново, а не задним числом
    assert m.apply(frozen, now=later, feed_connected=False) == []


def test_observation_without_our_team_is_dropped(storage, config):
    """Не та страница или сменившаяся разметка не должны записать чужой счёт."""
    add_match(storage)
    m = MatchMachine(storage, config)
    alien = MatchObservation(
        match_id=MATCH_ID, status="live", start_utc=None, event_name="", event_id=None,
        best_of=3, team1_id=1, team1_name="A", team2_id=2, team2_name="B",
        maps=maps((13, 10, None)), scorebot_id=None)
    assert m.apply(alien) == []
    assert storage.get_state(MATCH_ID) is None


def test_real_fixtures_drive_a_full_match(storage, config):
    """Прогон по настоящим страницам.

    Первая карта на live-фикстуре уже сыграна к моменту знакомства с матчем,
    поэтому E6 по ней не шлётся — это не переход. Вторая завершается уже под
    наблюдением, о ней сообщается.
    """
    add_match(storage, 2397047)
    m = MatchMachine(storage, config)
    live = match_page.parse((FIXTURES / "match-2397053-live.html").read_text(encoding="utf-8"),
                            2397047)
    over = match_page.parse((FIXTURES / "match-2397047-finished.html").read_text(encoding="utf-8"),
                            2397047)
    produced = [e.type for e in m.apply(live)] + [e.type for e in m.apply(over)]
    assert produced == ["E4", "E6", "E7"]


# ----------------------------------------------------------------------
# E6 — ключевое требование ТЗ: конец карты со счётом
# ----------------------------------------------------------------------


def test_e6_when_map_becomes_decided(storage, config):
    add_match(storage)
    m = MatchMachine(storage, config)
    m.apply(observe("live", maps(None, None, None)))
    events = m.apply(observe("live", maps((10, 13, "( 5 : 7 ; 8 : 3 )"), None, None)))
    assert [e.type for e in events] == ["E6"]
    e6 = events[0]
    assert e6.idempotency_key == "E6:555:map:1:result:13-10"
    assert (e6.payload["score_team"], e6.payload["score_opponent"]) == (13, 10)
    assert (e6.payload["series_team"], e6.payload["series_opponent"]) == (1, 0)
    assert e6.payload["map_name"] == "Map1"


def test_e6_not_repeated_while_match_continues(storage, config):
    """Страница ещё долго показывает счёт сыгранной карты — событие одно."""
    add_match(storage)
    m = MatchMachine(storage, config)
    m.apply(observe("live", maps(None, None, None)))
    decided = observe("live", maps((10, 13, None), None, None))
    assert [e.type for e in m.apply(decided)] == ["E6"]
    assert m.apply(decided) == []
    assert m.apply(decided) == []


def test_two_maps_decided_between_polls_get_their_own_series_score(storage, config):
    """Если опрос пропустил окончание первой карты, счёт серии в сообщении о
    ней должен быть на момент этой карты, а не итоговый."""
    add_match(storage)
    m = MatchMachine(storage, config)
    m.apply(observe("live", maps(None, None, None)))
    events = m.apply(observe("live", maps((10, 13, None), (13, 8, None), None)))
    assert [e.type for e in events] == ["E6", "E6"]
    first, second = events
    assert (first.payload["map_number"], first.payload["series_team"],
            first.payload["series_opponent"]) == (1, 1, 0)
    assert (second.payload["map_number"], second.payload["series_team"],
            second.payload["series_opponent"]) == (2, 1, 1)


def test_no_e6_if_match_discovered_already_over(storage, config):
    """Матч доигран, пока сервис лежал: сыпать E6 по всем картам задним
    числом — мусор, шлём только итог."""
    add_match(storage)
    m = MatchMachine(storage, config)
    events = m.apply(observe("over", maps((10, 13, None), (8, 13, None), None)))
    assert [e.type for e in events] == ["E7"]
    assert len(storage.map_results(MATCH_ID)) == 2


def test_e6_survives_overtime(storage, config):
    add_match(storage)
    m = MatchMachine(storage, config)
    m.apply(observe("live", maps(None, None, None)))
    events = m.apply(observe("live", maps((16, 19, "( 5 : 7 ; 7 : 5 ; 4 : 7 )"), None, None)))
    assert events[0].payload["overtime"] is True
    assert (events[0].payload["score_team"], events[0].payload["score_opponent"]) == (19, 16)


def test_full_bo3_replay_gives_exact_event_sequence(storage, config):
    """Записанная последовательность наблюдений всегда даёт один и тот же
    список событий — это и регрессионный тест."""
    add_match(storage)
    m = MatchMachine(storage, config)
    timeline = [
        observe("upcoming", maps(None, None, None)),
        observe("live", maps(None, None, None)),
        observe("live", maps((10, 13, None), None, None)),
        observe("live", maps((10, 13, None), None, None)),
        observe("live", maps((10, 13, None), (13, 9, None), None)),
        observe("live", maps((10, 13, None), (13, 9, None), None)),
        observe("live", maps((10, 13, None), (13, 9, None), (11, 13, None))),
        observe("over", maps((10, 13, None), (13, 9, None), (11, 13, None))),
    ]
    produced = []
    for observation in timeline:
        produced += [e.type for e in m.apply(observation)]
    assert produced == ["E4", "E6", "E6", "E6", "E7"]


def test_replaying_the_same_timeline_twice_sends_nothing_new(storage, config):
    """Тот же сценарий, что реконнект живого фида: полное состояние приходит
    заново, а число уведомлений меняться не должно."""
    from hltv_notify.notify.outbox import Notifier

    add_match(storage)
    m = MatchMachine(storage, config)
    notifier = Notifier(storage, config, telegram=None)
    timeline = [
        observe("live", maps(None, None, None)),
        observe("live", maps((10, 13, None), None, None)),
        observe("live", maps((10, 13, None), (13, 9, None), None)),
        observe("over", maps((10, 13, None), (13, 9, None), None)),
    ]
    for _ in range(2):
        for observation in timeline:
            for event in m.apply(observation):
                notifier.enqueue(event)

    assert storage.sent_event_count() == 4       # E4, E6, E6, E7
    assert storage.pending_count() == 4


def test_running_map_does_not_produce_e6(storage, config):
    """Главная ловушка, найденная на живом матче: у идущей карты счёт тоже
    числовой, и наивное правило прислало бы E6 с промежуточным счётом."""
    add_match(storage)
    m = MatchMachine(storage, config)
    m.apply(observe("live", maps(None, None, None)))

    running = [live_map_line(1, 5, 7)] + maps(None, None)[1:]
    assert m.apply(observe("live", running)) == []
    assert storage.map_results(MATCH_ID) == []

    # ...а когда карта закончилась, событие приходит один раз и с финальным счётом
    events = m.apply(observe("live", maps((11, 13, "( 5 : 7 ; 6 : 6 )"), None, None)))
    assert [e.type for e in events] == ["E6"]
    assert (events[0].payload["score_team"], events[0].payload["score_opponent"]) == (13, 11)


def test_series_score_stored_during_running_map_excludes_it(storage, config):
    add_match(storage)
    m = MatchMachine(storage, config)
    m.apply(observe("live", maps(None, None, None)))
    m.apply(observe("live", maps((11, 13, None), None, None) [:1]
                    + [live_map_line(2, 3, 4)] + maps(None, None, None)[2:]))
    assert storage.get_state(MATCH_ID)["series_score"] == "1-0"


def test_drawn_series_is_neither_a_win_nor_a_loss():
    """BO2 вполне заканчивается 1:1.

    Булево здесь означало бы поражение, а получателю, следящему за соперником,
    format.orient перевернул бы его в победу — про один и тот же результат.
    """
    from hltv_notify.notify import format as fmt

    payload = {"series_team": 1, "series_opponent": 1,
               "won": None, "team_id": 1, "opponent_id": 2,
               "team_name": "MOUZ", "opponent": "FORZE Reload",
               "event_name": "Major", "url": "u", "maps": []}
    event = Event(type="E7", idempotency_key="E7:1:finished:1-1",
                  match_id=1, payload=payload)

    ours = fmt.render(event, team_name="MOUZ", tz_name="UTC", for_team_id=1)
    theirs = fmt.render(event, team_name="MOUZ", tz_name="UTC", for_team_id=2)
    assert "🤝" in ours and "🤝" in theirs
    assert "🏆" not in theirs and "💀" not in ours
