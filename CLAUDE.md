# Project context for Claude

A Telegram notification service for CS2 team matches on HLTV. Python, one
process, run in Docker. Several Telegram accounts, each with its own team list
and its own mutes; the list is edited through the bot, `.env` only supplies the
whitelist and the first seed. There is one user and no web interface — the
interface is a chat with the bot.

**Write everything that goes into the repository in English** — code, comments,
docstrings, log messages, the bot's user-facing texts, tests and docs. Talk to
the owner in Russian.

**Spell out the event codes when talking to the owner.** `E6` is shorthand for
people who read the code; in a message it is "E6 (конец карты)" the first time
it comes up. The same for every other code. Nobody should have to go and look
up what a message is about.

| | | | |
|---|---|---|---|
| E1 new match | E2 reschedule | E3 cancellation | E4 match started |
| E5 map started | E6 map finished | E7 match finished | E8 / E8R degraded / recovered |
| E9 multikill | E10 reminder | E11 map point | E12 half time |
| E13 new overtime | | | |

A general description is in [README.md](README.md). How it works and why is in
[docs/architecture.md](docs/architecture.md). **Read it before making any
change**: almost every decision there came out of watching the live HLTV, and
without that context parts of the code look over-engineered and invite
"simplification".

## Working rules

**Keep the documentation current.** Change behaviour and fix the document in
the same commit, not "later":

| What changed | What to update |
|---|---|
| an event or a bot command appeared or changed | `README.md`, `docs/operations.md` |
| a decision, a component, a source mechanic | `docs/architecture.md` |
| something is deliberately not handled | `docs/known-limitations.md` |
| an environment variable, polling rates | `.env.example`, `docs/operations.md` |
| deployment, CI, image tags | `docs/deployment.md` |
| a new observation of HLTV's behaviour | the matching `docs/recon/R*.md` |
| a trap you were burned by | this file, the "Traps" section |

**Do not invent the source's structures.** If you have not seen a real server
response, you do not know how it is built. The net is full of 2015-2018
articles about HLTV with addresses like `scorebot.hltv.org:10022` — that is
stale. The source of truth is `docs/recon/` and the fixtures.

**Respect the source.** The ceiling of 1 request every 30 seconds is hardcoded
and cannot be raised by config. No parallel pools, no proxy rotation, no
captcha solvers. `403` means "back off", not "retry in a second".

**Events are born only on state transitions.** Not a style choice but the only
thing that saves you from an avalanche of duplicates. Details in the
architecture doc.

**Tests run on fixtures, not on the live source.** Matches are rare, and an
overtime, a dropped connection or a multikill cannot be reproduced on demand.

**Verify against real data whenever you can.** Half the bugs in this project
were found not by tests but by a run against the live HLTV. If a match is
running, run the service in `DRY_RUN` and look at the logs.

## Layout

```
src/hltv_notify/
  __main__.py          entry point, brings tasks up, stops them on a signal
  config.py            environment variables + the rate CEILING
  settings.py          the per-subscriber knobs; .env supplies the defaults
  http.py              the single point of egress to the network (curl_cffi)
  proxy.py             HTTP_PROXY/ALL_PROXY/NO_PROXY for all three sessions
  scoring.py           is the map over, from the score (thresholds from the format)
  scheduler.py         team-page polling, frequency modes
  match_poller.py      match-page polling, brings live workers up
  live_worker.py       the feed connection, reconnects, the supervisor
  streams.py           choosing which broadcasts go under a multikill
  replay.py            replaying a recorded dump through the state machine
  bot.py               bot commands
  sources/
    team_page.py       the schedule
    match_page.py      match state, per-map scores
    scorebot.py        the Engine.IO v3 client + frame parsing
  state/
    db.py              SQLite, schema, migrations
    machine.py         E1-E3 from the schedule
    match_machine.py   E4, E6, E7, E8 from the match page
    live_machine.py    E5, E6, E9 from the live feed
    multikill.py       the kill increment per round
  notify/
    audience.py        who a notification goes to (the only pause check)
    format.py          message rendering
    outbox.py          the queue with retries
    live_message.py    the live message per map (around the queue)
    telegram.py        the Bot API
docs/recon/fixtures/   real pages and feed recordings — the basis of the tests
scripts/               fixture collection: fetch_fixtures, record_scorebot, eio3
```

