"""The match transcript: what the feed's log says, written out readably.

The hard part is not the wording. It is that the log has no timestamps, no map
on an entry, no round on an entry, and — for most of its types — nothing at all
to tell one entry from the copy of it the next connect will replay. Every test
here is about one of those.
"""

import gzip
import json

import pytest

from conftest import FIXTURES
from hltv_notify.config import Config
from hltv_notify.sources.scorebot import (LiveFrame, LogEntry, feed_items,
                                          parse_log)
from hltv_notify.state import matchlog
from hltv_notify.state.db import utcnow
from hltv_notify.state.live_machine import LiveMachine

MATCH_ID = 777
US, THEM = 12857, 13973
DUMP = FIXTURES / "scorebot-2397053-forze.jsonl.gz"


def entry(kind, **data):
    return LogEntry(kind=kind, data=data)


def frame(*, regulation=12, overtime=3, rnd=1, ours=0, theirs=0):
    return LiveFrame(
        map_name="de_dust2", current_round=rnd, round_state="started", live=True,
        ct_team_id=US, ct_team_name="FORZE Reload", ct_score=ours,
        t_team_id=THEM, t_team_name="Color", t_score=theirs,
        regulation=regulation, overtime=overtime,
        starting_ct=US, starting_t=THEM)


def names(team_id):
    return {US: "FORZE Reload", THEM: "Color"}.get(team_id, "")


def add_match(storage):
    storage.upsert_match(
        match_id=MATCH_ID, opponent_id=THEM, opponent_name="Color",
        event_name="Test Event", start_utc=utcnow(),
        url=f"https://www.hltv.org/matches/{MATCH_ID}/x",
        snapshot={}, snapshot_hash="x")
    storage.link_match_team(MATCH_ID, US)


# ----------------------------------------------------------- the parser

def test_every_type_survives_the_parse():
    """`parse_kills` used to read `Kill` and drop the rest. The transcript
    prints everything, and an entry dropped here would also shift the
    positions it counts by."""
    payload = json.dumps({"log": [
        {"RoundEnd": {"counterTerroristScore": 1, "terroristScore": 0,
                      "winner": "CT", "winType": "CTs_Win"}},
        {"Kill": {"eventId": 7, "killerNick": "a", "victimNick": "b"}},
        {"RoundStart": {}},
    ]})
    entries = parse_log(payload)
    # Oldest first: the feed sends them newest first.
    assert [item.kind for item in entries] == ["RoundStart", "Kill", "RoundEnd"]


def test_an_assist_is_placed_by_the_kill_it_belongs_to():
    """`Assist` has no id of its own, only the `killEventId` of the kill it
    assisted — which is how it is placed in the stream."""
    assert entry("Assist", killEventId=42).event_id == 42
    assert entry("Kill", eventId=42).event_id == 42
    assert entry("RoundStart").event_id is None


# ------------------------------------------------------- map and round

def test_the_map_and_the_round_come_out_of_the_stream():
    """Not out of the frame. The backlog a connect replays reaches back to
    maps played before we were watching, and the frame describes the one being
    played now — so reading the map off it would stamp map one's rounds with
    map two's name."""
    state = matchlog.Transcript()
    stream = [
        entry("MatchStarted", map="de_mirage"),
        entry("Kill", eventId=1),
        entry("RoundEnd", counterTerroristScore=1, terroristScore=0,
              winner="CT", winType="CTs_Win"),
        entry("Kill", eventId=2),
        entry("RoundEnd", counterTerroristScore=1, terroristScore=1,
              winner="TERRORIST", winType="Terrorists_Win"),
        entry("MatchStarted", map="de_dust2"),
        entry("Kill", eventId=3),
    ]
    seen = []
    for item in stream:
        seen.append((state.map_name, state.round_of(item)))
        state.observe(item)
    assert seen == [("", 1), ("de_mirage", 1), ("de_mirage", 1),
                    ("de_mirage", 2), ("de_mirage", 2),
                    ("de_mirage", 3), ("de_dust2", 1)]


def test_a_round_is_numbered_by_the_score_it_ended_on():
    """Every round adds exactly one to the score, so the sum IS the round's
    number — in regulation and in overtime alike. Counting rounds instead
    would shift everything after a RoundEnd lost to a dropped connection."""
    state = matchlog.Transcript()
    state.observe(entry("RoundEnd", counterTerroristScore=9, terroristScore=4,
                        winner="CT", winType="CTs_Win"))
    assert state.round_number == 14


