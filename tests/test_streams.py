"""Broadcast links under a multikill: parsing, choosing, rendering.

The block exists for one reason — a 4k has just happened and the owner wants
to be on a stream within seconds to clip it. Every rule here follows from that:
short list, most watched first, and no link in a language the reader cannot
follow.
"""

import logging

import pytest
from bs4 import BeautifulSoup

from conftest import FIXTURES
from hltv_notify import streams as st
from hltv_notify.config import Config
from hltv_notify.models import Event
from hltv_notify.notify import format as fmt
from hltv_notify.sources import match_page

# The default table: English arrives under half a dozen country flags.
ALIASES = {"GB": "en", "US": "en", "WORLD": "en",
           "AU": "en", "CA": "en", "NZ": "en", "IE": "en"}


def one(flag, viewers, provider="twitch", name=None):
    return {"provider": provider, "name": name or f"{flag}-{viewers}",
            "flag": flag, "viewers": viewers,
            "url": f"https://www.twitch.tv/{flag}{viewers}"}


def langs(chosen):
    return [st.language_of(item["flag"], ALIASES) for item in chosen]


# ---------- reading them off the page ----------

def test_streams_are_read_from_a_real_match_page():
    html = (FIXTURES / "match-2397053-live.html").read_text(encoding="utf-8")
    found = match_page.parse(html, 2397053).streams
    assert [(s.provider, s.flag, s.viewers) for s in found] == [
        ("twitch", "RU", 51),
        ("kick", "WORLD", 41),
        ("kick", "RU", 20),
        ("twitch", "RU", 13),
    ]
    assert found[0].name == "GLuck"
    assert found[0].url == "https://www.twitch.tv/GLuck_Esports"


def test_streams_come_back_sorted_by_viewers():
    """Sorted once, at the source, so that nothing downstream re-derives what
    "the top three" means."""
    html = (FIXTURES / "match-2397091-live-midmap.html").read_text(encoding="utf-8")
    found = match_page.parse(html, 2397091).streams
    assert [s.viewers for s in found] == sorted([s.viewers for s in found], reverse=True)


def test_a_match_nobody_casts_has_no_streams():
    html = (FIXTURES / "match-2397047-finished.html").read_text(encoding="utf-8")
    assert match_page.parse(html, 2397047).streams == ()


def _boxes(html):
    return match_page._streams(BeautifulSoup(html, "lxml"))


def test_platforms_we_cannot_clip_on_are_dropped():
    """YouTube is listed by HLTV and has no clip button, so a link there is a
    dead end dressed up as a choice."""
    html = """
    <div class="stream-box" data-stream-provider="youtube">
      <div class="stream-box-embed" data-stream-embed="x">SomeCaster</div>
      <span class="viewers">9999</span>
      <div class="external-stream"><a href="https://www.youtube.com/watch?v=a"></a></div>
    </div>
    <div class="stream-box" data-stream-provider="twitch">
      <div class="stream-box-embed" data-stream-embed="x">Real</div>
      <img class="stream-flag" src="/img/static/flags/30x20/RU.gif">
      <span class="viewers">5</span>
      <div class="external-stream"><a href="https://www.twitch.tv/real"></a></div>
    </div>
    """
    assert [s.name for s in _boxes(html)] == ["Real"]


def test_a_link_leading_somewhere_else_is_dropped():
    """The href comes off a web page. This project has already been burned once
    by trusting one, and a link the owner is invited to tap is not the place to
    start trusting them again."""
    html = """
    <div class="stream-box" data-stream-provider="twitch">
      <div class="stream-box-embed" data-stream-embed="x">Evil</div>
      <span class="viewers">1</span>
      <div class="external-stream"><a href="https://www.twitch.tv.evil.example/x"></a></div>
    </div>
    <div class="stream-box" data-stream-provider="kick">
      <div class="stream-box-embed" data-stream-embed="x">AlsoEvil</div>
      <span class="viewers">1</span>
      <div class="external-stream"><a href="http://kick.com/x"></a></div>
    </div>
    """
    assert _boxes(html) == ()


def test_the_hltv_live_box_is_not_a_stream():
    """It is an <a> without a provider — it opens HLTV's own player, which is
    not something to clip from."""
    html = ('<a href="/live?matchId=1" class="stream-box hltv-live">'
            '<span class="hltv-live-logo">HLTV Live</span></a>')
    assert _boxes(html) == ()


# ---------- flags and languages ----------

def test_english_arrives_under_many_flags():
    for flag in ("GB", "US", "WORLD", "AU", "CA", "NZ", "IE"):
        assert st.language_of(flag, ALIASES) == "en"


def test_a_flag_that_is_not_aliased_is_its_own_language():
    assert st.language_of("RU", ALIASES) == "ru"
    assert st.language_of("BR", ALIASES) == "br"


