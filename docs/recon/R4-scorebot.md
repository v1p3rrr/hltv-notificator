# R4 — The live feed (scorebot)

Date of observation: 2026-08-29. Everything below came from our own connections
to live matches, not from articles and not from old READMEs.

## The entry point

On a **live** match page there is an element:

```html
<div id="scoreboardElement"
     data-scorebot-url="https://scorebot-lb.hltv.org"
     data-scorebot-id="2397053"
     data-max-rounds-regulation="12"
     data-max-rounds-overtime="3"
     data-cs-version="CS2"
     data-team1-id="…" data-team1-name="…"
     data-team2-id="…" data-team2-name="…">
```

Observation: **`data-scorebot-id` equals the match id** (matched on 2396935 and
2397053). So scraping it is not required — `listId` is taken from the match id.
That also matters because `#scoreboardElement` is **absent** on upcoming and
finished pages, meaning it cannot be learned in advance at all.

The historical address `scorebot.hltv.org:10022` from 2015-2018 articles is
stale. The current host is `scorebot-lb.hltv.org`, ordinary HTTPS/WSS with no
non-standard port.

## The protocol

**Engine.IO v3** (`EIO=3`), that is, socket.io v2. The version is critical:
clients for EIO v3 and v4 are incompatible, and `python-socketio` 5.x speaks
EIO4 and will not work with this server.

### The transport: polling only, websocket is closed

This is the recon's main practical conclusion, and it does not follow from
watching a browser. The measurements:

| Client | Transport | Result |
|---|---|---|
| A browser on an hltv.org page | websocket | OK |
| `websockets`, no headers | websocket | **403** |
| `websockets` + `Origin: https://www.hltv.org` | websocket | **403** |
| `websockets` + Origin + a browser `User-Agent` | websocket | **403** |
| `curl_cffi.ws_connect(impersonate="chrome")` + Origin + Referer | websocket | **403** |
| the same, on a session with warmed Cloudflare cookies | websocket | **403** |
| `curl_cffi` GET, **a cold session with no cookies** | polling | **200** |
| `curl_cffi` GET/POST, a warmed session | polling | **200**, frames flow |

That is, the websocket upgrade to `scorebot-lb.hltv.org` does not get through
under any headers, cookies or impersonation profiles, while polling gets through
even without cookies. A browser starts its session with polling itself (its
traffic shows `?EIO=3&transport=polling` before the upgrade), so polling is a
regular transport of the same protocol, not a workaround.

**The architectural consequence:** the live feed works through the same
impersonated HTTP client as the rest of the project. No separate websocket stack
and no separate socket.io library is needed at all. The implementation is
`scripts/eio3.py`.

### The sequence

```
GET  /socket.io/?EIO=3&transport=polling&t=<ms>
  <- 0{"sid":"…","upgrades":["websocket"],"pingInterval":25000,"pingTimeout":60000}
  <- 40
POST /socket.io/?EIO=3&transport=polling&t=<ms>&sid=<sid>
     body: <len>:42["readyForMatch","{\"token\":\"\",\"listId\":\"<match_id>\"}"]
  <- ok
GET  /socket.io/?EIO=3&transport=polling&t=<ms>&sid=<sid>     (long poll)
  <- 42["log",...] / 42["scoreboard",{...}]
```

Headers: `Origin: https://www.hltv.org`, `Referer` pointing at the match page.
Keep-alive: a POST with the body `1:2` (packet `2` = ping) at least every
`pingInterval` (25 s), to which the server answers `3` in the stream. There is
no authorisation, the token is empty.

### Framing of a polling response

Packets follow one another, each being: the byte `0x00` (string) or `0x01`
(binary), then the length **one digit per byte** (the value 0..9, not ASCII),
then `0xff`, then the body. A textual variant `<len>:<body>` also occurs. The
decoder must work on `response.content` (bytes): decoding to text turns the
length digits into `�` and breaks the parsing.

