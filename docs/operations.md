# Operations

## What the service can do today

| Event | State |
|---|---|
| E1 new match, E2 reschedule, E3 cancellation | done, from the team page |
| E4 match started, E7 match finished | done, from the match page |
| E6 map finished with the score | done, **at the winning round** from the live feed; the page confirms |
| E5 map started | done, from the live feed |
| E8 degradation (the source is silent, the match has stalled) | done |
| "The match has stalled" | only when there is no live feed; between maps the threshold is three times longer |
| The live score message during a map | done, one per map, `LIVE_MESSAGE` |
| A multikill by a player of our team | done, at the Nth kill, `MULTIKILL_THRESHOLD` |

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

| Command | What it shows |
|---|---|
| `/teams` | your teams and their mutes |
| `/track <link>` | add a team; its current schedule is recorded silently |
| `/untrack <id>` | switch a team off, the history is kept |
| `/mute <id> <types>` | mute event types for one team |
| `/unmute <id>` | clear the mutes |
| `/remind` | the list of reminders; `/remind 1h` adds, `/remind rm 15m` removes |
| `/tz` | your own timezone |
| `/pause` / `/resume` | the global quiet switch, on top of per-type muting |
| `/menu` | the same thing with buttons |
| `/whoami` | your own chat_id, answered for everyone |
| `/status` | polling modes, live feed health, match counts, the queue, the last error |
| `/live` | the running match: map, score, series score, map results and **which source** the data came from |
| `/next` | upcoming matches as the service sees them |
| `/check` | an out-of-turn schedule check |
| `/verbose on\|off` | verbose logs |

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
| `DEGRADED_ALERT_SECONDS` | 300 | how long before reporting that the service has gone blind (max 600) |

The ceiling of **1 request every 30 seconds** is hardcoded and cannot be raised
by config. Requests are sequential, with ±20% jitter.

**The live feed's long poll does not fall under that ceiling.** It is a held
connection (the equivalent of a websocket), one per active match, not frequent
polling.

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
everyone else — their `chat_id` goes to the log so there is something to add to
the list.

An allowed account becomes a subscriber on its first message to the bot and
builds its own team list through `/track`.

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
with the duration of the outage. Which subsystems are down right now is visible
in `/status`.

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

In `compose.yaml` it looks like this:

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
on it, the setting from `compose.yaml` would have silently done nothing. So the
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
