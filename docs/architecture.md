# Architecture

This document answers "why this way", not just "how". Almost every decision
here came out of watching the live HLTV, and without that context parts of the
code look over-engineered.

## The overall shape

```
┌──────────────────┐  team page, every 3-30 min
│ SchedulePoller   │──────────────┐
└──────────────────┘              │
                                  ▼
┌──────────────────┐        ┌──────────────┐        ┌──────────────┐
│ MatchPoller      │───────▶│    State     │───────▶│  Notifier    │
│ match page       │        │   machines   │ events │  queue +     │
└────────┬─────────┘        │  + Storage   │ with a │  retries     │
         │ brings up        └──────────────┘  key   └──────┬───────┘
         ▼                         ▲                       ▼
┌──────────────────┐               │                  Telegram
│ LiveSupervisor   │───────────────┘
│   LiveWorker     │  live feed, only while a match runs
│   (one per       │
│    match)        │──────▶ LiveMessenger ─────────▶ Telegram (editMessageText)
└──────────────────┘        the live message, around the queue
```

The components do not call each other directly. Sources record observations,
the state machines give birth to events, the notifier sends them. There is one
reason for the split: **deduplication lives in one place** rather than being
smeared across the code.

## The main principle: an event is born on a TRANSITION

This is not a matter of style but the only thing that saves you from an
avalanche of duplicates.

* The match page is polled every minute and says the same thing all that time.
* The live feed sends the full scoreboard state **several times a second**.
* On every connect the feed **replays its history from the beginning**. Over a
  recording of match 2397053 there were 15 connects in an hour, and across a
  two-map series **150 `MatchStarted` events** piled up.

So the logic "we saw a score of 13, send a notification" is guaranteed to
produce duplicates. Instead: the observation is compared with the stored state,
and an event arises only if the state changed. For the same reason **the feed's
log events (`Kill`, `MatchStarted`, `RoundEnd`) are not used anywhere** —
decisions are made from `scoreboard` snapshots.

## Idempotency

The second line of defence is the unique index on
`sent_events.idempotency_key`. Recording the event and queueing the message
happen in **one transaction**: if the journal were written and the queue were
not, the notification would be lost forever, because it will never be born
again.

There is deliberately no separate "is this already in the database" query —
that would be a race. An insert either goes through or does not.

The keys:

```
E1:<match>:new
E2:<match>:moved:<new_time_utc>
E3:<match>:cancelled
E4:<match>:started
E5:<match>:map:<n>:started:<map>
E6:<match>:map:<n>:result:<ours>-<theirs>
E7:<match>:finished:<maps_ours>-<maps_theirs>
E8:<subsystem>:<reason>:<utc_hour>
E9:<match>:map:<n>:round:<r>:<steam_id>:<kills>
E11:<match>:map:<n>:point:<us|them>:<target_score>
```

A key must depend **only on the content**. Let the time the response arrived
into it and deduplication stops working. The hour in the E8 key is a
compromise: we do not send "I have gone blind" on every failed attempt, but
neither do we mute the problem forever.

## The reschedule (E2) and its two deadlines

Moving a match back and forth is routine, so a shift under
`E2_MIN_SHIFT_MINUTES` is accepted silently and a bigger one waits out
`E2_DEBOUNCE_MINUTES` before it is announced — a burst of edits then collapses
into one message carrying the last value.

The window has a hard end: **it never runs past the start.** With the earlier
of the two times already inside the window there is no time left to debounce,
and the reschedule goes out on the first sighting. Measured on match 2397343:
the move 18:00 -> 18:20 appeared on the page at 17:57:43, the window still had
seven minutes to run, and E2 would have been born at 18:29 — nine minutes into
a match that had started at 18:20.

And once the new time itself has passed, E2 is not sent at all. It is no longer
news but history, and E4 is about to report the start anyway.

That case also exposed a second half of the same bug. Everything hangs off
`upcoming_matches`: the polling cadence, the reminders, `/next`. It judged by
the CONFIRMED time, which during a debounce is stale by definition — so at
18:00 the match dropped out of "upcoming", the schedule fell back to idle
(polling every half hour) and the next look at the page came at 18:29. The
query now takes the pending time where there is one, and keeps the confirmed
one available as `confirmed_start_utc`.