def test_flag_emoji():
    assert st.flag_emoji("RU") == "🇷🇺"
    assert st.flag_emoji("WORLD") == "🌍"
    assert st.flag_emoji("") == "🌍"


# ---------- where the alias table comes from ----------

def aliases_from(raw):
    return Config(stream_language_aliases=raw).flag_languages()


def test_the_alias_table_survives_the_spaces_a_person_writes():
    """Whitespace separates one GROUP from the next, so a space written inside
    one split it into pieces that each parsed to nothing and the table came
    back empty — for the form a person is most likely to type."""
    packed = aliases_from("en:GB,US,WORLD,AU")
    assert packed == {"GB": "en", "US": "en", "WORLD": "en", "AU": "en"}
    for spaced in ("en: GB, US, WORLD, AU",
                   "en : GB , US , WORLD , AU",
                   "en:GB, US, WORLD, AU"):
        assert aliases_from(spaced) == packed


def test_a_space_still_separates_one_language_from_the_next():
    assert aliases_from("en:GB,US ru:BY,KZ") == {
        "GB": "en", "US": "en", "BY": "ru", "KZ": "ru"}
    assert aliases_from("en: GB, US; ru: BY") == {
        "GB": "en", "US": "en", "BY": "ru"}


def test_a_second_language_may_be_appended_with_a_comma():
    """Extending the shipped default by writing `, ru:BY` after it is the
    obvious thing to do. Read as a separator inside English it stored the flag
    "ru:BY" and BY never meant Russian — silently, and the warning below could
    not catch it because the table was not empty."""
    for written in ("en:GB,US, ru:BY", "en:GB,US,ru:BY", "en:GB, US ru:BY"):
        assert aliases_from(written) == {"GB": "en", "US": "en", "BY": "ru"}
    assert not any(":" in flag for flag in aliases_from("en:GB,US, ru:BY"))


def test_a_spaced_table_keeps_the_australian_cast_in_the_block():
    """The consequence, which is why an empty table matters: without the alias
    AU is its own language, falls outside en/ru, and the cast on 155 viewers
    loses to the one on 8 — the very case the field exists for."""
    chosen = st.pick([one("AU", 155), one("RU", 8)], primary=("en", "ru"),
                     limit=3, aliases=aliases_from("en: GB, US, WORLD, AU"))
    assert [item["viewers"] for item in chosen] == [155, 8]


def test_a_table_that_parses_to_nothing_says_so_in_the_log(caplog):
    """An empty table breaks nothing visibly — the block still appears, just
    with the wrong broadcasts in it — so it has to be said out loud."""
    with caplog.at_level(logging.WARNING):
        assert aliases_from("en GB US") == {}
    assert "STREAM_LANGUAGE_ALIASES" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        # Nothing configured is not a mistake: every flag is its own language.
        assert aliases_from("") == {}
    assert caplog.text == ""


# ---------- choosing ----------

def pick(streams, primary=("en", "ru"), limit=3):
    return st.pick(streams, primary=primary, limit=limit, aliases=ALIASES)


def test_the_last_slot_goes_to_a_second_primary_language():
    """Three English casts at the top, French fifth, Russian seventh: the third
    English one gives way to the FRENCH one, because it is the more watched of
    the two languages that could take the slot."""
    streams = [one("GB", 100), one("US", 90), one("GB", 80), one("BR", 70),
               one("FR", 60), one("BR", 50), one("RU", 40)]
    assert langs(pick(streams, primary=("en", "fr", "ru"))) == ["en", "en", "fr"]


def test_the_third_stays_when_no_other_primary_language_is_casting():
    streams = [one("GB", 100), one("US", 90), one("GB", 80), one("BR", 70)]
    chosen = pick(streams)
    assert langs(chosen) == ["en", "en", "en"]
    assert [item["viewers"] for item in chosen] == [100, 90, 80]


def test_the_quota_is_satisfied_without_touching_anything():
    streams = [one("GB", 100), one("RU", 90), one("GB", 80), one("FR", 70)]
    assert langs(pick(streams)) == ["en", "ru", "en"]


def test_one_wanted_language_beats_five_unwanted_ones():
    """A short list is the right answer: a link nobody in the chat can follow
    is not a fallback, it is noise."""
    streams = [one("BR", 100), one("BR", 90), one("BR", 80),
               one("BR", 70), one("BR", 60), one("GB", 10)]
    chosen = pick(streams)
    assert len(chosen) == 1
    assert chosen[0]["flag"] == "GB"


def test_other_languages_appear_only_when_there_is_nothing_else():
    streams = [one("BR", 100), one("ES", 90)]
    assert langs(pick(streams)) == ["br", "es"]