### The key detail missing from old READMEs

The `readyForMatch` argument must be a **JSON string**, not an object. Both
variants were tested on the same match:

| Sent | Result |
|---|---|
| `42["readyForMatch","{\"token\":\"\",\"listId\":\"2396935\"}"]` | the stream started |
| `42["readyForMatch",{"token":"","listId":"2396935"}]` | **silence**, no error |

The refusal is silent — the server does not answer with an error, the connection
is alive, there is simply no data. Easy to mistake for "this match has no feed".

## The events

Two kinds arrive.

### `scoreboard` — the full scoreboard state

It comes often (roughly 4 frames/s on an active map), **in full every time**.
The fields:

| Field | Meaning |
|---|---|
| `mapName` | `de_mirage`, `de_nuke`, … |
| `currentRound` | the current round number |
| `currentRoundState` | `warmup` / `freezePeriod` / `started` / `ended` |
| `live`, `frozen`, `bombPlanted` | flags |
| `ctTeamScore` / `tTeamScore` | the **current map's** score by side |
| `counterTerroristScore` / `terroristScore` | a duplicate of the same |
| `ctTeamId` / `tTeamId` | who is on which side right now |
| `ctTeamName` / `terroristTeamName` | team names by side (asymmetric, exactly so) |
| `startingCt` / `startingT` | team ids by starting side |
| `regulationHalfLength` / `overtimeHalfLength` | 12 / 3 |
| `ctMatchHistory` / `terroristMatchHistory` | `{firstHalf:[…], secondHalf:[…]}` |
| `roundTimeRemainingMS` | time left in the round |
| `CT` / `TERRORIST` | arrays of 5 players (nick, K/D/A, hp, money, ADR…) |
| `matchFacts`, `ctTeamFacts`, `tTeamFacts` | empty in every observation |

A round-history element:

```json
{ "roundOrdinal": 3, "survivingPlayers": 3, "type": "Target_Bombed" }
```

The `type` values observed: `Target_Bombed`, `Target_Saved`, `CTs_Win`,
`Terrorists_Win`, `lost` (`lost` from the point of view of whichever side's
history is being read).

The sides swap after the break, so **the score must be tied to a team through
`ctTeamId`/`tTeamId`, not through the side.**

### `log` — game events

The payload is a JSON **string** with `{"log":[ {<Type>: {...}}, … ]}`. The
types observed: `MatchStarted`, `RoundStart`, `RoundEnd`, `Restart`, `Kill`,
`Assist`, `BombPlanted`, `Suicide`, `PlayerJoin`, `PlayerQuit`.

**`MatchStarted` carries the map name and fires at the start of every map in the
series**, not once per match. Caught on FORZE's match moving to its second map:

```json
{"log":[{"RoundStart":{}},{"MatchStarted":{"map":"de_dust2"}}, …]}
```

That is the direct signal for E5 ("the map has started") and the marker of a map
boundary in the dump.

## Operational quirks of polling (measured while recording a live match)

The recording of FORZE's match `2397053` produced three things that a short trial
connection does not reveal.

**1. You may only subscribe after packet `40`.** On the first connect `0{…}` and
`40` arrive in one response and the subscription works immediately. On a
reconnect `40` arrived on the **next** poll — and `readyForMatch` sent before it
was silently ignored by the server: the connection is alive, `40` was received,
there are no frames. The same silent refusal as with the wrong payload type. The
client is obliged to wait for `40`.

**2. The session burns out: polling starts returning 403.** The sequence in the
recording: ~2 minutes of normal frames → `HTTP 520` → reconnect → a poll timing
out after 45 s with no data → then steady **403** on every attempt. Reconnects
with the usual backoff (2, 4, 8, 16, 30 s) did not fix anything and only
hammered the source.

