# Architecture

This document answers "why this way", not just "how". Almost every decision
here came out of watching the live HLTV, and without that context parts of the
code look over-engineered.

It is written for someone about to read or change the code. To just run the
service, [../README.md](../README.md) and [operations.md](operations.md) are
enough and this document is not needed.

> Notification types are referred to by code throughout — `E6` is the end of a
> map, `E5` the start of one. The full list is in
> [../README.md#event-codes](../README.md#event-codes).

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
E9:<match>:map:<n>:round:<r>:<steam_id>
E10:<match>:<start_utc>:remind:<minutes>
E11:<match>:map:<n>:point:<us|them>:<target_score>
E12:<match>:map:<n>:half | E13:<match>:map:<n>:overtime:<k>
E14:<local_date>:<minute_of_day>
E15:<match>:map:<n>:round:<r>:<steam_id>
```

A key must depend **only on the content** — and on all of it. Let the time the
response arrived into it and deduplication stops working; leave out something
the message asserts and it stops working the other way round. E10 carries the
start for that reason: without it a reminder that had fired once could never
fire again, so a match moved after its reminder went out got none for the time
it actually started, while E2 — which has the new time in its key — was
delivered correctly all along.

Changing the shape of a key is therefore a migration, not an edit: the journal
is keyed by the old form and would match nothing, so the first run after the
upgrade would send again everything it had already sent. There are two such
migrations, both one-off under a flag in `meta` — `adopt_legacy_event_keys`
for the recipient prefix and `_migrate_reminder_keys` for the start in E10.
The second one rebuilds the start the way the reminder itself takes it (the
pending time during a debounce, the confirmed one otherwise), so a match that
has since been moved deliberately fails to match and gets its new reminder. The hour in the E8 key is a
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

**And the schedule keeps being polled while a match is overdue.** The slot
arrives, nothing starts, and that is exactly when HLTV moves the match — by
five minutes, then ten. Judged only by "is the start still ahead", such a match
is nobody's business: it is not upcoming, the mode falls to idle and the next
look at the page comes half an hour later. So a match past its start and not
yet running keeps the frequent cadence for `LATE_START_GRACE_MINUTES`, and
drops out the moment the page says LIVE. The window is bounded because a match
may never happen at all.

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

**A frame whose score the round cannot hold is discarded whole.** After N
rounds at most N are decided, so `ctTeamScore + tTeamScore <= currentRound`
is a physical invariant — measured on every frame of both recordings, 4005 of
them, with no exception. It is `<=` and not equality: a `freezePeriod` frame
keeps the number of the round that just ended while carrying its score. Seen
live once, on a fresh map's first non-warmup frame: round 1, `ended`, 9:4.
Where HLTV got that score is unknowable from here; what matters is that the
frame was evidence of nothing, and that the card was only the visible thing
built on it. `apply()` would also have fed it to the comeback trajectory,
taken the highlight baselines from it, tested it for a map point and a half,
and advanced the map memo so the real first round no longer looked like the
start. So `LiveFrame.coherent` is checked before anything reads the frame,
and an incoherent one is logged at WARNING once per map and dropped. The
known cost is written down in the limitations: a `currentRound` that HLTV
reset inside an overtime would blind the feed for the rest of that map, and
no recording has an overtime to say whether it does.

Details and raw measurements: [recon/R4-scorebot.md](recon/R4-scorebot.md).

## When a map counts as finished: two sources, different roles

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

## Round highlights: multikills (E9) and clutches (E15)

Both are computed from **increments in scoreboard frames**, not from `Kill`
events. The reason is the same as everywhere: the log is replayed on connect.
Every player in a frame carries the kills *and* the clutches accumulated over
the map, so both are read the same way — remember them when the round begins,
watch the increment.

Kills are accumulated **over the map**, so the baseline is reset on every
round; without that every subsequent round would look like a multikill. The
warmup is ignored — that is deathmatch. `advancedStats.oneOnXWins` behaves
identically and gets the same treatment.

### One round, one message

A clutch is only a clutch once the round is **won**, so it cannot be known
before the round is over. A multikill used to be reported the instant the Nth
kill landed. Those two cannot both hold while a round that produced both is to
be described in one message — and describing it in two is noise about one
moment.

So a round is resolved **once**, when it is decided for that player:

* **he dies** — his kills are final and there is nothing left to wait for. This
  is the ordinary case and it costs no delay at all;
* **the round ends** — for whoever was still alive, and for anyone who was at
  some point the last of his team alive. That exception matters: a clutch can
  be credited *after* the clutcher dies, when the bomb he planted goes off;
* **the round is left behind** without either — a missed `ended`, a reconnect
  across the boundary. The next round arriving is the last moment it can be
  told, and the report then carries **its own** round and score rather than the
  frame's: stamping it with the frame that triggered the flush named the next
  round in the message and in the key alike.

The tracker is per team **and per map**, like the comeback tracker. That is
what re-reads the bars when a map starts — keyed by team alone it kept the
bars it was built with for the whole match, so a threshold changed during a
match never took effect. It also means a round left unreported on a finished
map is dropped rather than dragged onto the next one, which is the right way
round: missed, never invented.

Measured across both recordings, waiting for the round to end costs a median of
**0 seconds** and at worst **32** — the last kill of a multikill is usually the
kill that ends the round. It also removed a real annoyance: with a bar of four,
an ace used to arrive as two messages ("4k round", then "ACE"), and now arrives
as one that says ACE.

### A round is credited only with what was seen during it

Not with the difference between its baseline and whatever frame arrives next.
**The feed skips rounds** — the forze recording jumps straight from round 2 to
round 8 — and a naive difference across that gap reported a ten-kill round for
four players at once. The peak is therefore tracked frame by frame while the
round is the current one, and a frame belonging to another round cannot
contribute to it.

### Sizing a clutch

Whether one was **won** is HLTV's verdict, taken from `oneOnXWins`: it also
covers the round taken on the bomb or the clock, which no reading of the alive
counts can distinguish from a round that simply ran out. How many it was
**against** is ours, because the feed never says — we count the live opponents
while a player is his team's only survivor, and keep the largest.

Two things that both produce silent nonsense if got wrong:

* the count is taken **only while `currentRoundState == "started"`**. At half
  time the `CT` and `TERRORIST` arrays swap under us during `ended` and the
  alive counts pass through `(1, 1)` — a forged 1v1;
* if HLTV credits a clutch and we never saw the standoff, it is **dropped**,
  not guessed at. The bar is expressed entirely in N, so an invented "1v1"
  would understate a 1v4 and be read as a fact.

### Which type, and why not one

A clutched round is **E15** and carries its kill count inside; a round with
kills alone stays **E9**. One message, one type — which is what keeps E9's mute
entry and threshold meaning exactly what they meant before, and gives the
clutch its own. Muting one must not silence the other.

The two bars are independent by design. They measure different things — kills
against opponents — and a 1v3 can be won with a single kill, so tying them
together would only produce refusals. The service stops watching rounds when
**both** are off for everybody, never when one is.

Errors lean the safe way: after a reconnect mid-round the baselines are taken
afresh, so a highlight can be **missed but never invented**.

## When a match starts (E4)

Not when the match page says LIVE. HLTV raises that flag when the teams connect
to the server, and the warmup before the first map can run twenty minutes with
the score at 0:0 — "the match has started" during it is not true, and it is
the first thing anyone watching notices.

The page cannot tell a warmup from a game; the feed can. So the same division
of labour as everywhere else, only the other way round: **the page writes the
message, the feed decides the moment.** The page machine builds E4 as it always
did — the picks and the opponent's real name are page data — and, while the
feed is reporting a warmup this very second, puts it aside instead of sending
it (`start_event:<match>`). The live machine sends that payload on the first
non-warmup frame, under the key the page would have used, so whoever gets there
first wins and the other copy is swallowed by the unique index.

The state still flips to LIVE at the page's word: the feed has to be brought
up, and the schedule has to stop treating the match as one that has not begun.
Only the message waits.

**The fallbacks matter more than the gate.** With no feed at all — a 403
cooldown, a feed that never comes up, a phase nobody has refreshed for two
minutes — the page decides on its own exactly as before. Losing E4 entirely
would be far worse than sending it during a warmup.

The card that follows must not overtake it. E4 goes through the queue while the
card goes straight to Telegram, so the worker holds the card back until the
start message has left the queue (30 seconds at the outside — a stuck queue
must not cost the live score). The queue now wakes on a new message instead of
sleeping out its five seconds, which is what makes that wait about a second.

## The half and the overtimes (E12)

Off by default, `PHASE_ALERTS`. Two moments where the map turns over: the sides
swap once `regulation` rounds have been played (7-5, 6-6, 12-0 — the split does
not matter), and a new overtime begins whenever the score is level at the end
of the previous one (12-12, 15-15, 18-18). The side swap INSIDE an overtime is
deliberately not reported: under MR3 that would be a message every three
rounds.

Both numbers come from the frame's own `regulationHalfLength` and
`overtimeHalfLength`, never from a hardcoded 12 — the same rule as everywhere
in `hltv_notify.scoring`.

## Comebacks

One more line on the map's result, not a message of its own — it belongs where
the score it talks about already is.

**The measure is the swing in the score difference, not a streak.** Two shapes
of the same story:

```
down  3:11, won 13:11    ten taken without reply
down  1:7,  won 13:9     twelve taken, two given away
```

The first is a streak, the second is not, and both are the same thing: −8 to
+2 is a swing of ten, −6 to +4 is a swing of ten. Counting rounds in a row
would have found only one of them. So the map's difference is followed frame by
frame and the biggest rise and the biggest fall are kept — a rise is our
comeback, a fall is theirs. Both are reported: a comeback made, a comeback
given away and a comeback denied are the same fact told from different sides,
and one verdict serves every recipient.

**There is a floor under the deficit as well as under the swing.** Without it a
13:1 win reads as "a comeback from 0:1" — the swing is twelve and there was
never a hole to climb out of. Half the swing is the smallest hole worth the
word, so it is derived from `COMEBACK_ROUNDS` rather than being a second
setting.

A run that only reached overtime, or was stopped short, still gets its line:
the map was lost but the run happened. The verdict is `won` when the team that
made it took the map and `stopped` when it did not.

Whose run it was is **derived from the score at render time, never stored** —
the E11 lesson: the score turns around for a subscriber following the opponent
and a stored "whose" would not turn with it. Every score in the line is written
from the comeback team's own side, so "Color came back from 1:10" reads the way
a person would say it whichever team the reader follows.

The tracker lives in the machine's memory, like the multikill trackers. It
survives feed reconnects, which are frequent; a restart in the middle of a map
loses the rounds before it, and then a comeback comes out understated or
missing — but never invented.

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

The warning carries the broadcasts, the same way a highlight does and for the
same reason: the point is to be watching when the round is played, so the
thing to tap is in the message. The whole list goes into the payload, and the
reader's own languages and count pick from it at render time. A new overtime
(E13) carries them too; the half (E12) does not — it is routine, and nobody
needs a link to watch a break. Whether a type carries streams is decided in
the machine alone; the renderer and the queue draw what the payload has.

And when the reader has the live card up, the warning does not arrive as a
message of its own at all — it goes into the card. See "The card absorbs the
map's milestones".

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
event is only *queued*, and at the time the worker slept out five seconds
between passes and spaced every send 1.2 s from every other. Measured on a real
match: E5 queued at 09:13:24.905,
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

## One door out to Telegram

Four things write to Telegram: the event queue, the live score card, command
replies and button acknowledgements. They share ONE budget of roughly thirty
calls a second, so the limit lives in the client itself (`telegram.py`,
`CALLS_PER_SECOND`) rather than in each of them. While every writer held its
own limiter, each was within its own rules and together they could still go
over — and a 429 does not arrive at the writer that caused it.

The lock covers only the bookkeeping, never the request: `getUpdates` hangs for
twenty-five seconds and holding the gate across it would stop everything.

## The queue's two rates

Telegram's limits are of two kinds and the queue answers each with its own
mechanism, because conflating them cost real delivery time.

* **Within one chat** — `SEND_INTERVAL_SECONDS`, 1.2 s between messages. This is
  also where the ordering guarantee lives: a chat's messages are sent one after
  another in queue order, so the live card cannot overtake the "match started"
  it continues.
* **Across different chats** — nothing of the queue's own. That is the shared
  budget above, held at the door, and it is the only thing two recipients
  share.

Chats are therefore drained in parallel (`MAX_CONCURRENT_CHATS` at a time, a
bound on tasks rather than a rate) while each chat stays strictly sequential.

The pause used to be applied between every two messages whoever they were for.
For one subscriber — the case the service was written for — the two limits are
the same thing, so nothing looked wrong; on a fan-out to twenty people one map
result took twenty-four seconds to finish delivering, for a score that is only
interesting while the match is running. The same batch now takes about a
second. What cannot be got around is Telegram's own ceiling: the queue and the
live card's edits draw on the same thirty a second.

Neither rate applies in `DRY_RUN`: nothing leaves for Telegram, and pacing the
log helps nobody.

## The card must not hold up the feed

The live card is one message per subscriber, and a round of edits used to be
`await`ed inside the frame loop: one Telegram call per person, in sequence.
With a hundred subscribers that is some ten seconds in which no frame is read
at all — so the score being drawn is already stale by the time it is drawn, and
the multikill counter, which reads the same frames, goes blind alongside.

The ordinary redraw is therefore handed over and not waited for
(`LiveMessenger.submit`), and **only the newest snapshot per match is kept**. A
frame overtaken while the previous round was in flight is a score nobody will
ever need again, so it is dropped rather than queued — the same reasoning that
keeps the card out of the outbox in the first place.

Two moments stay awaited, because their result is needed: creating the card,
which carries the map start and must report for whom it failed, and the final
edit. Both also **wait for a background redraw of that match to finish first**
(`_settle`). Without that the final edit races the redraw it overtook: the draw
read the row before `finalized` was written, and `save_live_message` writes
`finalized = excluded.finalized`, so the freeze was cleared and the stale score
became the card's last text — after which the finished map went on being
redrawn. The wait is a wait and not a cancel, for the same reason the queue is
never cancelled: a cancel inside `send_message` leaves the card posted with its
id unsaved, and the next start opens a second one for the same map. Shutdown
gives a draw in flight `CLOSE_GRACE_SECONDS` for exactly that reason.

**And the interval stretches with the audience.** The card's total cost is
`recipients / interval` while Telegram's budget is fixed, so holding the
per-person interval constant means the total climbs until it hits the ceiling
— and past the ceiling cards do not slow down, they start failing. What is held
fixed is the total instead (`LIVE_EDIT_BUDGET`, ten a second): a hundred people
still get the configured ten seconds, three hundred get thirty. A card that
updates more slowly is honest; one stuck on a five-minute-old score is not.

**A frame the throttle skips is drawn at the trailing edge, not dropped.** The
feed can fall silent right after the frame that mattered — half time, a
pause, the last round before a break — and a skipped frame with no successor
left the card showing the previous round for as long as the silence lasted.
So the throttle records how long it asked the skipped frame to wait, and
`_draw` sleeps that out and draws it, unless a newer frame arrives first (it
simply takes the slot) or `_settle` says stop — the sleep is an event wait,
so the final edit and shutdown never sit through it.

## Thresholds that differ per person

`MULTIKILL_THRESHOLD` and `COMEBACK_ROUNDS` are matters of taste — a four-kill
round is a highlight to one person and noise to another — so they are per
subscriber, edited with `/settings`. That collides head-on with the rule an
event is born ONCE (see "an event is born on a TRANSITION"): a single machine
cannot emit an E9 that is a 3k for one reader and nothing at all for another.

The way through is to split the decision in two, and the split is not
arbitrary — it follows where the recipient is known:

| Stage | Knows | Decides |
|---|---|---|
| the machine | nothing about recipients | whether to MEASURE at all, at the lowest bar in use |
| the queue (`outbox._wants`) | the recipient | whether this particular event reaches them |
| the renderer (`format.comeback_line`) | the recipient and the text | whether a LINE inside a message is printed |

**Why the lowest bar.** `Storage.threshold_in_use` returns the smallest
non-zero value among subscribers. Built too eagerly, an event can still be
withheld from whoever wanted more; built too conservatively, it does not exist
and cannot be given to whoever wanted less. Zero means "off" and is skipped
rather than being the minimum — one person switching multikills off must not
switch the tracker off for everybody. With no subscribers at all, single-user
mode, the config is the answer.

**Why the comeback goes to the renderer instead.** It is not an event; it is a
line inside E6, and E6 goes out whatever happens. The message is already being
composed for one reader — `format.orient` turns the score around there — so the
bar is applied where the sentence is written. The deficit floor
(`max(2, threshold // 2)`, see `state/comeback.py`) has to be recomputed with
the reader's bar too: it is derived from the bar rather than being a second
setting, and leaving it at the machine's value would tell somebody who asked
for 12 about a 13:1 win "from 0:1".

**The environment is a default, not an override.** A row in
`subscriber_settings` exists only once somebody changes something, so raising a
value in `.env` still reaches everyone who left it alone. This is the same
shape as `REMINDERS` and `/remind`, and it has one non-obvious requirement: the
consumer must not check the config first. `LiveMessenger.update` used to start
with `if not self.config.live_message: return` — with that line, `LIVE_MESSAGE=false`
would make `/settings card on` impossible to satisfy. The check belongs in
`_recipients`, per person, and nowhere else.

**What it costs.** A threshold changed mid-map takes effect on the next one:
the tracker is constructed when the map starts. And one person asking for 3k
makes the service track 3k rounds for everybody — that is arithmetic on frames
already being read, not messages, and nobody else receives them.

## Broadcast links under a multikill

A 4k is worth clipping, and the clip has to be made while the moment is still
on the stream — so E9 carries the broadcasts rather than making the reader go
looking through the match page.

**The source is free.** Streams are on the match page, which is already fetched
every 60-300 s while a match runs, so `match_page.parse` returns them alongside
the maps and the score. Fetching that page when E9 fires was considered and
rejected: the 1-request-per-30-seconds ceiling is process-wide and behind a
single lock, so an out-of-turn read would hold the message up to 30 s and steal
the slot from schedule polling — making the one thing that must be fast an
order of magnitude slower, several times a map. Staleness is bounded by the
poll cadence instead, and what ages is the ORDER, not the set of casters.

`Storage.set_match_streams` rewrites the list on every observation rather than
writing it once: a caster on a hundred viewers at the start of a match can be
behind three others on a thousand an hour later. It ignores an EMPTY parse
though — a page served mid-edit must not cost every link, and there is no way
back before the next multikill.

**Only Twitch and Kick.** HLTV also lists YouTube, which has no clip button; a
link there is a dead end dressed up as a choice. Two lists guard this: what
HLTV calls the provider, and what host the link may actually reach. The second
is the one that matters — the href comes off a web page, and `HLTV_BASE + href`
was a real SSRF in this project's history.

**The choosing is the same three-stage split as the thresholds.** One multikill
reaches many readers with different languages and different appetites, so the
event carries the WHOLE list and `streams.pick` runs in the renderer, where the
reader is known. Two rules, and they are not the same rule:

* a language outside the reader's list appears only when the match has none at
  all in it. One English cast beats five Portuguese ones even though the list
  comes out short — an unfollowable link is not a fallback;
* from three links up, the last slot goes to a *second* of the reader's
  languages when the top is all one and another is casting further down. Below
  three there is no quota: with two slots it costs more than it gives.

**Flags are countries, languages are not.** HLTV marks a broadcast with a flag,
so English arrives under `GB`, `US`, `WORLD` and every anglophone country.
`STREAM_LANGUAGE_ALIASES` folds them; an unlisted flag is its own language, so
only exceptions need writing. Measured on fixture 2397091: without `AU` in that
table the block drops a cast on 155 viewers in favour of one on 8.

## The daily digest (E14)

A reminder answers "this match starts soon". The digest answers a different
question — "is the day worth keeping free" — and it is asked at times the
person picks, in their own zone.

**The times are wall-clock, so they are stored as minutes from LOCAL midnight**
(`digest_times.minute_of_day`) and resolved against `subscribers.timezone` on
every tick. Storing the UTC equivalent would be simpler and wrong twice a year:
nine in the morning has to stay nine in the morning across a daylight-saving
jump.

**The window is a rolling 24 hours from the moment it fires**, not the rest of
the calendar day. At nine, a match at seven tomorrow is 22 hours away and worth
knowing about; one at eleven tonight is not more urgent for sharing a date with
today.

**Nothing on means nothing sent.** This is the rule the feature stands on: a
digest that arrives every morning to say "no matches" is one people mute, and a
muted digest is worth nothing on the morning something is on.

**A match being played is left out.** It is the one thing the owner cannot have
missed: it was announced when it started, and its live card is at the bottom of
the chat being edited round by round. `/live` is the command for that question.

`Storage.matches_within` and not `upcoming_matches` all the same, for a smaller
reason that is easy to miss: the latter judges by the time alone, so a match
already cancelled but still dated tomorrow is "upcoming". A digest listing it is
worse than one that is a few hours short, so the state filter is part of the
query.

**The event is targeted at one chat** (`only_chat`, the same mechanism as a
reminder). Two people with different times, different zones and different teams
share nothing, so there is nothing to gain from building it once — and because
the reader is known while it is built, the line can be turned to face their
team there rather than at render time.

**The key is the local date and the slot**, nothing about the matches. The
message asserts "this is what the next 24 hours hold as of 09:00", and that
assertion does not stop being true when a match is added an hour later: a
second digest for the same slot would be a duplicate, not an update. The date
is the subscriber's own, which is why the chat prefix `record_event` adds
matters here.

A missed slot is caught up for an hour and then abandoned. A restart must not
cost the morning digest; a container that was down all day must not deliver it
at bedtime.

## The card absorbs the map's milestones

The card is the message a person watches during a map, and it is a fixed
message in a chat that keeps moving: a map point or a half-time message would
push it out of view and the reader has to scroll back for the score. So those
milestones — E11, E12, E13, `fmt.CARD_EVENTS` — do not arrive as messages of
their own when the reader has a card. The card is deleted and sent again with
the milestone's **banner** on top and the score as of that moment underneath:
one message where there used to be two, and it is the last thing in the chat.
Every later redraw keeps the banner (it is stored on the card's row); a later
milestone replaces it.

This is the second time a message has been folded into the card, and for the
same reason E5 was: the card goes straight to Telegram while events wait in
the queue, so anything that must sit beside the score is better off inside it.

**The body comes from memory, never from the stored text.** The card used to
be moved below the milestone by re-sending `last_text` from the database, and
that produced the defect that motivated the rewrite: "Map point — 8:12" with
the card right under it saying 8:11, one round behind. Not a race — the frame
that produced the milestone had been submitted for a redraw, and the edit
throttle dropped that redraw exactly as designed. The stored text is therefore
stale by construction at the one moment it is wanted. `LiveMessenger._latest`
keeps the newest snapshot per match, written by `submit` (throttled or not)
and by `update`, and the rebuild renders from it. Half time, when the feed
falls silent, is no problem: the newest frame is the half-time frame.

**The hand-over is the queue's.** `Notifier._deliver` asks the card first
(`absorb`) and sends the plain body only when the card answers "not this way".
That keeps the per-chat ordering where it always was, and marks the milestone
sent with the card's new message id — the journal is unchanged. Rendered
twice at enqueue, banner and plain body, because which form goes out is only
known at delivery: rows queued before the columns existed carry NULL and go
plain, which is also the safe direction.

**The banner faces the same side as the card.** The queue shows an event from
the first of the reader's teams that has not muted it; the card is always
drawn from the reader's FIRST team. For a chat following both teams of one
match, with the map point muted for the first, those differ — and the banner
would read "map point for us, 12:6" over a body saying "6:12". So for
`CARD_EVENTS` the queue orients on the card's team regardless of the mute
(`Notifier._recipients`); the second team only decides whether the event
reaches the chat at all.

**When the card says "not this way".** The milestone then arrives as its own
message and the card stays where it is, edited as before:

* nothing is really sent (DRY_RUN, no Telegram);
* the chat gets no card — `/settings card off`, the pause — answered by
  `_recipients`, the one place that knows (rebuilding a card means SENDING
  one, so it answers to the same rules as every other delivery);
* there is no snapshot of that map in memory: a restart between the milestone
  and the feed's first frame, or a milestone delivered after its map ended.
  Falling back to the stored text here would bring the stale-score defect back
  through a side door;
* the card is finalized — the map is over and its final score stays put;
* Telegram refuses to delete the old card (too old, the bot lost the right).

**One lock per card, around the whole write.** The queue rebuilds a card from
its own task, and between its delete and its send the row carries no message
id at all. A redraw reading the row in that window would conclude there is no
card, send its own, and the map would end with two — the rebuilt one plus an
orphan nobody edits again. So `_update_one` takes `_move_lock` before it
decides which message the card IS and holds it through the send or edit and
the write; the row is re-read inside, `finalized` included, and the render
happens inside too, because the banner is part of the row and the queue is
what changes it. `absorb` waits for a redraw in flight (`_settle`) before
taking the lock; that is safe because it runs in the queue's task, never
inside `_draw`.

A delete that succeeds is written to the database (`forget_live_message_id`)
before the send is attempted, because `save_live_message` COALESCEs the
message id — without that, a failed send would leave the row pointing at a
message that no longer exists and every later redraw would edit a ghost. When
that send fails the milestone simply goes plain and lands above the card the
next redraw creates, which is the right order.

E9 and E15 are deliberately not on the list — there are several a map, and a
card that rebuilds itself after each would spend the budget jumping around.
Events about other matches are absent for the same reason in reverse.

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
from the home IP it runs on.

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
  exactly how it is written in `docker-compose.yml` — the setting would have
silently
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
| a multikill (E9) or a clutch (E15) | those following **that player's** team |
| service (E8, E8R) | all enabled subscribers |

**Turning the score around.** The event is oriented on the match's canonical
team. Someone following its opponent is shown the mirrored score — otherwise
they read "13:10" where for them it is "10:13". `format.orient` does the
turning at render time, so different subscribers have different texts of the
same event sitting in the queue.

Both halves of that depend on the payload naming its own team: `team_name`,
because `format.render` otherwise falls back to `TEAM_NAME` from the
environment — the first seed's name, which is wrong for every team added
through the bot afterwards; and `team_id`, because that is what `orient`
compares the reader's team against, and without it the event is handed back
untouched. The live machine builds that header in one place (`_context`); the
schedule machine builds it per event, and for a while built it not at all —
E1, E2 and E3 went out naming the seed team whoever they were about.

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
confirm the bot exists.

There used to be one exception, `/whoami`, so that a newcomer could learn the
id they need to be added under. It was the wrong place to solve that: a command
that answers everybody is a command a stranger can lean on, and it tells them
the bot is there. The id of whoever knocks is written to the log, which is where
the owner reads it from, and [@userinfobot](https://t.me/userinfobot) reports
the same number without involving this bot at all.

**One command is narrower still than the whitelist.** `/verbose` changes the log
level of the whole process, while every other command touches only the caller's
own subscription. It answers the main chat alone — with several subscribers,
and more so with the whitelist off, a service-wide setting must not be a lever
anybody can pull. It is not offered to anybody else either: Telegram takes a
command list scoped to a single chat, so the main chat's hint list carries it
and the default one does not. Offering a command that will refuse you is worse
than not offering it.

**The refusal itself is rate-limited.** Every message from a chat that is not
allowed writes a line to the log, and that line is load-bearing: it is where
the owner reads the id of a chat that has not been added yet, a group's
especially, since nothing else reports that number. It is also the only thing
an outsider can make this bot do, and the container rotates logs at 10 MB — so
an unthrottled line means a stranger can push out the history you would want to
read. A chat is therefore written about once and then left alone for ten
minutes; a different chat knocking is still seen at once.

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
