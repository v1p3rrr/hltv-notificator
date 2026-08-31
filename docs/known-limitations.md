# Known limitations

A list of what is deliberately not handled, or handled only in part. Extended
as the work goes on.

> Notification types are referred to by code throughout — `E6` is the end of a
> map, `E5` the start of one. The full list is in
> [../README.md#event-codes](../README.md#event-codes).

## The match id changes

An organiser sometimes recreates a match and it gets a new id on HLTV. To the
service that looks like the old match disappearing (→ E3 "cancelled") and a new
one appearing (→ E1 "new match"). The user gets two spurious notifications
instead of one "rescheduled".

Not handled: the old and the new id can only be linked reliably by a heuristic
over opponents and tournament, and the cost of getting it wrong (gluing two
different matches together) is higher than the cost of two extra messages.

## HTML is brittle

The schedule source is the team page's markup. A redesign breaks the parser.
Mitigation: a parser that returns zero matches on an HTTP 200 is treated as a
source failure (→ E8) rather than as "there are no matches". The fallback path
is the mobile JSON endpoint, which requires proxying the app's traffic (see R3,
variant A).

## The scorebot websocket transport is unavailable

The live feed runs over Engine.IO's polling transport because the websocket
upgrade returns 403 to non-browser clients (see R4). It is a regular transport
of the same protocol, but it is slightly more expensive in HTTP requests and
could in theory be turned off on HLTV's side separately from websocket.

## Per-round data only exists on a live match

`#scoreboardElement` is only present on a live match page. Connecting to the
feed after the fact is impossible: for a finished match the per-round data is
only available through the statistics page, which robots.txt restricts by
filters. In practice that means: if the service was down during a match, its
detailed data cannot be recovered — only the final score by maps remains.

## E4 is not sent if the match was already finished when we found it

If the service was not running when the match started, the very first poll will
see `Match over` on the page. A "the match started" notification at that point
is stale information, so only the result (E7) is sent. It shows in the log as
"match N discovered already finished, E4 and E6 skipped".

## A match that vanished from the page after its scheduled start

Such a match may equally have started or been cancelled — the schedule cannot
tell them apart. No E3 is sent in that case, the state is set to `UNKNOWN` and
the match stops being polled. If it was in fact running, the event about it is
lost. In practice this happens when a match drops out of "Recent results"
because of list depth rather than because of a cancellation.

## No E6 for maps played before the observation began

The "map finished" event is born on a map's transition from undecided to
decided. If the service saw the match when the first map had already been
played, there will be no notification for it — that is not a transition but the
state at the moment of meeting. The map's result still lands in the database and
in the final E7 message.

The same applies to a match played out in full while the service was down:
showering E6 over every map after the fact is noise, so only the result is sent.

## The maps section on the match page updates late

Observed on live match 2397091: while a map is running, the score in its
`.mapholder` updates by halves rather than by rounds — at a real score of 12:11
the section still read 5:7, the result of the first half. The halves were shown
as `(5:7; --)` at the time.

The practical consequence: an E6 driven by the match page is structurally late —
it arrives not when the map ends but when HLTV updates the section. That is
precisely why scorebot became the primary source for speed while the page
remains the confirmation. There will be no duplication: the event for a map is
born once, whoever brings it first.

## The live feed takes the map number from the page

The feed sends only the map name (`de_mirage`), it has no number in the series.
The number comes from the map lineup read off the match page. While the veto has
not been played and the lineup is unknown, the number is computed as "however
many maps are already recorded, plus one" — correct on the first map, but if the
service connected to the feed mid-series and the page has not managed to record
the previous map yet, the number can come out one lower. This affects neither
the score nor the fact of the notification, only the "Map N" label.

## The live message is not re-delivered after a failure

The live score message goes around the outbox queue: it has no idempotency key,
and there is no point re-delivering a stale score frame. If an edit did not go
through (a Telegram limit, the network), it is simply skipped — the next edit a
few seconds later brings the current score. The milestones (E6, E7) are
unaffected: they go through the queue and are not lost.

E5 is the one that now rides on the live message, so it gets an explicit
fallback: if the message cannot be CREATED for a chat, a plain E5 is queued for
that one chat instead. A failed later EDIT is still only skipped — by then the
map start has already been delivered.

## A reschedule spotted after its own new time is not reported

E2 says "the match has moved to 18:20". Delivered at 18:25 that is not a
notification but a post-mortem, and E4 is about to report the start anyway. So
if the new time has already passed by the moment the service gets to look, the
time is accepted silently and no message is sent. It happens when the service
was blind across the reschedule — HLTV timing out for several polls in a row is
enough.

## Without a live feed, the match start is still reported from the page