## Traps

Each one cost time. Do not step on them again.

**A button press must ALWAYS be answered.** Without `answerCallbackQuery`
Telegram spins the indicator until it times out and the person concludes the
bot has hung. We answer even when there is nothing to do.

**There is one list of mutable types** — `menu.MUTABLE`, which is also where
`MUTABLE_EVENTS` for the text `/mute` comes from. Two copies would drift apart
and a button would start muting what the command cannot. E8 is deliberately not
on the list: mute the alarm and you may never learn the service has gone blind.

**There is a third such list — `settings.SETTINGS`.** The `/settings` command,
the buttons, the defaults and the parser all come out of it. Same reasoning as
`menu.MUTABLE` and `bot.COMMANDS`.

**A per-person threshold cannot live in the machine.** An event is born once
for everybody, so the machine measures at the LOWEST bar in use
(`Storage.threshold_in_use`) and the QUEUE withholds it from whoever wanted
more — `outbox._wants`. The other direction is impossible: an event never born
cannot be given to the person who wanted it. Zero means "off" and is skipped
rather than being the minimum, or one person switching multikills off would
switch the tracker off for everybody. The comeback goes further still and is
decided in `format.comeback_line`, because it is a LINE inside E6 rather than
an event, and the deficit floor is derived from the bar so it has to be
recomputed for the reader too.

**An environment variable behind a `/settings` knob is a DEFAULT, not an
override.** The consumer must not check the config first and return early:
`LiveMessenger.update` did, and with `LIVE_MESSAGE=false` no `/settings card
on` could ever be satisfied. The check belongs where the recipient is known —
`_recipients` — and a row in `subscriber_settings` exists only once someone
changes something, so raising a default still reaches everybody who left it
alone.

**There is one list of commands** — `bot.COMMANDS`, which generates both
the `/help` text and the `setMyCommands` payload (the list Telegram offers on
"/"). The same reasoning as `menu.MUTABLE`, and it had already gone wrong: the
hand-written `/help` never mentioned `/live`. `tests/test_commands.py` checks
both directions — nothing advertised that the bot cannot answer, nothing
dispatched that is not advertised.

**"Never happened" is None, not zero, whenever the clock is
`time.monotonic()`.** It counts from the machine's boot, so on a freshly
started host it is a small number and `now - 0.0` looks recent. The refusal
log was swallowed for the first ten minutes of uptime because of it — green
on a developer's machine with days of uptime, red on a CI runner booted a
minute earlier. `_log_refusal` and the card's edit throttle both check
`is not None` now.

**CI runs Linux and Python 3.12, the dev machine does not.** When the suite
is green locally and red in Actions, reproduce it in the container above
before touching the tests.

**Nothing answers a chat that is not on the whitelist, and the refusal is
logged at most once per chat per ten minutes.** That log line is the only
way to learn the id of a GROUP that is not allowed yet — @userinfobot reports
a person's id, not a group's — so it must not be throttled away entirely; and
it is also the only thing an outsider can make the bot do, so under the
container's log rotation an unthrottled line would evict the history.

**`TELEGRAM_CHAT_ID` is required even with the whitelist off.** The first id
is the main chat, and without it `telegram_enabled()` is false and the command
bot never starts. Startup warns about it: a silent bot looks like a dead
service.

**The Telegram rate limit lives in the CLIENT, not in the senders.** Four
things write out — the queue, the live card, command replies, button
acknowledgements — and they share one budget. A limiter per sender leaves
everybody inside their own rules and the total over. Per-CHAT spacing stays
in the queue, because that is also where the ordering guarantee is.

