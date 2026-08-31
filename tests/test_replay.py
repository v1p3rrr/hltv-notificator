"""Replaying a recorded live match.

The dump was taken from a real FORZE Reload match (2397053): 2150 records,
1977 frames, 15 connects and 14 disconnects over the run. That cannot be
reproduced on demand, which is why it sits in the repository.

The deduplication checks required by the spec are here: two passes in a row,
and a pass with an artificial disconnect in the middle.
"""

from pathlib import Path

import pytest

from conftest import FIXTURES
from hltv_notify.notify.outbox import Notifier
from hltv_notify.replay import _prepare, frames, replay
from hltv_notify.state.db import Storage
from hltv_notify.state.live_machine import LiveMachine

DUMP = FIXTURES / "scorebot-2397053-forze.jsonl.gz"
MATCH_ID = 2397053
LINEUP = ["Mirage", "Dust2", "Ancient"]


@pytest.fixture()
def prepared(storage):
    _prepare(storage, MATCH_ID)
    storage.set_map_lineup(MATCH_ID, LINEUP)
    return storage


def test_dump_is_readable():
    assert DUMP.exists()
    assert sum(1 for _ in frames(DUMP)) > 500


def test_replay_produces_the_expected_events(prepared, config):
    """A recorded match always yields one and the same list of events."""
    events = replay(DUMP, prepared, config, MATCH_ID)
    # The map went 10:13, so the map point on this dump belongs to the opponent.
    assert [e.type for e in events] == ["E5", "E11", "E6"]
    assert [e.idempotency_key for e in events] == [
        "E5:2397053:map:2:started:Dust2",
        "E11:2397053:map:2:point:them:13",
        "E6:2397053:map:2:result:10-13",
    ]


def test_replayed_score_matches_the_site(prepared, config):
    """On the site this map ended 10:13, not in our favour."""
    events = replay(DUMP, prepared, config, MATCH_ID)
    e6 = events[-1]
    assert (e6.payload["score_team"], e6.payload["score_opponent"]) == (10, 13)
    assert (e6.payload["series_team"], e6.payload["series_opponent"]) == (0, 1)
    assert e6.payload["map_name"] == "Dust2"
    assert e6.payload["overtime"] is False


def test_running_the_same_dump_twice_adds_nothing(prepared, config):
    """The project's main trap: the feed sends the full state all over again."""
    first = replay(DUMP, prepared, config, MATCH_ID)
    second = replay(DUMP, prepared, config, MATCH_ID)
    assert len(first) == 3
    assert second == []


def test_break_in_the_middle_and_full_state_again(prepared, config):
    """An artificial disconnect: half the frames, then everything from the
    start — exactly what happens on a reconnect mid-map."""
    machine = LiveMachine(prepared, config)
    all_frames = list(frames(DUMP))
    half = len(all_frames) // 2

    produced = []
    for frame in all_frames[:half]:
        produced += machine.apply(MATCH_ID, frame)
    # the "disconnect" — and the feed serves the history from the beginning
    for frame in all_frames:
        produced += machine.apply(MATCH_ID, frame)

    assert [e.type for e in produced] == ["E5", "E11", "E6"]


def test_notifications_are_not_duplicated_across_replays(tmp_path, config):
    """The same pass but through the notifier: the notification count is unchanged."""
    storage = Storage(tmp_path / "replay.db")
    _prepare(storage, MATCH_ID)
    storage.set_map_lineup(MATCH_ID, LINEUP)
    notifier = Notifier(storage, config, telegram=None)

    for _ in range(3):
        for event in replay(DUMP, storage, config, MATCH_ID):
            notifier.enqueue(event)

    assert storage.sent_event_count() == 3
    assert storage.pending_count() == 3
    storage.close()