E4 waits for a round to actually be played only while the feed is up and says
it is a warmup. With no feed — a 403 cooldown, a feed that will not come up —
the page decides on its own, and the page cannot tell a warmup from a game. So
in that situation "the match has started" can still arrive twenty minutes
early. Deliberate: losing the message entirely would be worse, and the map
lineup it carries is the useful part at that moment anyway.

## The half inside an overtime is not reported

E12 covers the regulation half and the START of each overtime. Sides also swap
in the middle of every overtime, and that is not reported: under MR3 it would
mean a message every three rounds.

## A match that never happens holds the frequent polling for an hour

A match past its start and not running keeps the schedule on the pre-match
cadence for `LATE_START_GRACE_MINUTES` (60 by default), because that is the
window in which HLTV moves it. If it was simply cancelled without the page
saying so, that is up to twenty extra requests spent on nothing. The
alternative — falling back to a poll every half hour — is what lost a
reschedule.

## A map decided without the live feed carries no comeback line

The comeback is measured from the score trajectory, and only the live feed has
one. When a map's result comes from the match page — the feed was down, or in
its cooldown after a 403 — the result arrives as usual and the comeback line
simply is not there. The same after a restart in the middle of a map: the
rounds before it are gone, so the run can come out smaller than it was, or
below the threshold and therefore unmentioned. Understated, never invented.

## Map point is only about the MAP

E11 fires when somebody is one round from taking the map, in regulation and
once per overtime. There is no separate warning for "one map from taking the
match" — instead the map point says so in its own text when winning that map
would decide the series. A team that goes 1-0 up in a BO3 gets no warning of
its own; the next map's map point carries it.

## The map start is not reported during the warmup

"A map has started" waits for `currentRoundState` to leave `warmup`. That is
honest — during the warmup the score is 0:0 and nothing is being played — but it
means you learn which map was picked a few minutes later than the feed knew it.
For a rotation between maps that is usually seconds; before the first map of a
match a warmup can run for twenty.

## A multikill can be missed after a reconnect

The alert is computed from the increment in kills in scoreboard frames between
the start of a round and the current moment. The baseline lives in the worker's
memory. If the connection to the feed was recreated mid-round, the baseline is
taken afresh and a multikill started before the drop goes unnoticed.

This is a deliberate trade in the safe direction: missing a highlight is
annoying, a false alert about a non-existent ace is worse. For the same reason
the alert is not computed from the `Kill` events in the log: on connecting, the
feed replays its backlog and alerts would rain down for long-finished rounds.

Intermediate values between the threshold and an ace are not reported: two
messages per round is already noise.

## Muting cannot be set "for everything at once"

Muting lives on the pair "subscriber + team": an event type cannot be muted
globally with a single command — you have to walk through your teams. For two or
three teams that is tolerable; for a dozen it would be awkward.

## The live score message is not re-oriented per subscriber retroactively

The orientation is applied at render time, so a subscriber who adds a team
mid-map will see the existing message in the old orientation until the next
edit. That edit arrives within a few seconds, so it has almost no practical
significance.

## A failure between two poll cycles may pass unreported

The watchdog only re-evaluates a subsystem when its poller next runs. In idle
mode the schedule is polled every ~30 minutes, so a failure at minute 0 that
heals by minute 30 is never announced — the next attempt simply succeeds. Seen
in the logs: a 35-minute schedule outage produced no alarm at all.

This is deliberate rather than an oversight: in idle mode there are no matches,
so nothing is being missed. Around a match the intervals are 3 minutes and
1 minute, so a real failure is re-checked and reported quickly. What was fixed
is the other half of it — a "Recovered" no longer arrives for an outage nobody
was told about.

## The alarm about a stuck queue goes into that same queue

If Telegram is not accepting messages, the notification about it will join that
same stuck queue and only get through together with the rest. There is no way
around this while staying within a single delivery channel. Until then the state
is visible in `/status` and in the logs.

## It does not scale to a public audience, and the database is not why

The service is built for one person or a handful, and the ceilings it runs into
are both on the way out to the network. Neither is storage: SQLite is a distant
third and swapping it for a database server would buy nothing, because the two
walls in front of it are reached first.

**How many people follow a team costs nothing.** Schedules are polled per
distinct team (`tracked_teams()` groups by `team_id`), match pages per match,
and the live feed keeps one connection per match. Ten people following the same
team produce exactly the requests one person does.

A first guard against wall one is in place: `MAX_TEAMS_PER_SUBSCRIBER` (ten by
default) caps how many teams one person may follow, and the scheduler writes a
line to the log once the total outgrows what the ceiling can sweep in time. It
is a guard, not a solution — the limit is per subscriber while the cost is per
DISTINCT team across everybody, so enough people with disjoint interests still
blow through it.