## The sources and their quirks

### The team page — the schedule

`/team/<id>/<slug>`. Chosen instead of the obvious `/matches?team=<id>`,
because the latter is **disallowed by robots.txt** while the data is the same.

The time comes from the `data-unix` attribute (epoch in milliseconds), not from
the text: HLTV renders the time in the browser's timezone, and "17:00" means
different things to different readers.

Our own team comes first on its page and the score is given from its point of
view — but the parser does not rely on the ordering and matches by id.

### The match page — state and per-map scores

**Trap one.** An unscoped `[data-unix]` selector picks up **another** match's
time: the first thing in the DOM is the `.fbw-vp-header-time` widget with
featured matches. `.timeAndEvent [data-unix]` is mandatory, otherwise the
service sends false E2 events.

**Trap two, the more expensive one.** A **running** map also has a numeric
score in its `.mapholder` — that is the current score, not the final one. The
rule "there is a score, so the map is played" would have sent an E6 with an
in-play score (caught on a live match: the section read 5:7 while the real
score was 12:11). The `won`/`lost` classes do not save you: on a running map
they mark the current leader.

The completion signal is the **appearance of the `.results-stats` link** to the
map statistics: HLTV creates that record exactly at the moment the map ends.
For a finished match the signal is backed by the page status, because a forfeit
may have no statistics at all.

### The live feed (scorebot)

`scorebot-lb.hltv.org`, **Engine.IO v3 over polling**. The websocket upgrade
returns **403** to every non-browser client — verified with bare `websockets`
and with `curl_cffi.ws_connect`, with Origin, Referer, a browser UA and warmed
cookies. Polling goes through even on a cold session, and a browser starts with
it itself.

Two details, each of which produces a **silent failure with no error**:

* the `readyForMatch` argument must be a JSON **string**, not an object;
* you may only subscribe **after** packet `40` — on a reconnect it arrives not
  with the handshake but on the next poll.

A long poll with no data (45 seconds) is **normal**, not a disconnect: the feed
goes quiet when the map is paused, and the whole break between maps passes like
that. Treating it as a disconnect means reconnecting every 45 seconds exactly
when you are waiting for the next map to start.

The meaning of the `live` flag is "the map is in play", not "the match is
running": at the end of a map it stays `true`, and during the next one's warmup
it becomes `false`.

Details and raw measurements: [recon/R4-scorebot.md](recon/R4-scorebot.md).

## The map-completion rule (D7): two sources, different roles

**The feed decides, the page confirms.**

The feed knows the round score immediately, so E6 is born from the score at the
moment of the winning round. The thresholds are computed by `scoring.py` from
`regulationHalfLength` and `overtimeHalfLength`, which the feed itself sends:
13, then 16, 19, 22. No hardcoded "13 rounds" anywhere — the first MR3 overtime
or non-standard format would break it. 12:12 and 15:15 do not count as a
finished map.

The page gives the same result but **later**: its maps section updates by
halves. It remains the source of truth for the cases arithmetic does not cover
— a forfeit, a technical loss, a team withdrawing.

There will be no duplicate with two sources: the event for a map is born once,
whoever brings it first (a guard on the recorded maps plus the unique key).

## Multikills (E9)

Computed from the **increment in kills in scoreboard frames** between the start
of a round and the current moment, not from `Kill` events. The reason is the
same as everywhere: the log is replayed on connect. A side benefit is the alert
at the Nth kill rather than at the end of the round.

Kills in a frame are accumulated **over the map**, so the baseline is reset on
every round; without that every subsequent round would look like a multikill.
The warmup is ignored — that is deathmatch.

Errors lean the safe way: after a reconnect mid-round the baseline is taken
afresh, so a multikill can be **missed but never invented**.

## Map point (E11)

One round from taking the map — time to stop what you are doing and watch.

The threshold is not written down anywhere in this feature: it asks
`hltv_notify.scoring.rounds_to_win`, the same module that decides the map is
over. The warning therefore cannot drift apart from the result it warns about,
and it follows the format the feed itself reports rather than a hardcoded 13.