def test_a_restart_puts_the_round_count_back():
    """The knife round scores 1:0 and the server then resets the map. Without
    this the real round one would be numbered two."""
    state = matchlog.Transcript()
    state.observe(entry("RoundEnd", counterTerroristScore=1, terroristScore=0,
                        winner="CT", winType="CTs_Win"))
    assert state.round_number == 2
    state.observe(entry("Restart"))
    assert state.round_number == 1


# ------------------------------------------------------------- the sides

@pytest.mark.parametrize("round_number,half", [
    (1, 0), (12, 0), (13, 1), (24, 1), (25, 2), (27, 2), (28, 3), (31, 4),
])
def test_which_half_a_round_belongs_to(round_number, half):
    assert matchlog.ct_half(round_number, 12, 3) == half


def test_the_team_on_a_side_comes_from_the_start_of_the_map():
    """From `startingCt`, which is fixed for the map — never from `ctTeamId`,
    which is only true of the frame in hand. The transcript writes rounds that
    are already over, the whole backlog of them, and the current frame's sides
    are the wrong answer for every round of the other half."""
    first_half = frame()
    assert matchlog.team_on("CT", 1, first_half) == US
    assert matchlog.team_on("TERRORIST", 1, first_half) == THEM
    # After the break the same frame answers the other way for round 13.
    assert matchlog.team_on("CT", 13, first_half) == THEM
    assert matchlog.team_on("TERRORIST", 13, first_half) == US
    # The first overtime half puts them back on the sides they started on.
    assert matchlog.team_on("CT", 25, first_half) == US


def test_without_the_starting_sides_no_team_is_named():
    """Rather than naming one from the current frame and being wrong for half
    the map."""
    bare = frame()
    assert matchlog.team_on("CT", 1, LiveFrame(
        map_name="x", current_round=1, round_state="started", live=True,
        ct_team_id=US, ct_team_name="a", ct_score=0, t_team_id=THEM,
        t_team_name="b", t_score=0, regulation=12, overtime=3)) is None
    assert matchlog.team_on("CT", 1, bare) == US


# ------------------------------------------------------------ rendering

def render(kind, round_number=1, frame_=None, **data):
    return matchlog.render(entry(kind, **data), round_number=round_number,
                           frame=frame_, name_of=names)


def test_a_kill_names_everything_that_was_special_about_it():
    line = render("Kill", killerNick="KusMe", killerSide="TERRORIST",
                  victimNick="reyoz", victimSide="CT", weapon="ak47",
                  headShot=True, penetrated=True, throughSmoke=True)
    assert line == ("KusMe (T) killed reyoz (CT) — ak47, headshot, wallbang, "
                    "through smoke")


def test_a_plain_kill_says_only_the_weapon():
    assert render("Kill", killerNick="a", killerSide="CT", victimNick="b",
                  victimSide="TERRORIST", weapon="awp") == \
        "a (CT) killed b (T) — awp"


def test_a_bomb_plant_carries_the_site_and_who_was_alive():
    assert render("BombPlanted", playerNick="Ryujin", bombSite="B",
                  ctPlayers=2, tPlayers=3) == \
        "Ryujin (T) planted the bomb at B (2 CT vs 3 T alive)"


def test_a_round_end_names_the_team_and_the_score():
    line = render("RoundEnd", round_number=3, frame_=frame(),
                  counterTerroristScore=2, terroristScore=1,
                  winner="CT", winType="Target_Saved")
    assert line == ("round 3 ended — FORZE Reload (CT) won · the bomb was "
                    "never planted [Target_Saved] · FORZE Reload 2:1 Color")


def test_a_round_end_without_a_frame_still_says_what_happened():
    """The first log packet of a match arrives BEFORE any scoreboard frame, so
    this is the ordinary case for the whole backlog — and naming a team from a
    frame we do not have is the one thing it must not do."""
    line = render("RoundEnd", counterTerroristScore=1, terroristScore=0,
                  winner="CT", winType="CTs_Win")
    assert line == "round 1 ended — CT won · CTs_Win · CT 1:0 T"