**The settings store now holds TWO value types, and the writers must blank each
other.** `subscriber_settings` has `value INTEGER NOT NULL` and `text_value
TEXT`, and a NON-NULL `text_value` is what makes a row textual — that is the
whole type test, used by `settings_for`. So `set_setting` writes `text_value =
NULL` and `set_text_setting` writes `value = 0`. Leave either out and a name
that changed kind is read back through the wrong column. Which column a name
uses is decided in ONE place, `settings.Setting.kind`; nothing else may guess.

**`Setting.kind` replaced `boolean` rather than joining it.** Two type flags
can both be true, and every consumer would then have to decide which wins —
`describe`, `parse_value`, `range_hint` and `menu.settings_screen` all switch
on it. Same reasoning as the "one list" rule, one level down.

**Zero does not always mean off — `Setting.zero_word` says what it means.**
`streams_count` uses zero for "list every one of them", and there is a real off
switch (`streams`) beside it. The word is read in two places: `describe`, so
the setting does not report "off" while showing all the streams, and
`parse_value`, which must REFUSE "off" where zero does not mean off — otherwise
the word for "none" would store the value for "all".

**An on/off word is a WORD, never data — and for a free-text setting nothing
refuses it for you.** `/settings streams_langs on` stored the primary language
"on": two letters of the alphabet is all a language code has to look like, so
every shape check passed. No flag matches it, so `pick` found no primary stream
and fell back to "every broadcast in every language" — the filter switched
itself off while the reply read "Stream languages: on". The vocabulary lives in
ONE place — `settings.ON_WORDS` / `OFF_WORDS` / `ANY_WORDS` — because a word
that stops being recognised as a word is not rejected, it is stored. `is_reset`
is what says which words mean "back to the service default": `on` does for a
list, and must NOT for a number, where it goes through the -1 signal that knows
how to say "the default is itself off, so yours stays".

**And check the vocabulary against the DATA before adding to it.** Sweeping the
whole off list into the language setting made `no` mean "any language" — but
`no` is Norwegian (`NO.gif` -> `no`), the one off word that collides, so
somebody asking for Norwegian casts was answered with all of them.
`LIST_ANY_WORDS` subtracts it, and that is the entire reason the constant
exists rather than the expression being written inline.

**A lenient parser must DROP what it cannot read, never guess.**
`STREAM_LANGUAGE_ALIASES` took three attempts, and each of the first two fixed
one shape of input by breaking another. Splitting groups on whitespace lost
`en: GB, US` completely — four pieces, none parseable, table EMPTY. Squeezing
the whitespace out around every separator then read the comma in
`en:GB,US, ru:BY` as belonging INSIDE English and stored a flag called
"ru:BY". Making the colon the only thing that opens a group fixed both and
introduced the worst one yet: a group that lost its colon (`en:GB,US ru BY`)
no longer went missing, it mapped `RU` onto English and labelled every Russian
broadcast English. The rule that holds is TWO rules — a colon opens a language
and its comma-separated flags follow it; anything after a SPACE with no colon
of its own is dropped and logged. Which separator preceded a token is
information the parser needs, so `re.split` keeps them.

Every one of those failures was silent, and an alias table is invisible when
wrong: the block still appears, it just has the wrong broadcasts in it. Note
that the "parsed to nothing" WARNING caught none of them — a half-parsed table
is not an empty one — which is why the drop is logged per token. And note the
direction: an unclaimed flag is still its own language, so dropping is right
where guessing is wrong everywhere it is read.
`tests/test_streams.py` now asserts every shape at once, working and broken,
so a fourth version cannot trade one for another.

**Only Twitch and Kick get stream links, and the host is checked, not the
provider.** HLTV lists YouTube too, and there is no clip button there, so a
link is a dead end. `data-stream-provider` says what HLTV calls it;
`STREAM_PROVIDERS` also pins the hosts the href may reach, because that href
comes off a web page and `HLTV_BASE + href` was a real SSRF here once.