**Every overtime brings its own map point.** Under MR12/MR3 the target moves
13, then 16, then 19, so a series of overtimes produces a warning per overtime
— which is the point, since that is exactly when a map is most likely to end
at any moment. The target is part of the idempotency key, which is what keeps
them apart; the leader is in it too, because at 11:12 the map point is theirs
and at 12:11 it is ours, and those are two different warnings.

A repeat is not born while the round is played out: the score stands at map
point for a whole round, i.e. some hundreds of frames. The journal would
swallow them anyway, but the machine keeps an in-memory memo so the queue is
not written to hundreds of times. After a restart the memo is gone and the
journal takes over.

The message also says whether taking this map ends the whole MATCH — that is
the difference between "get ready" and "it is over in a minute", and it comes
from the same `series_decided` the end-of-match event uses. It is symmetric
between the two teams, so unlike the score it needs no turning around.

Both teams get a warning. A map point against us is the more urgent of the two,
and the message is written from the score, not from a stored "whose": the score
is turned around for a subscriber who follows the opponent, and a "whose" field
would not have turned with it.

## The live message

It goes **around the outbox queue**, and that is deliberate: it has no
idempotency key, and there is no point re-delivering a stale score frame after
a failure — the next edit a few seconds later brings the current one. The
milestones meanwhile go through the queue and are not lost.

The message id is kept in the database: otherwise a restart would start a
second live message for the same map. The lower bound on the edit interval
(5 seconds) is hardcoded and cannot be worked around by config — but it applies
to edits only, never to creating the message, or the map's card would be held
back by a whole interval.

**The live message is also the map's card: it carries E5.** The two used to be
separate messages, and the order between them was always wrong. The reason is
the two delivery paths: the live message goes straight to Telegram, while an
event is only *queued* and the outbox worker wakes every five seconds and
spaces sends 1.2 s apart. Measured on a real match: E5 queued at 09:13:24.905,
the live message created at 09:13:25.035, E5 actually delivered at 09:13:28.189
— the score for a map arrived three seconds before "the map has started".

So where `LIVE_MESSAGE` is on, E5 is not queued at all and the live message's
heading says what E5 would have said. A subscriber who muted E5 gets the plain
score form instead — muting asked for exactly that. And if the message cannot
be created for someone (Telegram refused), E5 goes to that one chat through the
queue after all: a milestone must not be lost on a best-effort path.

## When a map starts

Not when the feed first names the new map — that happens in the warmup, which
can run for twenty minutes with the score sitting at 0:0.

The feed reports the warmup explicitly through `currentRoundState`, and that is
the only reliable signal. The `live` flag is **not** one. Measured on a recorded
map boundary:

```
warmup   live=False  round=1  0:0     177 frames
started  live=False  round=1  0:0      64 frames   <- the map really starts here
ended    live=True   round=1  0:1                  <- only now is live true
```

`live` turns true only once the first round has been PLAYED. Gating on it would
announce the map after its first round was already decided.

The live message obeys the same rule, because it IS the announcement: the card
is not opened during the warmup. Only creation is held back — a warmup in the
middle of a map (a server restart, a technical pause) finds the card already
there and keeps updating it.

The private memo `live_map_name` is not advanced during the warmup either —
otherwise the comparison would find nothing left to notice by the time the map
really starts.

## The end of the match: the same division of labour

The feed only ever knows the current map, so on its own it cannot tell that the
match is over. Give it the series format and it can: BO3 ends when somebody
takes 2 maps, BO1 at 1, BO5 at 3, BO7 at 4; BO2 has no majority to take, so it
ends when both maps are played and may legitimately end level. `best_of` comes
from the match page and is stored in `match_state`.

So "the match finished" now goes out at the same moment as the last map,
instead of the four minutes later it took the page to notice its own status
flip (measured on a real match: 12:09:58 and 12:14:22).

The page still has the last word, and it needs no new machinery to keep it. The
live feed emits E7 with **the same idempotency key the page machine would
produce**. If the two agree, the unique index swallows the page's copy in
silence. If they disagree — a forfeit, a technical decision, a map replayed —
the key differs, the page's message goes out, and it says it is a correction so
it does not read as a duplicate.

With an unknown format nothing is guessed: the page reports the end of the
match as it always did.

## Being careful with the source