def test_no_quota_below_three_links():
    """With two slots, spending one on a less watched cast costs more than the
    rule gives."""
    streams = [one("GB", 100), one("US", 90), one("RU", 50)]
    assert langs(pick(streams, limit=2)) == ["en", "en"]


def test_zero_means_every_one_of_them():
    streams = [one("GB", 100), one("RU", 90), one("US", 80), one("RU", 70)]
    assert len(pick(streams, limit=0)) == 4


def test_zero_still_drops_the_languages_nobody_wants():
    """"All" is all of the POOL, and the pool is primary-only whenever a
    primary cast exists."""
    streams = [one("BR", 100), one("GB", 90), one("ES", 80)]
    assert langs(pick(streams, limit=0)) == ["en"]


def test_an_empty_language_list_means_any_language():
    streams = [one("BR", 100), one("GB", 90)]
    assert langs(pick(streams, primary=())) == ["br", "en"]


def test_choosing_does_not_trust_the_order_it_was_given():
    """The parser sorts, but the payload is JSON that has been through the
    database; the selection re-sorts rather than assuming."""
    streams = [one("RU", 5), one("GB", 100), one("US", 50)]
    assert [item["viewers"] for item in pick(streams)] == [100, 50, 5]


def test_an_australian_cast_is_english_and_keeps_its_place():
    """Straight off fixture 2397091. Without AU in the alias table the block
    would drop a cast on 155 viewers in favour of one on 8."""
    streams = [one("AU", 155), one("RU", 8)]
    assert [item["viewers"] for item in pick(streams)] == [155, 8]


# ---------- rendering ----------

def prefs(limit=3, languages=("en", "ru")):
    return st.StreamPreference(limit=limit, languages=languages, aliases=ALIASES)


def test_the_block_is_a_quote_of_icon_flag_and_link():
    block = fmt.stream_block([one("RU", 51, name="GLuck"),
                              one("WORLD", 41, provider="kick", name="DutchBoy")],
                             prefs())
    assert block.startswith("<blockquote>") and block.endswith("</blockquote>")
    assert "🟣 🇷🇺 <a href=" in block
    assert "🟢 🌍 <a href=" in block
    assert ">GLuck</a>" in block
    assert len(block.split("\n")) == 2


def test_no_block_when_the_reader_switched_it_off():
    assert fmt.stream_block([one("RU", 5)], None) == ""


def test_no_block_when_nobody_is_casting():
    assert fmt.stream_block([], prefs()) == ""
    assert fmt.stream_block(None, prefs()) == ""


def test_a_caster_name_cannot_smuggle_markup_into_the_message():
    """Names come off a web page and go into a message sent with
    parse_mode=HTML."""
    block = fmt.stream_block([one("RU", 5, name="<b>x</b> & <script>")], prefs())
    assert "<script>" not in block
    assert "&lt;b&gt;x&lt;/b&gt; &amp; &lt;script&gt;" in block


def test_the_multikill_message_carries_the_block_above_the_match_link():
    event = Event(type="E9", idempotency_key="k", match_id=1, payload={
        "team_name": "FORZE", "team_id": 1, "opponent": "Vitality",
        "opponent_id": 2, "event_name": "BLAST", "nick": "donk", "kills": 4,
        "map_name": "Mirage", "round": 12, "score_team": 7, "score_opponent": 5,
        "url": "https://www.hltv.org/matches/1/x",
        "streams": [one("RU", 51, name="GLuck")]})
    text = fmt.render(event, team_name="FORZE", tz_name="UTC", stream_prefs=prefs())
    assert text.index("<blockquote>") < text.index("Watch the match")


def test_a_multikill_without_the_block_is_unchanged():
    event = Event(type="E9", idempotency_key="k", match_id=1, payload={
        "team_name": "FORZE", "team_id": 1, "opponent": "Vitality",
        "nick": "donk", "kills": 5, "map_name": "Mirage", "round": 12,
        "score_team": 7, "score_opponent": 5, "url": "https://www.hltv.org/m"})
    text = fmt.render(event, team_name="FORZE", tz_name="UTC")
    assert "blockquote" not in text
    assert "ACE" in text


# ---------- storing them, and getting them into the event ----------

def test_the_stored_list_is_refreshed_and_never_wiped_by_an_empty_read(storage):
    """Rewritten on every observation — a caster on a hundred viewers when the
    match began can be behind three others on a thousand an hour later. But an
    empty parse is a page served mid-edit, not a match nobody casts, and losing
    every link over one such read cannot be undone before the next multikill.
    """
    storage.set_match_streams(7, [one("RU", 10)])
    storage.set_match_streams(7, [one("GB", 900), one("RU", 10)])
    assert [item["viewers"] for item in storage.match_streams(7)] == [900, 10]
    storage.set_match_streams(7, [])
    assert len(storage.match_streams(7)) == 2


def test_a_match_never_seen_on_the_page_has_no_streams(storage):
    assert storage.match_streams(12345) == []


