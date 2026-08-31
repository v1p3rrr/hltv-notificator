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
| E9 multikill | E10 reminder | E11 map point | E12 half / new overtime |

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
  http.py              the single point of egress to the network (curl_cffi)
  proxy.py             HTTP_PROXY/ALL_PROXY/NO_PROXY for all three sessions
  scoring.py           is the map over, from the score (thresholds from the format)
  scheduler.py         team-page polling, frequency modes
  match_poller.py      match-page polling, brings live workers up
  live_worker.py       the feed connection, reconnects, the supervisor
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

**There is one list of commands** — `bot.COMMANDS`, which generates both
the `/help` text and the `setMyCommands` payload (the list Telegram offers on
"/"). The same reasoning as `menu.MUTABLE`, and it had already gone wrong: the
hand-written `/help` never mentioned `/live`. `tests/test_commands.py` checks
both directions — nothing advertised that the bot cannot answer, nothing
dispatched that is not advertised.

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

**Non-ASCII logs bring the handler down on Windows.** The console is cp1252.
`setup_logging` forces the streams to UTF-8; in scripts use
`PYTHONIOENCODING=utf-8`.

**Escaping in patch scripts.** A `\n` inside a Python string you are patching a
file with turns into a real newline and breaks the source. For code edits Edit
is safer than `str.replace` from a heredoc.

## Commands

```bash
python -m pytest                                    # 423 tests
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