def test_only_the_bomb_outcomes_are_put_into_words():
    """`Target_Bombed` and `Bomb_Defused` say what happened to the bomb and
    nothing else could be meant. `CTs_Win` does NOT say whether the round went
    on elimination or on the clock, so it is not glossed into either."""
    assert "the bomb exploded" in render(
        "RoundEnd", counterTerroristScore=0, terroristScore=1,
        winner="TERRORIST", winType="Target_Bombed")
    assert "CTs_Win" in render(
        "RoundEnd", counterTerroristScore=1, terroristScore=0,
        winner="CT", winType="CTs_Win")


def test_the_source_has_no_defuser_so_none_is_invented():
    """There is no `BombDefused` entry in the feed's log at all — a defuse is
    visible only as a RoundEnd's winType, with no player on it. The line says
    the bomb was defused and does not guess who did it."""
    line = render("RoundEnd", counterTerroristScore=1, terroristScore=0,
                  winner="CT", winType="Bomb_Defused")
    assert "the bomb was defused" in line
    assert "defused by" not in line


def test_an_unknown_type_is_printed_rather_than_dropped():
    """The whole point of the file is to show what arrived."""
    assert "SomethingNew" in render("SomethingNew", foo=1)


# -------------------------------------------------- the replayed backlog

def test_the_first_packet_of_a_match_is_all_new():
    fresh, position, first = matchlog.new_entries(
        [entry("Kill", eventId=5), entry("RoundStart")],
        position=0, first_id=None)
    assert len(fresh) == 2 and position == 2 and first == 5


def test_a_replayed_backlog_resumes_at_the_cursor():
    stream = [entry("Kill", eventId=5), entry("RoundStart"),
              entry("Kill", eventId=9)]
    fresh, position, _ = matchlog.new_entries(stream, position=2, first_id=5)
    assert [item.kind for item in fresh] == ["Kill"]
    assert position == 3


def test_an_assist_packet_is_not_mistaken_for_a_replay(storage):
    """The feed sends an `Assist` as its own packet carrying the killEventId
    of the kill before it, so its only id EQUALS the newest one seen. Read as
    a replay, its position of one overwrote a cursor of two thousand and the
    next connect rewrote the whole match — measured, 317 round-end lines for a
    match with 46 rounds."""
    fresh, position, _ = matchlog.new_entries(
        [entry("Assist", killEventId=9)], position=50, first_id=5)
    assert len(fresh) == 1
    assert position == 51


def test_the_cursor_never_moves_backwards():
    """A backlog taken mid-poll can be shorter than what the live stream has
    already delivered."""
    _, position, _ = matchlog.new_entries(
        [entry("Kill", eventId=5)], position=40, first_id=5)
    assert position == 40


# ------------------------------------------------------- the machine

def test_the_transcript_is_written_and_resumes_after_a_restart(storage):
    add_match(storage)
    config = Config(team_id=US, match_log=True)
    machine = LiveMachine(storage, config)
    machine.apply(MATCH_ID, frame())
    machine.observe_log(MATCH_ID, (
        entry("MatchStarted", map="de_dust2"),
        entry("Kill", eventId=1, killerNick="a", killerSide="CT",
              victimNick="b", victimSide="TERRORIST", weapon="awp"),
    ))
    assert len(storage.match_log_lines(MATCH_ID)) == 2

    # A brand-new machine on the same database, and the next packet.
    again = LiveMachine(storage, config)
    again.apply(MATCH_ID, frame())
    again.observe_log(MATCH_ID, (
        entry("MatchStarted", map="de_dust2"),
        entry("Kill", eventId=1, killerNick="a", killerSide="CT",
              victimNick="b", victimSide="TERRORIST", weapon="awp"),
        entry("RoundEnd", counterTerroristScore=1, terroristScore=0,
              winner="CT", winType="CTs_Win"),
    ))
    rows = storage.match_log_lines(MATCH_ID)
    assert len(rows) == 3
    # The round picked up where the stored line left it rather than at one.
    assert rows[-1]["round_number"] == 1
    assert rows[-1]["kind"] == "RoundEnd"


def test_nothing_is_written_when_the_transcript_is_off(storage):
    add_match(storage)
    machine = LiveMachine(storage, Config(team_id=US, match_log=False))
    machine.apply(MATCH_ID, frame())
    machine.observe_log(MATCH_ID, (entry("Kill", eventId=1),))
    assert storage.match_log_lines(MATCH_ID) == []


