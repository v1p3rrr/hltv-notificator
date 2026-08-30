# Known limitations

A list of what is deliberately not handled, or handled only in part. Extended
as the work goes on.

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
few seconds later brings the current score. The milestones (E5, E6, E7) are
unaffected: they go through the queue and are not lost.

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

## The alarm about a stuck queue goes into that same queue

If Telegram is not accepting messages, the notification about it will join that
same stuck queue and only get through together with the rest. There is no way
around this while staying within a single delivery channel. Until then the state
is visible in `/status` and in the logs.