**`set_match_streams` overwrites on every poll but ignores an empty parse.**
Those are two different guards and it is easy to copy the wrong one: the
`set_map_lineup` call right beside it is guarded against recording a veto of
"TBA", whereas here the newest order is always better — a caster on a hundred
viewers at the start can be behind three others on a thousand later. But an
empty read is a page served mid-edit, not a match nobody casts, and there is no
way back from wiping the list before the next multikill.

**THREE stores hold an event type, and splitting one reaches all three.**
The journal's keys (`sent_events`, `outbox`), the per-person setting
(`subscriber_settings.name`) and the per-team mute list (`teams.muted_events`).
Splitting E12 into E12 (half) and E13 (overtime) migrated the first two and
missed the third, so a subscriber who had muted "half / overtime" started
receiving overtimes again. Each migration gets its OWN flag in `meta`: sharing
one means a database that already ran the earlier half never gets the rest.

**Angle brackets in a reply are HTML, and Telegram answers 400.** Replies go
out with `parse_mode=HTML`, so a placeholder like `<off, or 2-5>` is an
unsupported start tag and the message never arrives — and the failure is
invisible from inside, because the bot only logs "could not reply". `_help_text`
already escapes its arguments for this reason; anything else that wants angle
brackets must escape them too, or say it in prose. `tests/test_bot.py` now runs
every command and checks the reply against Telegram's tag list.

**A setting must not accept a value the code overrides.** `MultikillTracker`
floors its threshold at 2, so `/settings multikill 1` stored a number the
alerts never use and reported it back as if it were in force — the refusal
message even suggested it. `Setting.smallest_on` is that floor, and `clamp`
keeps zero as zero so "off" is still reachable.

**Do not clear a stored value before knowing what replaces it.**
`/settings <name> on` means "back to the service default"; it deleted the row
first and only then discovered the default was itself off, so the command a
person types to turn something ON deleted the value that was keeping it on.
Work out the replacement first, and say what happened to theirs.

**A field nothing reads is a trap, and this has now happened twice.**
`Setting.minimum` survived one commit after `clamp` and `parse_value` stopped
consulting it, so a future `minimum=3` would have been silently ignored; and
before it, `Config.phase_alerts`. If a field stops being read, delete it in the
same change.

**An umbrella environment variable must not also be a field.** `PHASE_ALERTS`
is the fallback both `HALF_ALERTS` and `OVERTIME_ALERTS` read, and it is
resolved inside their factories, from the environment. A `phase_alerts` field
nothing reads is a trap: `Config(phase_alerts=True)` looks like it turns both
on and does nothing at all — which is exactly how two tests were silently
wrong.

**Changing a key format needs a migration in the same commit.** The journal
is keyed by the old format, so without one the first run after the upgrade sees
nothing matching and sends again what it has already sent. There are two of
them now: `adopt_legacy_event_keys` (the chat prefix) and
`_migrate_reminder_keys` (E10 gaining the start). Both are one-off under a flag
in `meta`, and `tests/test_migration.py` opens an old database with the current
code.

**A key must carry everything the message asserts.** E2 has the new time in
it; E10 did not, so a reminder fired once for the old time and the journal
then blocked it forever — a match moved after its reminder went out got no
second one. If the payload says "at 18:00", the key says 18:00 too.

**Stopping a worker is a flag first and a cancel later.** `release` used to
do both in one breath, so the flag was never seen and the cancel could land
inside the Telegram call creating a card — message posted, id unsaved, second
card on the next start. `RELEASE_GRACE_SECONDS` is not there to let it exit
gracefully (the long poll holds for 45 s); it is there to outlast a write.

**The final edit of a card waits for any redraw in flight.** The background
draw reads the row before `finalized` is written and finishes after it, and
`save_live_message` does `finalized = excluded.finalized` — so the freeze was
cleared and the stale score became the card's last text. `_settle` waits
(never cancels: a cancel inside `send_message` loses the message id and the
next start opens a second card).