**Wall one: the request ceiling against the number of distinct teams.** One
request every 30 seconds, hardcoded, strictly sequential. A full sweep of the
schedule therefore costs *teams × 30 s*: five minutes for ten teams, ten for
twenty, half an hour for sixty — while pre-match mode wants to look every three
minutes and the match pages draw from the same budget. The practical ceiling is
somewhere around **10-20 distinct teams per instance**, regardless of the number
of subscribers. Raising the rate is not the answer (see the ceiling in
`config.py`); the live feed is, since a held connection does not draw on that
budget at all.

**Wall two: outgoing Telegram, against the number of recipients.** An event is
rendered and queued per recipient. The queue delivers those in parallel across
chats, so the fan-out itself is no longer the problem — but everything shares
Telegram's ceiling of roughly thirty messages a second, and the live score card
spends it fastest. Its key is `(chat_id, match_id, map_number)`: every
subscriber has their own message, and every one of them is edited every
`LIVE_EDIT_SECONDS`. A hundred people watching one map is ten edits a second;
three hundred is the whole budget gone on the score of a single map, with
nothing left for the events. `LIVE_EDIT_BUDGET` keeps that from turning into
failures — the interval stretches instead, so the cards slow down rather than
break — but slowing down is all it can do. Note what the binding limit is not: one edit per
ten seconds is far inside the per-chat allowance of about one a second. It is
the sum across chats that runs out, which is why no setting fixes it — the
message is per chat because the score is turned around to face each recipient's
team.

What a public version would need is therefore a different delivery shape, not a
bigger machine: one post per team into a channel instead of N private messages,
which turns the cost from "per subscriber" into "per team" — at the price of the
per-recipient orientation, since a channel can only show the score from one
side. The live card would have to live in that channel too, or not exist.

Two smaller things become real at that size as well: every chat that writes to
the bot becomes a subscriber row, which with the whitelist off is unbounded, and
command handling has no per-chat rate limit.

## The queue's throughput is bounded by Telegram, not by the queue

The two limits Telegram works to are different in kind, and the queue now
answers each with its own thing: `SEND_INTERVAL_SECONDS` (1.2 s) spaces messages
**within one chat**, and `GLOBAL_SENDS_PER_SECOND` (25) is all that different
chats share. Chats are drained in parallel, each strictly in queue order, so a
fan-out to twenty people takes about a second instead of the twenty-four it used
to — while two messages for the same person still cannot overtake each other,
which is what the live card depends on.

What remains is Telegram's own ceiling of roughly thirty a second in total. The
queue cannot go faster than that, and neither can anything else: it is the same
budget the live card's edits are drawn from.

## A per-person threshold reaches the next map, not this one

`/settings multikill 3` and `/settings comeback 12` take effect on the next
map. The trackers that measure both are built when a map starts, from the
lowest threshold in use at that moment, and they are not rebuilt while the map
runs — rebuilding would throw away the trajectory measured so far, which cannot
be recovered from the current score.

There is no plan to change it: the alternative is to keep the full round-by-round
history so a tracker can be replayed at a new bar, and a map's worth of that per
match is not worth the two minutes it saves anybody.

## A raised threshold does not save any work

The service measures at the LOWEST bar anybody is using, so one subscriber
asking for three-kill rounds means three-kill rounds are counted for the whole
service. Everyone else simply does not receive them — the queue withholds the
event — but the arithmetic runs.

That is deliberate and the cost is negligible: it is a comparison on frames
already being read for the score. It is worth knowing only because "I raised my
threshold, so the service does less" is a reasonable thing to assume, and it is
not true.

## Moving the card down loses the intermediate copies

The card is deleted and sent again after a map point or half time, so the chat
history keeps only the last one. The score at each milestone is not lost — E11
and E12 quote it, and E6 records the map's final score — but there is no trail
of the card itself.

That is what "always in the last message" costs, and it is the trade the owner
asked for. `/settings card off` turns the card off entirely if the history
matters more.

## Two matches live in one chat cannot both be last

The card is moved for milestones of its own map only, so with two tracked teams
playing at the same time the two cards do not fight each other — but only one
of them can be the bottom message, and the other stays wherever it was. Nothing
is lost; it is simply not what "always last" suggests.

## A delete Telegram refuses leaves the card where it is

Telegram can decline `deleteMessage` — the message is too old, or the bot lost
the right in a group. The card then stays in place and is edited as before, and
the burial is written off rather than retried, so a chat that refuses deletes
does not get one attempt per frame for the rest of the map. It is logged at
WARNING.