The conclusion for the Live Worker: `403` is not a network failure but a "back
off". Handle it separately from timeouts: a new session **with a warm-up** (a
GET of the match page sets the Cloudflare cookies), a pause measured in minutes
rather than seconds, and a switch to the Live Poller as the primary source.
Implemented in `scripts/eio3.py` (`SessionRejected`) and in
`scripts/record_scorebot.py`.

**3. A long poll with no data is normal.** `curl: (28) timed out after 45000ms`
arrives routinely when the map is paused. It is no reason to consider the feed
dead; the sign of death is a 403, or silence while the match page says `LIVE`.

## What is critical for deduplication

**On connecting, the server immediately sends the full current state**, and only
then deltas. After every reconnect the state arrives again from scratch. On top
of that `scoreboard` is sent many times a second with identical content anyway.

Worse: **on connecting, the `log` delivers a backlog of what has already
happened**. The full recording of match `2397053`
(`fixtures/scorebot-2397053-forze.jsonl.gz`, 2150 records, 1977 frames) shows
this in full: over the run there were **15 connects and 14 disconnects**, and
across a series of just two maps **150 `MatchStarted` events** piled up:

```
MatchStarted: de_dust2 ×90, de_mirage ×60
```

That is, every reconnect replays the match's whole history, including a map
played out long before the connection. Disconnects, meanwhile, are normal rather
than an emergency.

A naive "a MatchStarted arrived, send E5" would have produced a barrage of
map-start notifications, some of them about maps finished long ago. E5 must be
born on a **transition** of the match state (the current map number changed),
not on the fact that an event arrived.

It is the same trap in another guise: the logic "a score of 13 arrived,
send E6" is guaranteed to produce duplicates. An event is born only on a
**transition** of state, and the idempotency key is written to `sent_events`
under a unique index.

## The map boundary: the feed does show it

Verified on match 2397091 at the moment of the Mirage → Inferno transition.
Right after the map changed, a connection to the feed returned:

```json
{ "mapName": "de_inferno", "currentRound": 1, "currentRoundState": "warmup",
  "live": false, "ctTeamScore": 0, "tTeamScore": 0 }
```

So the map change is visible through several signals at once: `mapName` changes,
the round number resets to 1, the state goes to `warmup`, the score is zeroed.
Plus `MatchStarted` with the new map's name arrives in the `log`.

This also pins down the meaning of `live`: it is **not** "the match is running"
but "the map is in play". At the end of a map it stayed `true` with
`currentRoundState: "ended"`, and during the next map's warmup it became
`false`.

The earlier recording (match 2397053) showed no `mapName` change not because the
feed does not report it but because the recording was made on the series' last
map.

**During the break between maps the feed goes quiet.** The long poll returns on
a timeout with no data — 45 seconds of silence is normal here, not a sign that
the feed has died.

## What the feed does not give

- **A signal that a map has ended.** The moment a map ended was caught on
  FORZE's match: `currentRound:23`, the score 13:10,
  `currentRoundState:"ended"`, `frozen:true`, but `live` stays `true`. `ended`
  is the round's state, and it also occurs at the end of any ordinary round.
- **The series score by maps.** The feed only has the current map's score. The
  map boundary is visible through `MatchStarted` and through the reset of
  `mapName`/the score, but how many maps each team has already won is not
  reported.
- **The end of the match.** In the `2397053` recording the match finished and
  the feed simply stopped sending changes: no separate event, no flag change.
  "The match has finished" cannot be told from "there is a long pause" by the
  feed alone — the match page does that.

That is why the source of truth for E6/E7 is the match page
([R3](R3-schedule-source.md)), while the feed provides the speed and E5.

## Fixtures

- `fixtures/scorebot-2397053-forze.jsonl.gz` — raw frames of FORZE's live match,
  gzip JSONL, written by `scripts/record_scorebot.py`. Consecutive
  byte-identical frames are collapsed into `{"kind":"repeat","n":N}`;
  disconnects and reconnects are marked `disconnect`/`connect` — the
  deduplication replay test uses exactly those.