**"Something was sent below the card" is a COUNTER, not a flag.** The card is
deleted and re-sent after E11/E12/E13 so it stays the last message, and that takes
a moment. A boolean cleared at the end of the move erases a burial that arrived
during it, and the card then sits above that message for the rest of the map.
`bury_seq` is incremented; the move writes back into `posted_seq` the value it
READ. Same shape as the `finalized` race `_settle` exists for.

**The card is moved by the QUEUE, not by the feed.** The obvious trigger — the
next redraw — fails at exactly the moment it is needed: half time is when the
feed falls silent, so the card would stay above the half-time message for a
minute. `_drain_chat` calls `repost_buried` after delivering a chat's messages,
which also gives the ordering for free, since that loop serves one chat in
order. `_update_one` keeps a copy of the logic as a safety net for a restart,
but it must NOT call `_settle` — it runs inside `_draw` and would wait on
itself.

**The card's whole write path is under `_moving`, not just the move.**
`_drawing` serialises the feed's redraws, but the queue moves cards from its
OWN task, and between that move's delete and its send the row carries NO
message id. A redraw reading the row in that window concludes there is no card,
sends its own, and the map ends with two — one of them an orphan nobody edits
again. It fires on E11, when the feed is certainly running, so it is the
ordinary case. Everything read before the lock is stale by definition: re-read
the row inside it, the id and `finalized` included.

**A move must carry `finalized` through.** `save_live_message` defaults it to
False and writes `finalized = excluded.finalized`, so a move that omits it
unfreezes a card frozen while the move was in flight — and the final score is
then overwritten by later frames. `_buried()` checks `finalized` too, because
the query that found the card ran before `_settle`.

**Bury only the CURRENT map's card.** `finalize` runs only when the live
machine emits E6, so a map that ends while the feed is down leaves its card
unfinalized forever. Burying every unfinalized card of the match drags that
stale one to the bottom of the chat next to the running map.

**A newly created card owes nothing to an older burial.** It lands at the
bottom by construction, so its `posted_seq` is caught up on creation. Without
that it is born already "buried" — after a re-send that failed, say — and the
next redraw deletes and re-posts the message that had only just appeared.

**A deleted card must have its id forgotten before the re-send.**
`save_live_message` COALESCEs `telegram_message_id`, so writing NULL through it
keeps the old one. If the send after a successful delete fails, the row would
point at a message that no longer exists and every later redraw would edit a
ghost. `forget_live_message_id` is a separate write for that reason.

**The live card must not be awaited by the frame loop.** It is one message
per subscriber; a hundred of them is ten seconds of sequential calls with no
frames read. `submit()` hands over the newest snapshot and drops whatever it
overtook. Creating the card and the final edit stay awaited — their result
is needed.

**Upgrading an old database must preserve the journal.** Keys written before
subscribers existed get a chat prefix once (`adopt_legacy_event_keys`). Without
that, the very first run of a new version would treat everything it had already
sent as new and send the history again. There is a test for it —
`tests/test_migration.py` assembles a first-version database and opens it with
the current code.

**Recipients are computed in ONE place — `notify/audience.py`.** The pause
check lives there too. The rule "check it in two places" has already failed
here twice: `subscribers_tracking` knows nothing about the pause, and first the
pause did not work for match events, then for the live score message. If a third
delivery path appears, it must go there too rather than growing its own
computation.

**The event journal key includes the recipient** (`<chat>|<key>`). Without it an
event about a shared match would reach only the first subscriber: the unique
index would cut the rest off as duplicates.

**Every event payload names its own team — `team_name` AND `team_id`.**
`format.render` falls back to `config.team_name` when the payload is silent,
and that is the FIRST SEED's name, not the reader's team: E1, E2 and E3 carried
no team at all, so a subscriber following Natus Vincere was told about a new
match of "FORZE Reload". The id matters just as much: without it `format.orient`
has nothing to compare against, hands the payload back untouched, and in a
match between two tracked teams the second one reads its own opponent as
itself. The live machine gets this right in one place — `_context` — and the
schedule machine had no such place. `tests/test_multi_team.py` now renders every
schedule event from BOTH followers' sides.