The single HTTP layer (`http.py`) is the only point of egress to the network.
Ordinary `requests`/`httpx` get a **403** where a browser gets data: the
filtering is by TLS fingerprint, not by headers. Permuting them is useless,
hence `curl_cffi` with an impersonation profile from the very beginning.

The ceiling of **1 request every 30 seconds** is hardcoded and cannot be raised
by config. Requests are strictly sequential, with ±20% jitter. The wait is a
loop rather than a single `sleep`: the timer returns control a few milliseconds
early and the ceiling would systematically fall short.

**The live feed's long poll does not fall under the ceiling** — it is a held
connection, one per match, not frequent polling.

`403` is handled separately from network failures: it is not an outage but a
"back off". The pause is measured in minutes while page polling keeps working.

## Who a notification goes to

The recipients are computed by `notify/audience.py` — and that is the ONLY
place the pause is checked. There used to be two: the event queue and the live
score message, and they drifted apart. The queue knew about the pause, the live
message did not, so someone who pressed "Quiet" kept receiving the score as the
map went on. The rule "check it in two places" is unenforceable; the right
conclusion is to have one place.

The other half of the same defect lives there too: the "this match has no team
links" branch used to hand back the chat from the config directly, bypassing
both the subscriber list and the pause. That is what the database looks like
right after an upgrade, until the team page is polled again — and at that
moment the person who had paused received notifications while the person who
had not received nothing. Now such a match is shown to everyone who is
listening.

The queue adds what only it knows: targeted events (a reminder goes to one
chat — the intervals differ per person) and muting by type.

## Where the service is allowed to go

The host list is closed and lives in code (`config.ALLOWED_HOSTS`), and the
check sits at the network egress itself, in `http.py` and in the feed client.

This is not belt-and-braces but a closed hole. The match address used to be
assembled by concatenating `HLTV_BASE + href`, where `href` came off the HLTV
page. `HLTV_BASE` does not end in a slash, so an `href` like
`@10.0.0.1:8080/matches/1/x` produced
`https://www.hltv.org@10.0.0.1:8080/matches/1/x`: `www.hltv.org` is userinfo
there and the request goes to `10.0.0.1`. Verified against a live libcurl — it
goes exactly there. The variant `.evil.example/matches/1/x` did not even need
the at-sign. The address was saved to the database and then requested every
minute, meaning foreign markup could make the service hammer the local network
from the owner's home IP.

The fix has two layers:

1. the match address is assembled from a **validated number**, not from a
   string: `f"{HLTV_BASE}/matches/{match_id}/{slug}"`, where `slug` is only
   `[A-Za-z0-9_-]`, so there is no getting out of one path segment;
2. the host is checked once more right before the request — so it also fires on
   a record written to the database before the fix.

What is compared is the `hostname` from the parsed URL, not the start of the
string: `startswith` is useless here, and the attack is built on exactly that.

## Proxy

There are three ways out to the network — HLTV pages, the live feed, the
Telegram Bot API — each with its own `curl_cffi` session. The proxy is chosen
per request address (not per session) from the standard
`HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`/`NO_PROXY` (`proxy.py`). No custom
variables were introduced, deliberately: this is the case where the common name
beats a private one. Per-address selection is what lets a `NO_PROXY` exception
apply precisely — the feed client, for one, talks to two hosts: the feed itself
and the match page it warms up on.

The parsing is written by hand even though libcurl can read the environment
itself. The reasons are concrete, and all three are observations rather than
precautions:

* `curl_cffi` does not read it at all. The session has a `trust_env` field, but
  it does not affect proxy selection — there is simply no code under it;
* libcurl does read it, but deliberately ignores `HTTP_PROXY` in UPPERCASE (a
  CGI legacy, where the variable came from the client). And uppercase is
  exactly how it is written in `compose.yaml` — the setting would have silently
  done nothing;
* CIDR support in `NO_PROXY` depends on the libcurl version.

Hence a non-obvious detail: when an address matches `NO_PROXY` the service sets
the proxy to an **empty string** rather than "does not set it". An empty string
means "no proxy" to libcurl and overrides the environment variable; the absence
of a setting would send it back to reading `ALL_PROXY` and the bypass would not
work. This is verified live: with a dead `ALL_PROXY` and `NO_PROXY=hltv.org`
the team page downloads.