def test_map_result_is_recorded_once(prepared, config):
    replay(DUMP, prepared, config, MATCH_ID)
    replay(DUMP, prepared, config, MATCH_ID)
    rows = prepared.map_results(MATCH_ID)
    assert len(rows) == 1
    assert (rows[0]["map_number"], rows[0]["map_name"]) == (2, "Dust2")
    assert (rows[0]["score_team"], rows[0]["score_opponent"]) == (10, 13)


# ----------------------------------------------------------------------
# The second dump: a live match with a map boundary and a real multikill
# ----------------------------------------------------------------------

BOUNDARY = FIXTURES / "scorebot-2396936-map-boundary.jsonl.gz"
MOUZ_MATCH = 2396936
MOUZ_ID = 4494


@pytest.fixture()
def mouz(storage):
    _prepare(storage, MOUZ_MATCH)
    storage.set_map_lineup(MOUZ_MATCH, ["Ancient", "Mirage", "Nuke"])
    return storage


@pytest.fixture()
def mouz_config():
    from hltv_notify.config import Config
    return Config(team_id=MOUZ_ID, team_name="MOUZ")


def test_boundary_dump_gives_map_start_multikill_and_map_end(mouz, mouz_config):
    """Recorded from a live BLAST match: the map Ancient from start to finish."""
    events = replay(BOUNDARY, mouz, mouz_config, MOUZ_MATCH)
    assert [e.type for e in events] == ["E5", "E9", "E11", "E6"]


def test_real_multikill_is_detected(mouz, mouz_config):
    """xertioN took 4 kills in round 15 — the event was born from the kill
    increment in scoreboard frames, without a single look at the log."""
    e9 = [e for e in replay(BOUNDARY, mouz, mouz_config, MOUZ_MATCH) if e.type == "E9"][0]
    assert e9.payload["nick"] == "xertioN"
    assert e9.payload["kills"] == 4
    assert e9.payload["round"] == 15
    assert e9.payload["map_name"] == "Ancient"


def test_real_map_end_score(mouz, mouz_config):
    e6 = [e for e in replay(BOUNDARY, mouz, mouz_config, MOUZ_MATCH) if e.type == "E6"][0]
    assert (e6.payload["score_team"], e6.payload["score_opponent"]) == (13, 4)
    assert e6.payload["map_number"] == 1


def test_boundary_dump_is_idempotent(mouz, mouz_config):
    replay(BOUNDARY, mouz, mouz_config, MOUZ_MATCH)
    assert replay(BOUNDARY, mouz, mouz_config, MOUZ_MATCH) == []


def test_half_time_is_found_in_a_real_recording(prepared):
    """The arithmetic against real frames rather than a hand-made score."""
    from dataclasses import replace as _replace

    from hltv_notify.config import Config

    events = replay(DUMP, prepared,
                    _replace(Config(), half_alerts=True, overtime_alerts=True),
                    MATCH_ID)
    phases = [e for e in events if e.type == "E12"]
    assert [e.idempotency_key for e in phases] == ["E12:2397053:map:2:half"]
    # 12 rounds played, the map went on to 10:13 — so no overtime alert.
    assert phases[0].payload["overtime"] == 0
    assert (phases[0].payload["score_team"]
            + phases[0].payload["score_opponent"]) == 12


def test_the_comeback_is_measured_on_real_frames(prepared, config):
    """That map went 10:13 our way — the opponent came back from 2:8 down and
    took eleven of the last fourteen rounds."""
    e6 = [e for e in replay(DUMP, prepared, config, MATCH_ID) if e.type == "E6"][0]
    assert (e6.payload["comeback_from_team"],
            e6.payload["comeback_from_opponent"]) == (8, 2)
    assert e6.payload["comeback_swing"] == 9
    assert e6.payload["comeback_result"] == "won"


def test_a_one_sided_map_gets_no_comeback_line(mouz, mouz_config):
    """13:4 from the front — there was never a hole to climb out of."""
    e6 = [e for e in replay(BOUNDARY, mouz, mouz_config, MOUZ_MATCH)
          if e.type == "E6"][0]
    assert "comeback_swing" not in e6.payload