**An event is built entirely from one team's point of view.** Take the canonical
team's context and swap only the name in it — that is how E9 about a player of
the second tracked team ended up with that same team as its opponent
("FORZE — FORZE") and a mirrored score. It cannot be turned around afterwards:
`format.orient` sees that the payload's `team_id` matches the recipient's team
and concludes there is nothing to flip.

**The score is turned around at render time, not in the machine.** The event is
oriented on the canonical team; `format.orient` flips it for whoever follows the
opponent. That logic must not be dragged into the machine — there is one event
and many recipients.

**The match perspective is chosen once and does not change.** It is stored in
`matches.team_id` — the team that saw the match first — and `canonical_team()`
reads exactly that. Allow it to be overwritten and adding a second team flips
the score, the keys become mirrored and everything already sent goes out again.
In `upsert_match` the COALESCE argument order is exactly this, and it must not
be changed.

**A guard is useless if it stands on something nobody reads.** That very
COALESCE spent months protecting `matches.team_id` while all the machines asked
`canonical_team()`, and it returned `MIN(match_teams.team_id)` — so the
perspective flipped away happily. There was a test for it, but it asserted on
the unused column. Assert on the value the code actually WORKS from.

**The first-run flag is per team.** A shared one would make adding a team
through the bot noisy: an E1 for every match it has already played.

**One state field, one writer.** The most expensive class of bug in this
project. The page machine and the live machine shared `current_map_name` and
drew conclusions about their own history from it — and both got it wrong. Now
each has its own private memo: `live_map_name` is written and read only by the
live machine, `page_seen_utc` only by the page machine. `current_map_name`
remains a DISPLAY field; decisions must not be made from it.

**Do not derive "I have seen this already" from last_source.** The live feed
rewrites `last_source` several times a second, so for the page the marker "I am
seeing this match for the first time" was permanently true and page-side E6
stayed silent the whole time the feed ran. The marker has to be separate and
monotonic.

**The page puts the UPCOMING map into the state.** With no map running,
`_current_map` takes the first unplayed one. Compare that with the map from the
feed and "the map started" looks like "the map has not changed", so E5 is never
born.

**Testing the machines separately does not catch seam defects.** Both bugs above
slipped past 175 tests because each machine was checked in isolation. There is
`tests/test_page_and_feed_together.py` — extend it whenever either machine
changes.

**An unscoped `[data-unix]` picks up somebody else's time.** On the match page
the featured-matches widget `.fbw-vp-header-time` comes first in the DOM.
`.timeAndEvent [data-unix]` is required, otherwise the service sends false E2s.

**A running map has a numeric score too.** "There is a score, so it is played"
is wrong: that is the current score. The `won`/`lost` classes do not save you,
they mark the current leader. The signal is the `.results-stats` link.

**`readyForMatch` takes a JSON STRING, not an object.** With an object the
server stays silent: the connection is alive, there is no error, there are no
frames.

**You may only subscribe after packet `40`.** On a reconnect it does not arrive
with the handshake. Send earlier and you get the same silent refusal.

**The feed's log is replayed on every connect.** Across a two-map series 150
`MatchStarted` events were counted. That is why decisions are made from
`scoreboard` frames and the log is not used anywhere.

**Scorebot's websocket is closed (403), polling works.** Verified with every
header, cookie and impersonation profile, including `curl_cffi.ws_connect`.

**The `live` flag is not a "the map has started" signal.** Measured on a
recorded map boundary: it turns true only once the FIRST ROUND HAS BEEN PLAYED.
The sequence is `warmup/live=False` (177 frames), then `started/live=False`
(64 frames — the map really running), then `ended/live=True` with the score
already 0:1. The only reliable warmup signal is `currentRoundState == "warmup"`.

**The page's LIVE flag is not the start of play.** HLTV raises it when the
teams connect to the server, and the warmup before the first map runs twenty
minutes. So E4 is written by the page but put aside (`start_event:<match>`)
while the feed reports a warmup, and the live machine sends it under the same
key on the first real round. Without a feed the page still decides on its own —
losing E4 would be worse than sending it early.

