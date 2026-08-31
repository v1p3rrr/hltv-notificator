# Operations

> Notification types are referred to by code throughout — `E6` is the end of a
> map, `E5` the start of one. The full list is in
> [../README.md#event-codes](../README.md#event-codes).

## What the service can do today

| Event | State |
|---|---|
| E1 new match, E2 reschedule, E3 cancellation | done, from the team page |
| E4 match started | done, from the match page; waits for the first round while the feed reports a warmup |
| E7 match finished | done, from the live feed by the map count; the page confirms |
| E11 map point | done, from the live feed; a separate one for every overtime |
| E12 half time | done, from the live feed, `/settings half` (default `HALF_ALERTS`), **off by default** |
| E13 a new overtime begins | done, from the live feed, `/settings overtime` (default `OVERTIME_ALERTS`), **off by default**; separate from E12 because a half comes on every map and an overtime usually does not come at all |
| E6 map finished with the score | done, **at the winning round** from the live feed; the page confirms |
| A comeback on the map | done, an extra line on E6, `/settings comeback` (default `COMEBACK_ROUNDS`) |
| E5 map started | done, from the live feed, once the warmup is over |
| E8 degradation (the source is silent, the match has stalled) | done |
| "The match has stalled" | only when there is no live feed; between maps the threshold is three times longer |
| The live score message during a map | done, one per map, `/settings card` (default `LIVE_MESSAGE`); it is also the map's card and carries the map start, so it is not opened during the warmup, and it is moved back to the bottom of the chat after E11, E12 and E13 |
| A multikill by a player of our team | done, at the Nth kill, `/settings multikill` (default `MULTIKILL_THRESHOLD`) |

## Starting up

1. `cp .env.example .env` and fill in `TELEGRAM_BOT_TOKEN` (from @BotFather) and
   `TELEGRAM_CHAT_ID` (from @userinfobot). `.env` never enters the repository.
2. The first run must be **with `DRY_RUN=true`**: notifications go to the log
   instead of Telegram. That is how you check the service is not sending noise.
3. `docker compose up -d --build`, logs with `docker compose logs -f`.
4. Once everything looks sensible, set `DRY_RUN=false` and restart.

### Timezone

Everything is stored and computed in UTC. In messages the time is shown in the
`TZ_DISPLAY` zone from `.env` — `Europe/Moscow` by default. Any zone from the
IANA database will do (`Europe/Riga`, `Asia/Tbilisi`, ...); it can be changed on
the fly and the data does not drift, because in the database it is UTC.

Match times come from the `data-unix` attribute (epoch in milliseconds), not
from the text on the page. That matters: HLTV renders the time in the browser's
timezone, so the text "17:00" means different things to different readers —
whereas the epoch is the same for everyone.

Locally without Docker: `PYTHONPATH=src python -m hltv_notify`.
Tests: `python -m pytest`.

**The first run is silent.** All the matches from the team page are written into
the database without notifications — otherwise a dozen and a half messages about
already played matches would arrive at startup. Notifications begin from the
next poll.

The database (`data/hltv.db`) holds both the state and the journal of sent
events. **It must not be lost**: without the journal the service will send
notifications again about everything it considers new.

## Bot commands

The list is registered with Telegram at every start (`setMyCommands`), so
typing "/" in the chat pops it up with descriptions and the Menu button next
to the input field shows the same. `/help` prints it as text, and so does any
command the bot does not recognise.

| Command | What it shows |
|---|---|
| `/teams` | your teams and their mutes |
| `/track <link>` | add a team; its current schedule is recorded silently |
| `/untrack <id>` | switch a team off, the history is kept |
| `/mute <id> <types>` | mute event types for one team |
| `/unmute <id>` | clear the mutes |
| `/remind` | the list of reminders; `/remind 1h` adds, `/remind rm 15m` removes |
| `/tz` | your own timezone |
| `/settings` | your own thresholds; `/settings comeback 12` changes one, `/settings comeback default` returns it to the environment's value |
| `/pause` / `/resume` | the global quiet switch, on top of per-type muting |
| `/menu` | the inline menu: status, live, upcoming, teams and their mutes, reminders, settings, quiet. `/track`, `/tz`, `/check`, `/whoami` and `/verbose` have no buttons |
| `/whoami` | your own chat_id, for allowed chats only |
| `/status` | polling modes, live feed health, match counts, the queue, the last error |
| `/live` | the running match: map, score, series score, map results and **which source** the data came from |
| `/next` | upcoming matches as the service sees them |
| `/check` | an out-of-turn schedule check |
| `/verbose on\|off` | debug logging in the service log, without a restart. **The main chat only**: it is a setting of the whole process |

When a notification did not arrive, start with `/live`: it shows whether the
score reached the service at all and whether the live feed is working.

## Polling rates and what to turn

The values live in `.env`; the defaults are a balanced profile:

| Variable | Default | Mode |
|---|---|---|
| `POLL_IDLE_SECONDS` | 1800 (30 min) | no matches within the next 30 min |
| `POLL_PREMATCH_SECONDS` | 180 (3 min) | 30 min before the scheduled start |
| `POLL_LIVE_SECONDS` | 60 | a match is running, there is no live feed |
| `POLL_LIVE_WITH_FEED_SECONDS` | 300 (5 min) | a match is running, scorebot works |
| `PREMATCH_WINDOW_MINUTES` | 30 | how long before the start pre-match mode turns on |
| `LATE_START_GRACE_MINUTES` | 60 | how long a match that should have started keeps pre-match mode |
| `MAX_TEAMS_PER_SUBSCRIBER` | 10 | teams one person may follow; the sweep costs one request per distinct team |
| `LIVE_EDIT_BUDGET` | 10 | card edits a second in total; the per-person interval stretches beyond that |
| `COMMAND_RATE_LIMIT` | 0 | commands per chat per minute, 0 is off; meant for the open mode |
| `COMEBACK_ROUNDS` | 9 | the swing that counts as a comeback; 0 removes the line. A **default**: `/settings comeback` overrides it per person |
| `DEGRADED_ALERT_SECONDS` | 300 | how long before reporting that the service has gone blind (max 600) |

The ceiling of **1 request every 30 seconds** is hardcoded and cannot be raised
by config. Requests are sequential, with ±20% jitter.

**The live feed's long poll does not fall under that ceiling.** It is a held
connection (the equivalent of a websocket), one per active match, not frequent
polling.

## The card stays at the bottom

The live score card is a fixed message in a moving chat, so anything sent after
it leaves the reader scrolling back for the score. After a milestone of the
same map the card is therefore **deleted and sent again** below it, keeping the
score it had; the next ordinary redraw edits the new message as usual.

| Moves the card | Does not |
|---|---|
| E11 (map point) | E9 (multikill) — several a map, the card would jump about |
| E12 (half) and E13 (each new overtime) | anything about a different match |
| | E5 and E6 — one lives in the card, the other ends it |

The move is triggered by the **queue**, not by the feed, right after it has
delivered that chat's messages. Half time is precisely when the feed falls
silent, so a card waiting for the next frame could sit above the message for a
minute. A burst — a map point and then half time — costs one move, not two.

Nothing to configure: `/settings card off` already turns the whole card off,
and with no card there is nothing to move.

## Settings that are per person

`/mute` says "never this type for this team". `/settings` says "not this one":

| Name | What it is | Default from |
|---|---|---|
| `multikill` | kills in a round worth an alert; `0` off | `MULTIKILL_THRESHOLD`, `MULTIKILL_ALERTS` |
| `comeback` | swing in the score difference worth a line on E6; `0` off | `COMEBACK_ROUNDS` |
| `half` | a message when the sides swap | `HALF_ALERTS` (default `PHASE_ALERTS`) |
| `overtime` | a message at the start of every overtime | `OVERTIME_ALERTS` (default `PHASE_ALERTS`) |
| `card` | the live score card during a map | `LIVE_MESSAGE` |

A row is written only when somebody changes something, so raising a default in
`.env` still reaches everyone who never touched it, and `/settings <name>
default` deletes the row rather than freezing today's value into it.

**How this survives "one event, many recipients".** An event is born once (see
[architecture.md](architecture.md)), so the thresholds cannot live in the state
machine — one machine cannot emit an E9 that is simultaneously a 3k and a 5k.
Instead:

* the machine builds at the **lowest** threshold anybody is using
  (`threshold_in_use`), because an event never born cannot be given to the
  person who wanted it, while one born too eagerly can be withheld;
* the queue withholds it — `outbox._wants` compares the payload's number with
  each recipient's own;
* the comeback line is decided later still, in the renderer, because it is not
  an event but a line inside E6, and the message is already being written per
  reader.

Two consequences worth knowing:

* a threshold changed in the middle of a map takes effect on the **next** map:
  the tracker is built when the map starts;
* one person setting `multikill 3` makes the service track 3k rounds for
  everybody. That is work, not messages — nobody else receives them.

## Several users

Allowed accounts are listed in **one** variable, separated by commas:

```
TELEGRAM_CHAT_ID=123456789,987654321,-1001234567890
```

Semicolons and surrounding spaces are accepted too. **The first one in the list
is the main chat**: the team from `TEAM_ID` is seeded there on the first run,
and messages go there while there are no subscribers in the database. For groups
and channels the id is negative, which is normal; non-numeric values are skipped
with a line in the log.

> **When upgrading from an older version.** There used to be a separate variable
> `TELEGRAM_ALLOWED_CHATS` — it no longer exists, all ids live in
> `TELEGRAM_CHAT_ID`. Move them there, separated by commas. An old line left in
> `.env` is simply ignored, but the service writes about it to the log at
> startup: if the whole list lived only there, the whitelist ends up empty and
> the bot stops answering anyone at all.

With `TELEGRAM_WHITELIST_ONLY=true` (the default) the bot stays silent to
everyone else, `/whoami` included: any answer at all confirms the bot exists
to whoever probed it. The refused `chat_id` goes to the log, and that is
where an id that is not on the list yet comes from:

```
command /start from chat -1001234567890 refused: not on the whitelist
```

It is also how a **group** is added, since the id needed there belongs to the
group rather than to any person: put the bot in the group, send it anything,
take the number out of the log. A person's own id also comes from
@userinfobot.

An allowed account becomes a subscriber on its first message to the bot and
builds its own team list through `/track`.

**`TELEGRAM_CHAT_ID` is required even with the whitelist off.** The first id is
the main chat: the seed team goes there, messages go there while nobody has
subscribed, and it is the only chat allowed to change the log level. With the
variable empty the bot is not started at all and nothing is sent — which from
the outside looks like a dead service, so the startup log says so explicitly.

**Commands that act on the service rather than on a subscription** answer the
main chat alone. Today that is `/verbose`. They are also kept out of everyone
else's hint list: Telegram takes a separate command list scoped to one chat,
so the main chat sees them and nobody else does.

Muting applies **to the pair "subscriber + team"**. If tracked teams play each
other, the event reaches a subscriber when at least one of their teams in that
match wants it: otherwise one team would silently mute notifications about the
other.

## The pause

`/pause` switches notifications off entirely, on top of per-type muting —
including the live score message and the service alarms. What is missed is
**not accumulated and not delivered later**: the point of the pause is silence,
not deferred delivery. `/resume` restores everything.

## Healthcheck

The image has a `HEALTHCHECK`: every five minutes it runs
`python -m hltv_notify --health`, which looks at whether the database opens and
whether the schedule was polled not too long ago. What is checked is the work
itself, not the presence of a process: a hung process looks alive to Docker, and
without this `restart: unless-stopped` would leave it alone.

The same command can be called by hand:

```bash
docker compose exec hltv-notify python -m hltv_notify --health
```

## The "service degraded" alarm

It arrives when notifications have stopped working: the schedule or the match
page cannot be read, the live feed will not come up, the queue is not reaching
Telegram.

The threshold depends on urgency. If there is less than a minute to the match
start, the match should have started, someone is three rounds from winning or an
overtime is being played — the alarm comes after a minute. Otherwise after
`DEGRADED_ALERT_SECONDS`.

One alarm per failure; when everything recovers, a separate "Recovered" arrives
with the duration of the outage. A "Recovered" only follows an alarm that was
actually sent — a failure that healed before the threshold, or before the
poller's next attempt, is never announced in either direction. Which subsystems
are down right now is visible in `/status`.

## Proxy

If HLTV or Telegram cannot be reached directly from the server's network, all
outbound traffic is redirected through the standard environment variables — no
custom ones are introduced for this:

| Variable | For what |
|---|---|
| `HTTP_PROXY` | addresses on `http://` |
| `HTTPS_PROXY` | addresses on `https://` — that is, practically everything |
| `ALL_PROXY` | the fallback when the previous two are not set |
| `NO_PROXY` | a comma-separated list of exceptions |

Schemes: `http://`, `https://`, `socks5://`, `socks5h://`. The difference
between the last two is who resolves the name: with `socks5h` it is the proxy,
and that is usually the one you need to get out of a closed network.
Credentials go in the address (`socks5h://user:pass@host:1080`); they never
reach the log.

In `docker-compose.yml` it looks like this:

```yaml
    environment:
      HTTP_PROXY: http://192.168.1.10:20171
      HTTPS_PROXY: http://192.168.1.10:20171
      ALL_PROXY: socks5h://192.168.1.10:20170
      NO_PROXY: localhost,127.0.0.1,192.168.1.0/24
```

The proxy applies to **all** three outbound directions: HLTV pages, the scorebot
live feed and the Telegram Bot API. There are no separate variables for each —
they can be split apart with exceptions:

```
# everything through the proxy, but go to Telegram directly
ALL_PROXY=socks5h://192.168.1.10:20170
NO_PROXY=api.telegram.org
```

The decision is made per address rather than "by the session's host", so an
exception can be attached to just one thing — for instance only to
`scorebot-lb.hltv.org`, leaving the `www.hltv.org` pages going through the
proxy.

`NO_PROXY` understands names (`hltv.org` covers `www.hltv.org` too), addresses,
subnets in CIDR notation (`192.168.1.0/24`) and `*` — "nowhere through the
proxy".

At startup, if a proxy is configured, a line like
`proxy: https=http://192.168.1.10:20171, bypass: localhost,127.0.0.1` appears in
the log. No line means the variables never reached the process.

Uppercase works. That is worth calling out: libcurl, which the HTTP layer sits
on, **deliberately ignores** uppercase `HTTP_PROXY`, and had the service relied
on it, the setting from `docker-compose.yml` would have silently done nothing.
So the
variables are read by the service itself and passed into the requests
explicitly.

## Revoking access

Remove a chat from `TELEGRAM_CHAT_ID` and restart — at startup the service
switches its delivery off and writes about it to the log. The whitelist used to
close only the way in (commands and buttons) while notifications kept arriving,
because delivery goes by the subscribers table and clearing it out could only be
done by hand.

In open mode (`TELEGRAM_WHITELIST_ONLY=false`) nobody is switched off: there is
no list there. An empty list switches nobody off either — that is almost
certainly an unfinished `.env` rather than an intent.

To restore access: put the id back and restart, and both commands and the
subscription come back to life.

## When failures start

First look at what exactly is in the logs and in the bot's `/status`.

### HTTP 403 on HLTV pages
That means the TLS fingerprint has stopped getting through. Headers have nothing
to do with it — permuting them will achieve nothing, and that is measured (an
ordinary client gets a 403 where `curl_cffi` gets a 200).

What to do: update `curl_cffi`, change the profile in `HTTP_IMPERSONATE`
(`chrome` → `chrome124`, `safari`, `edge` — the list depends on the library
version). Do **not** raise the polling rates while doing so.

### The live feed will not come up
The service does not fall over because of it: the match page is polled as usual
and E4, E6 and E7 arrive through it. What is lost is speed (the page updates
late) and the E5 event. `/status` shows whether the feed is up.

### HTTP 403 on the live feed's polling
Observed while recording a real match: after roughly two minutes of work the
session burned out and every subsequent request started getting a 403.

This is **not a network failure but a "back off"**. The service handles it
separately from disconnects: a new session with a warm-up (a GET of the match
page sets the Cloudflare cookies) and a pause of
`LIVE_FEED_COOLDOWN_SECONDS` (600 by default, never below 60). Throughout that
time match-page polling works as usual, so E4, E6 and E7 arrive; only E5,
multikills and speed are lost.

If 403s keep coming — increase the pause and live on page polling.

### HTTP 429
Not observed directly, but if it appears it means the rate is too high. Raise
`POLL_IDLE_SECONDS` to 3600 and `POLL_LIVE_SECONDS` to 120, that is, switch to
the frugal profile. Speed back up only if the 429 does not recur for a day.

### HTTP 520 and long-poll timeouts
Normal, cured by the usual reconnect with backoff. `curl: (28) timed out after
45000ms` on the live feed arrives routinely when the map is paused and does not
have to mean the feed is dead.

### "request to a foreign address refused" in the log
The service is only allowed to go to `hltv.org`, `www.hltv.org` and
`scorebot-lb.hltv.org`. This line means a match address leading somewhere else
ended up in the database — that is, the markup on the team page is not what is
expected. The request did not go out.

How to look into it: find the match in the database (`SELECT match_id, url FROM
matches WHERE url NOT LIKE 'https://www.hltv.org/%'`) and look at the team page
with your own eyes. If HLTV simply changed the link format — fix
`MATCH_SLUG_RE` in `sources/team_page.py`.

### A proxy is set and everything times out
Check that the log has a `proxy: …` line at startup — without it the variables
never reached the container (a common cause: they are in `.env` but `env_file`
is attached to the wrong service).

Next, `curl: (7) Failed to connect to www.hltv.org:443 over proxy` means the
proxy is not answering, not that HLTV is unreachable. The proxy is what needs
checking.

The opposite case — there is a proxy but the requests go around it: most likely
the address matched `NO_PROXY`. Subnets there are compared as subnets and names
on a dot boundary, so `hltv.org` covers `www.hltv.org` but not `nothltv.org`.

### The parser returned zero matches on an HTTP 200
Most likely a markup redesign. The service treats this as a source failure (E8)
rather than as "there are no matches". It is fixed by updating the selectors;
the test fixtures live in `docs/recon/fixtures/` and are refreshed with
`scripts/fetch_fixtures.py`.