## Shutdown

On SIGTERM the pollers, the watchdog, the reminders and the live feed are
stopped at once: they only produce new work, and new work is no longer wanted.
The bot meanwhile hangs in `getUpdates` for up to twenty-five seconds — we
cannot wait for it, Docker has its own timer.

The queue is the exception and is **not cancelled**. A cancel in the middle of
`send_message` would leave the message sent to Telegram but not marked in the
database, and on the next start it would go to the person a second time.
Instead it exits on its own (the stop flag is already set) and makes a final
pass: an event may have been born a second ago — the end of a map in a match
that finished right during the restart, say — and there is no reason for it to
sit until the next start.

The pass is time-bounded, and the whole wait is eight seconds, less than
`stop_grace_period` in compose. Whatever did not make it goes nowhere: the
queue rows are in the database and go out on the next start.

## The data model

| Table | What for |
|---|---|
| `matches` | known matches, the snapshot and its hash |
| `match_state` | state, current map, score, the progress fingerprint |
| ↳ `current_map_name` | **display only**: written by both machines |
| ↳ `live_map_name` | the live machine's private memo (E5 is decided from it) |
| ↳ `page_seen_utc` | the page machine's private marker (first observation) |
| `map_results` | the results of played maps |
| `sent_events` | **the journal of what was sent, unique index on the key** |
| `outbox` | the outgoing queue with retries |
| `live_messages` | the id of the live message per map |
| `raw_log` | raw responses for debugging, pruned by age |
| `meta` | the first-run flag, a match's map lineup, the last poll time |

All times are **in UTC**. Conversion happens only at render time, through
`zoneinfo`. The display zone is `TZ_DISPLAY`, `Europe/Moscow` by default.

The database is in autocommit with `synchronous=NORMAL`: with `FULL` the
initial fill took 21 seconds in a container instead of 0.2 — an fsync per
INSERT.

## Private state versus shared state

The page machine and the live machine write into one table, and that is fine
right up until they start drawing conclusions **about their own history** from
shared fields. The project has been burned by this twice:

* the live machine asked `current_map_name` "has the map changed", but that
  field is written by the page too — and with the name of the UPCOMING map. The
  answer was always "it has not", and E5 was never born;
* the page machine derived "I have not seen this match yet" from `last_source`,
  which the live feed rewrites several times a second. The answer was always "I
  have not", and page-side E6 stayed silent the whole time the feed ran — that
  is, the page stopped backing anything up exactly when the feed missed
  something.

The rule: **a conclusion about your own history is drawn only from your own
field.** Shared fields are fine for display and for data, but not for
decisions.

## Several tracked teams

The team list lives in the `teams` table and is edited through the bot. The
schedule is polled per enabled team separately; a failure on one does not get
in the way of the others, and only a failure across all of them counts as a
source failure.

The first-run flag is **per team** (`bootstrapped:<id>`). Otherwise adding a
team mid-run would be noisy: it immediately has a dozen and a half played
matches, and every one of them would produce an E1.

### A match between two tracked teams

There is one match, so notifications about it must arrive once each. But each
team sees it from its own side, and naively the score is oriented on "our"
team. The key `E6:<match>:map:2:result:13-10` would become `...:result:10-13`
for the second team — a different key, and therefore **a second notification
about the same thing**.

That is why a match has a **canonical perspective** (`matches.team_id`): the
score and the team name in the message are taken from that one team alone. It
is the team that saw the match first — the choice is arbitrary but must be
deterministic.

More importantly: the perspective **does not change** once chosen
(`COALESCE(matches.team_id, excluded.team_id)`). If a new one overwrote the
old, adding a second team in the middle of a running match would flip the
score, the keys would become mirrored and everything already sent would go out
again.

**Multikills are the exception, deliberately.** Their key contains the
`steam_id`, so a 4k by a player of either team arrives on its own: those are
different highlights and there is no sense muting one for the other's sake.

## Several subscribers

A notification is addressed. For every event the notifier works out the
recipients and puts ITS OWN row in the queue for each: `outbox.chat_id`, with
the journal key extended by the recipient (`<chat>|<key>`). Otherwise an event
about a shared match would reach only one of them — the unique index would cut
the rest off.

Who gets what:

| Event | Recipients |
|---|---|
| about a match (E1-E7) | subscribers following any participant |
| a multikill (E9) | those following **that player's** team |
| service (E8, E8R) | all enabled subscribers |

**Turning the score around.** The event is oriented on the match's canonical
team. Someone following its opponent is shown the mirrored score — otherwise
they read "13:10" where for them it is "10:13". `format.orient` does the
turning at render time, so different subscribers have different texts of the
same event sitting in the queue.

**The match perspective is remembered once** — in `matches.team_id`, by the
team that saw the match first; `canonical_team()` reads exactly that. While it
returned simply the lower id among the participants, the perspective flipped out
of nowhere: add a team with a lower id through the bot mid-match, and the scores
of already played maps swapped over while the next messages about the same match
contradicted the previous ones. The `COALESCE` guard in `upsert_match` was
standing there faithfully all along — but on a column nobody read.

**Muting** is on the pair "subscriber + team" (`teams.muted_events`). The rule
for a match between two tracked teams: the event goes out if **at least one** of
that subscriber's teams in the match wants it. Otherwise one team would silently
mute notifications about the other.

**The whitelist.** The bot has a public address, and without a restriction
anyone who finds it could command it. By default we answer only those listed in
`TELEGRAM_CHAT_ID` (comma separated); everyone else gets silence, so as not to
confirm the bot exists. The exception is `/whoami`: a person has to be able to
learn their own id, otherwise there is nobody to add them to the list.

## The watchdog: "I have gone blind"

A separate component (`watchdog.py`), because the meaning of the event is not
"the source returned an error" but "notifications have stopped working, go and
look by hand". It watches four subsystems: the schedule, the match page, the
live feed and the sending queue.

The alarm is not raised immediately — a short failure fixes itself through
retries. But neither is it raised "whenever": the threshold depends on what is
at stake **right now**.

| Situation | Threshold |
|---|---|
| less than a minute to the match start | 60 s |
| the match should have started and we cannot see it | 60 s |
| someone is ≤3 rounds from winning the map | 60 s |
| an overtime is being played | 60 s |
| everything else | `DEGRADED_ALERT_SECONDS`, 300 by default, 600 max |

The "N rounds left" thresholds are computed by the same `scoring.py` from the
format the source reports, so MR15 and non-standard overtimes do not break the
judgement.

**One alarm per failure:** the key contains the moment the failure started, so
repeated checks of the same failure send nothing while a new failure is reported
afresh. Recovery arrives as a separate message — otherwise it is unclear whether
it has passed.

**A "Recovered" only follows an alarm that was actually sent.** Not one that was
merely counted down: those are different moments. The countdown starts on the
first failed attempt, while the alarm goes out only once the failure has held
past the threshold — and it may never go out, because the next attempt happens
on the poller's own cycle. Both halves of that were seen in production: the live
feed connected 0.7 s after its countdown began, and the schedule recovered on
its next attempt 35 minutes later. Both produced a "Recovered" for an outage
nobody had ever been told about. Elapsed time is not the test; whether an alarm
was sent is.

There is a paradox about the sending queue: if Telegram is not accepting, the
alarm about Telegram goes into that same stuck queue. That is deliberate — it
will get through when the connection returns, and until then it is visible in
`/status` and in the logs. Staying quiet is worse: mute delivery looks exactly
like an absence of events.

## Match states

```
SCHEDULED ──▶ LIVE ──▶ FINISHED
    │           │
    │           └──▶ (E8 "stalled", if there is no feed and nothing changes)
    └──▶ CANCELLED / UNKNOWN
```

`UNKNOWN` means the match vanished from the team page after its scheduled
start: it may equally have begun or been cancelled, and guessing here does
harm.

The "match has stalled" event is not sent **while the live feed is connected**:
the meaning of the event is "I have gone blind", and if the feed answers, we do
see the match. Between maps the threshold is stretched threefold — at a LAN a
twenty-minute break is normal (a false alarm on this has already happened).

## Extension points

### HLTV's mobile JSON endpoint

A fallback schedule source, resistant to a redesign. It requires proxying the
app's traffic through mitmproxy; the recon is deferred — see
[recon/R3-schedule-source.md](recon/R3-schedule-source.md).