**The page must not decide "already announced" from the shared state.** Both
machines write `MatchState.LIVE`, so "previous != LIVE" is not the page's
transition — if the feed had written it first, the page would stay quiet
forever. Use the page's own memo, `page_seen_utc`.

**A message that continues another must not overtake it.** The card goes
straight to Telegram, events wait in the queue: after releasing a held E4 the
worker holds the card until that message has left the queue. Same class of bug
as E5 racing its own score. **The guarantee is per chat**, which is what makes
the queue's concurrency safe: `_drain_chat` serves one chat strictly in queue
order, and only different chats are served at the same time. Two messages for
one person must never end up in two tasks.

**The live message is not opened during the warmup.** It IS the map's card —
it carries E5, it says the map has started — so creating it at 0:0 in a warmup
that can run twenty minutes is a lie told several times a second. Only creation
is held back: a warmup in the middle of a map finds the card already there.

**The debounce must not outlive what it is debouncing.** E2 collapsed a burst
of reschedules over ten minutes — including a move seen three minutes before
the start, which would have been announced nine minutes into the match. And the
other half of it: `upcoming_matches` judged by the CONFIRMED start, which during
a debounce is stale by definition, so the match dropped out of "upcoming" at its
old time and the schedule fell to idle. Both halves cost the same
notification. And the third: HLTV moves a match AFTER its slot has arrived, so
one that should have started and has not keeps the frequent cadence
(`matches_awaiting_start`) — otherwise nobody is looking at the page precisely
when the moves happen.

**The live message carries E5, and it is not a stylistic choice.** The two
delivery paths differ: the live message goes straight to Telegram, an event is
merely queued and the outbox worker wakes every five seconds. So a separate E5
ALWAYS lost the race to its own map's score — measured 3 seconds behind. If a
third such message ever appears, put it in the card too rather than adding a
fourth race.

**"Recovered" requires an alarm that was SENT, not one that was counted down.**
Those are different moments, and elapsed time cannot tell them apart: a
subsystem is only re-checked on its poller's cycle, so a 0.7-second outage can
look like a minute and a 35-minute one can pass without any alarm. The flag is
`degraded_alerted:<subsystem>`; both halves of this were seen in production.

**A long-poll timeout is not a disconnect.** The feed goes quiet during pauses,
and the whole break between maps passes like that. See `FeedIdle`.

**Kills in a frame are accumulated over the MAP, not the round.** Without
resetting the baseline every round, each subsequent round looks like a
multikill.

**Tie the score to `ctTeamId`/`tTeamId`, not to the side.** Sides swap after the
break.

**"First observation" means the first FROM THE MATCH PAGE.** The state row is
created by the schedule polling, so a "there is no state yet" check is almost
always false, and maps played before the observation began were getting E6
retroactively.

**Ordinary HTTP clients get a 403.** The filtering is by TLS fingerprint, not by
headers. Permuting them is useless — only `curl_cffi` with impersonation.

**Different accounts share ONE variable, `TELEGRAM_CHAT_ID`, ids separated by
commas.** The first one is the main chat: the team seed from `TEAM_ID` and
single-user mode are addressed to it (`config.main_chat_id`, not
`config.chat_id` — the latter is now the raw list string).
`TELEGRAM_ALLOWED_CHATS` has been removed; the service warns at startup if it is
still in the environment, because a silently empty whitelist looks like "the bot
died".

**The queue is not cancelled on shutdown.** A cancel in the middle of
`send_message` leaves the message sent but not marked in the database, and on
the next start it goes to the person a second time. The queue task exits on its
own by the stop flag and makes a final pass with a deadline; `__main__` waits
for it with `wait_for` and cancels only everything else.