def test_the_multikill_event_carries_the_streams(storage, config):
    """The WHOLE list goes into the payload. Which of them a reader sees is
    decided at render time, because the event is born once for everybody."""
    from hltv_notify.sources.scorebot import LiveFrame, PlayerLine
    from hltv_notify.state.db import utcnow
    from hltv_notify.state.live_machine import LiveMachine

    team, foe, match = 12857, 13973, 777
    storage.upsert_match(match_id=match, opponent_id=foe, opponent_name="Color",
                         event_name="Test", start_utc=utcnow(),
                         url="https://www.hltv.org/matches/777/x",
                         snapshot={}, snapshot_hash="x", team_id=team)
    storage.set_map_lineup(match, ["Mirage"])
    storage.set_match_streams(match, [one("RU", 51, name="GLuck")])

    def at(kills):
        return LiveFrame(
            map_name="de_mirage", current_round=4, round_state="started", live=True,
            ct_team_id=team, ct_team_name="us", ct_score=2,
            t_team_id=foe, t_team_name="them", t_score=1,
            regulation=12, overtime=3,
            ct_players=(PlayerLine(steam_id="1", nick="donk", kills=kills),))

    machine = LiveMachine(storage, config)
    machine.apply(match, at(0))                      # the round's baseline
    events = machine.apply(match, at(4))
    e9 = next(event for event in events if event.type == "E9")
    assert [item["name"] for item in e9.payload["streams"]] == ["GLuck"]


def test_a_multikill_on_a_match_with_no_streams_carries_an_empty_list(storage, config):
    from hltv_notify.sources.scorebot import LiveFrame, PlayerLine
    from hltv_notify.state.db import utcnow
    from hltv_notify.state.live_machine import LiveMachine

    team, foe, match = 12857, 13973, 778
    storage.upsert_match(match_id=match, opponent_id=foe, opponent_name="Color",
                         event_name="Test", start_utc=utcnow(),
                         url="https://www.hltv.org/matches/778/x",
                         snapshot={}, snapshot_hash="x", team_id=team)
    storage.set_map_lineup(match, ["Mirage"])

    def at(kills):
        return LiveFrame(
            map_name="de_mirage", current_round=4, round_state="started", live=True,
            ct_team_id=team, ct_team_name="us", ct_score=2,
            t_team_id=foe, t_team_name="them", t_score=1,
            regulation=12, overtime=3,
            ct_players=(PlayerLine(steam_id="1", nick="donk", kills=kills),))

    machine = LiveMachine(storage, config)
    machine.apply(match, at(0))
    e9 = next(e for e in machine.apply(match, at(4)) if e.type == "E9")
    assert e9.payload["streams"] == []
    text = fmt.render(e9, team_name="FORZE", tz_name="UTC", stream_prefs=prefs())
    assert "blockquote" not in text


# ---------- the number the whole list is ordered by ----------

@pytest.mark.parametrize("text,expected", [
    ("51", 51), ("155", 155), ("0", 0),
    ("1,234", 1234), ("1.2k", 1200), ("12K", 12000), ("3M", 3000000),
    ("", 0), ("—", 0), ("offline", 0),
])
def test_viewer_counts_are_read_in_whatever_shape_they_come(text, expected):
    """No fixture we have shows a tier-one match, so whether HLTV abbreviates
    large counts is unknown. Guessing wrong is not cosmetic: the list is
    ORDERED by this number, so an unreadable count on the biggest broadcast
    would sort it last — the opposite of what the block is for.
    """
    assert match_page._viewers(text) == expected


def test_an_unreadable_count_does_not_break_the_ordering():
    html = """
    <div class="stream-box" data-stream-provider="twitch">
      <div class="stream-box-embed" data-stream-embed="x">Big</div>
      <img class="stream-flag" src="/img/static/flags/30x20/GB.gif">
      <span class="viewers">12.5k</span>
      <div class="external-stream"><a href="https://www.twitch.tv/big"></a></div>
    </div>
    <div class="stream-box" data-stream-provider="twitch">
      <div class="stream-box-embed" data-stream-embed="x">Small</div>
      <img class="stream-flag" src="/img/static/flags/30x20/RU.gif">
      <span class="viewers">40</span>
      <div class="external-stream"><a href="https://www.twitch.tv/small"></a></div>
    </div>
    <div class="stream-box" data-stream-provider="twitch">
      <div class="stream-box-embed" data-stream-embed="x">Odd</div>
      <img class="stream-flag" src="/img/static/flags/30x20/RU.gif">
      <span class="viewers">who knows</span>
      <div class="external-stream"><a href="https://www.twitch.tv/odd"></a></div>
    </div>
    """
    assert [s.name for s in _boxes(html)] == ["Big", "Small", "Odd"]