def test_a_transcript_failure_does_not_cost_the_feed(storage, monkeypatch):
    """It is a convenience; the feed is the service. An exception here lands
    inside the frame loop, which is the one place that costs every subscriber
    their live score."""
    add_match(storage)
    machine = LiveMachine(storage, Config(team_id=US, match_log=True))
    machine.apply(MATCH_ID, frame())
    monkeypatch.setattr(storage, "append_match_log",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk")))
    machine.observe_log(MATCH_ID, (entry("Kill", eventId=1),))   # must not raise


def test_a_transcript_older_than_the_window_is_dropped(storage):
    import datetime as _dt
    add_match(storage)
    storage.append_match_log(MATCH_ID, [("Dust2", 1, "Kill", "a killed b")])
    storage.set_match_log_cursor(MATCH_ID, 1, 5)
    storage.conn.execute(
        "UPDATE match_log SET created_utc = ?",
        ((utcnow() - _dt.timedelta(days=3)).isoformat(),))
    assert storage.prune_match_log(2) == 1
    assert storage.match_log_lines(MATCH_ID) == []
    # The cursor goes with the lines: a match whose transcript is gone must
    # not resume halfway through a stream whose beginning no longer exists.
    assert storage.match_log_cursor(MATCH_ID) == (0, None)


# ------------------------------------------------------ the recording

def recorded_entries():
    for line in gzip.open(DUMP, "rt", encoding="utf-8"):
        record = json.loads(line)
        if record.get("kind") != "frame":
            continue
        for kind, item in feed_items([record["raw"]]):
            yield kind, item


def test_a_real_match_writes_one_line_per_entry(storage):
    """The numbers the design stands on, against the recording rather than
    against a hand-made stream."""
    from hltv_notify.replay import _prepare, replay

    _prepare(storage, 2397053)
    config = Config(team_id=US, match_log=True)
    replay(DUMP, storage, config, 2397053)
    rows = storage.match_log_lines(2397053)
    kills = [row for row in rows if row["kind"] == "Kill"]
    # 543 unique kills in this recording, and not one of the 8255 replayed
    # copies of them.
    assert len(kills) == 543
    # The transcript is exactly as long as the longest backlog the feed sent,
    # which is what says the cursor landed on every entry and no more.
    longest = max((len(item) for kind, item in recorded_entries()
                   if kind == "log"), default=0)
    assert len(rows) == longest

    # And the production repeat — a reconnect replaying the whole backlog —
    # adds nothing at all.
    backlog = max((item for kind, item in recorded_entries() if kind == "log"),
                  key=len)
    LiveMachine(storage, config).observe_log(2397053, backlog)
    assert len(storage.match_log_lines(2397053)) == len(rows)


def test_the_file_reads_as_a_transcript(storage):
    from hltv_notify.replay import _prepare, replay

    _prepare(storage, 2397053)
    replay(DUMP, storage, Config(team_id=US, match_log=True), 2397053)
    text = matchlog.to_text(2397053, "FORZE Reload vs Color",
                            storage.match_log_lines(2397053))
    assert text.startswith("Match 2397053 — FORZE Reload vs Color")
    # Headings flush left, everything that happened inside a round indented
    # under them.
    assert "\n=== map: de_dust2 ===" in text
    assert "\n--- round 1 ---" in text
    assert "\n    " in text
    assert "planted the bomb at" in text
    assert "ended — " in text


# ----------------------------------------------------- the upload itself

def test_the_document_really_leaves_as_multipart():
    """Against a real socket, because nothing less would have caught this.

    curl_cffi accepts a `files=` argument — the one every other HTTP client in
    Python takes — and then raises NotImplementedError from inside the
    request. A mocked Telegram would have agreed with the mistake all the way
    to production, where the only symptom is a command that answers nothing.
    """
    import asyncio
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from hltv_notify.notify.telegram import Telegram

    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            seen["type"] = self.headers.get("Content-Type", "")
            seen["body"] = self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true,"result":{"message_id":7}}')

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    telegram = Telegram("token")
    import hltv_notify.notify.telegram as module
    original = module.API
    module.API = f"http://127.0.0.1:{port}/{{method}}"
    try:
        message_id = asyncio.run(telegram.send_document(
            "42", "match-1.txt", b"round 1 ended\n", caption="a match"))
    finally:
        module.API = original
        asyncio.run(telegram.close())
        server.shutdown()

    assert message_id == 7
    assert seen["type"].startswith("multipart/form-data")
    assert b"match-1.txt" in seen["body"]
    assert b"round 1 ended" in seen["body"]
    assert b"42" in seen["body"]