**libcurl ignores uppercase `HTTP_PROXY`.** Deliberately, a CGI legacy. And
uppercase is exactly how it is written in `docker-compose.yml`, so the service
reads
the variables itself (`proxy.py`) and passes them into the session explicitly.
The non-obvious part lives there too: when an address matches `NO_PROXY` the
proxy is set to an EMPTY STRING rather than "not set" — otherwise libcurl picks
the environment variable up itself and the bypass does not work.

**Do not concatenate the base address with a string from the source.**
`HLTV_BASE + href` was a genuine SSRF: the base does not end in a slash, so an
`href` starting with `@` or a dot steered the request to a foreign host
(`www.hltv.org` became userinfo) and the service then went there every minute.
Addresses are assembled from validated numbers, and `config.ALLOWED_HOSTS`
checks the host at the network egress itself. Check the `hostname` from
`urlparse`, not `startswith` — the attack is built on exactly that.

**A scanner's CVE count is mostly Debian's, not ours.** Of ~150 findings on
the image, everything with a fix is the base image's own lag (`python:3.12-slim`
freezes Debian as of ITS build date) plus `pip`, which nothing at runtime needs.
The `Dockerfile` handles both — `apt-get upgrade` and `pip uninstall -y pip` —
and after that `trivy --ignore-unfixed` comes back empty. What remains has no
Debian patch at all: the three CRITICALs are `perl-base`, pulled in by `dpkg`
and never executed here. Judge the image by `--ignore-unfixed`; the raw total
is noise and chasing it invites a base-image swap that fixes nothing.

**Non-ASCII logs bring the handler down on Windows.** The console is cp1252.
`setup_logging` forces the streams to UTF-8; in scripts use
`PYTHONIOENCODING=utf-8`.

**Escaping in patch scripts.** A `\n` inside a Python string you are patching a
file with turns into a real newline and breaks the source. For code edits Edit
is safer than `str.replace` from a heredoc.

## Commands

```bash
python -m pytest                                    # 581 tests
docker run --rm -v "/d/Documents/Claude Projects/HLTV:/app" -w /app \n  python:3.12-slim sh -c "pip install -q -r requirements.txt pytest && python -m pytest"
                                                    # what CI actually runs
PYTHONIOENCODING=utf-8 PYTHONPATH=src DRY_RUN=true python -m hltv_notify
PYTHONPATH=src python -m hltv_notify.replay <dump.gz> --team-id N --match-id M --twice
python scripts/fetch_fixtures.py                    # rebuild the HTML fixtures
python scripts/record_scorebot.py <match_id> <file.gz> --duration 3600
docker compose up -d --build && docker compose logs -f
```

Git: the repository is **its own**, inside the project folder (the parent
`D:\Documents` is somebody else's repository, do not commit there). The remote
is over **SSH** (`git@github.com:v1p3rrr/hltv-notificator.git`) — the HTTPS
credentials in the credential manager do not fit.

## How to capture new fixtures

Find a live match on `hltv.org/matches` (the live section). Then:

* page HTML — `scripts/fetch_fixtures.py`, by editing the `PAGES` list;
* a feed dump — `scripts/record_scorebot.py <match_id> <file> --referer <match url>`.

What is valuable is the moments that cannot be reproduced: **the map boundary**,
an overtime, a dropped connection, a multikill. If you catch a live match, take
one that is **on its first map** — then the boundary will definitely happen.

The dump collapses consecutive identical frames and marks disconnects — the
deduplication tests are built on that.

## Status

Stages 1-5 of the plan are closed, plus beyond the plan: several tracked teams
managed through the bot, the live score message, multikill alerts and a
degradation watchdog with an urgency-dependent threshold.

Discussed but not done: HLTV's mobile JSON endpoint as a second schedule source
(requires proxying the app's traffic, see recon/R3).

## What not to do

Do not widen the scope to other games and sites. Do not build a web interface,
dashboards, Prometheus metrics or microservices. Do not raise the polling rate
to "keep up" — if we are not keeping up, that is an argument for the live feed.
Do not keep secrets in the repository. Do not send notifications around the
state machine.
