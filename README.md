# hltv-notificator

Telegram notifications about CS2 matches on [HLTV](https://www.hltv.org),
at a granularity the site's own subscriptions do not offer.

You pick the teams you care about; the service watches their schedule, watches
the matches while they are being played, and writes to you in Telegram — when a
match appears, when it is moved, when it starts, when **each map ends and with
what score**, when someone reaches map point, when a player of your team takes
a 4k.

It is one small Python process. It stores everything in a single SQLite file,
opens no ports, has no web interface and needs no database server. The whole
interface is a chat with your bot.

```
✅ Map 2 finished
Dust2 — 13:11
Vitality — Spirit
Series score: 2:0
🔥 Comeback: Vitality turned 3:11 around — a swing of 10 rounds
BLAST Premier World Final 2026
Match page
```

---

## Contents

* [Why this exists](#why-this-exists)
* [What arrives in the chat](#what-arrives-in-the-chat)
* [Requirements](#requirements)
* [Quick start](#quick-start)
* [Running without Docker](#running-without-docker)
* [Using the bot](#using-the-bot)
* [Following teams](#following-teams)
* [Several people, one service](#several-people-one-service)
* [Settings](#settings)
* [How it works](#how-it-works)
* [Data and backups](#data-and-backups)
* [Updating](#updating)
* [When something goes wrong](#when-something-goes-wrong)
* [Development](#development)
* [What it deliberately does not do](#what-it-deliberately-does-not-do)
* [Documentation](#documentation)
* [License](#license)

---

## Why this exists

HLTV's built-in subscriptions can tell you "the match started" and "the match
finished". If you follow a team through a best-of-three, that is not enough:
you want to know that map one ended 13:11 the moment the winning round is
played — not twenty minutes later when the site's maps section catches up, and
not at all if you happened to be away.

This service fills exactly that gap. It takes the schedule from the team page,
the match state from the match page, and the round-by-round detail from HLTV's
own live feed (the same one that drives the scoreboard in your browser), and it
turns every meaningful change into one message.

It is deliberately small and single-purpose: **CS2, HLTV, Telegram.** No other
games, no other sites, no dashboards.

---

## What arrives in the chat

Roughly in the order they turn up over the life of a match. Every type can be
switched off individually, per team, from the bot.

| Notification | Where it comes from |
|---|---|
| A new match appeared in the team's schedule | team page |
| The match time changed | team page |
| The match was cancelled or removed | team page |
| A reminder, N minutes before the start | your own setting |
| **The match started** — with the map lineup and whose pick each map is | match page |
| A map started | live feed |
| A player of a followed team took 4+ kills in one round | live feed |
| Half time, and the start of every overtime *(off by default)* | live feed |
| **Map point** — someone is one round from taking the map | live feed |
| **A map finished, with the score** — plus a comeback line when there was one | live feed, confirmed by the page |
| The match finished, with the result map by map | live feed, confirmed by the page |

And one that is not about a match at all: the service says when it has **gone
blind** — the source is unreachable, the live feed will not come up, messages
are not reaching Telegram — and says again when it has recovered.

### The live map card

While a map is being played you also get **one message per map**, edited in
place with the current score and frozen on the final one:

```
🗺 Map 1: Nuke
Vitality 7:5 Spirit · round 13 · freeze time
Series score: 0:0
```

That card *is* the map's announcement — it carries "the map started", which is
why E5 does not arrive as a separate message when the card is enabled. Turn it
off with `LIVE_MESSAGE=false` if you would rather have a quiet chat.

### A few more examples

```
🆕 New match
Natus Vincere — FaZe
ESL Pro League Season 25
🕒 Sat 6 Sep, 18:00
Match page
```

```
🔴 Match started
Vitality — Spirit
BLAST Premier World Final 2026 · BO3
Nuke      our pick
Dust2     their pick
Inferno   decider
Match page
```

```
🚨 Map point — Spirit
Inferno — 11:12
Vitality — Spirit
One round from taking the map — and the match
```

```
💥 ZywOo — ACE
Mirage, round 14 · score 8:6
Vitality — Spirit
```

Times are shown in your own timezone (`/tz`); everything is stored in UTC.

### Event codes

Every notification type has a short code. You do not need it to use the bot —
the menu names them all in words — but it appears in the logs, in the
documentation, and in the text `/mute` command, so here is the whole list:

| Code | Notification |
|---|---|
| `E1` | a new match in the schedule |
| `E2` | the match time changed |
| `E3` | the match cancelled or removed |
| `E4` | the match started |
| `E5` | a map started |
| `E6` | a map finished, with the score |
| `E7` | the match finished |
| `E8`, `E8R` | the service has gone blind / has recovered |
| `E9` | a multikill: 4+ kills in one round |
| `E10` | a reminder before the match |
| `E11` | map point |
| `E12` | half time, or a new overtime |

(The table above is in the order things happen; this one is by number, because
that is how you look a code up.)

---

## Requirements

**With Docker (recommended):** Docker with the Compose plugin
(`docker compose version`), and a machine that stays on — a mini-server, a NAS,
a VPS, a Raspberry Pi. A few megabytes of disk.

A prebuilt image is published at
[**`vprlol/hltv-notificator`**](https://hub.docker.com/r/vprlol/hltv-notificator)
for `linux/amd64`, so on an ordinary x86 server nothing has to be compiled. On
arm — a Raspberry Pi, an Apple-silicon Mac — build it locally instead; it is
one extra flag, shown below.

**Without Docker:** Python 3.10 or newer.

Either way you need **outbound** access to `hltv.org`, `scorebot-lb.hltv.org`
and `api.telegram.org` — directly or through a proxy. Nothing needs to be
reachable from outside: the bot uses long polling, not webhooks, so no ports
are opened and no domain is needed.

---

## Quick start

### 1. Create the bot

Message [@BotFather](https://t.me/BotFather), send `/newbot`, follow the
prompts, and copy the token it gives you (it looks like `123456:AA...`).

### 2. Find your chat id

Message [@userinfobot](https://t.me/userinfobot) — it replies with your numeric
id. Then **send your own new bot a `/start`**: Telegram does not let a bot
write to someone who has never written to it first.

### 3. Get the files

**Pulling the published image** — two files are all you need, no sources, no
build:

```bash
mkdir hltv-notify && cd hltv-notify
```

```bash
curl -O https://raw.githubusercontent.com/v1p3rrr/hltv-notificator/main/docker-compose.yml && curl -o .env https://raw.githubusercontent.com/v1p3rrr/hltv-notificator/main/.env.example
```

**Or building it yourself** — clone the repository instead; you get the docs
and the tests with it, and this is the path to take on arm:

```bash
git clone https://github.com/v1p3rrr/hltv-notificator.git && cd hltv-notificator && cp .env.example .env
```

### 4. Fill in `.env`

The required minimum is two lines:

```
TELEGRAM_BOT_TOKEN=123456:AA...
TELEGRAM_CHAT_ID=123456789
```

Leave `DRY_RUN=true` for the first run — notifications go to the log instead of
Telegram, so you can see what the service *would* have sent.

Set `TEAM_ID`, `TEAM_SLUG` and `TEAM_NAME` to the team you want seeded on the
first run — the file ships with the author's team as an example. All three come
straight out of a team's URL: `https://www.hltv.org/team/9565/vitality` gives
`TEAM_ID=9565`, `TEAM_SLUG=vitality`, `TEAM_NAME=Vitality`. You can also leave
it as it is and add your teams from the chat afterwards.

Leave `IMAGE=` commented out unless you want to pin a specific version — with
nothing set, Compose pulls `vprlol/hltv-notificator:latest`.

### 5. Start it

```bash
docker compose up -d
```

Building from source instead — add the dev overlay, which swaps the pulled
image for a local build:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

Watch it come up:

```bash
docker compose logs -f
```

You should see something like:

```
first tracked team taken from the config: Vitality (id 9565)
DRY_RUN is on: notifications go to the log, not to Telegram
service started: 1 subscriber(s), 1 team(s) watched (Vitality)
GET https://www.hltv.org/team/9565/vitality -> 200 in 0.28s
team 9565 taken under observation: 17 matches recorded silently
```

**The first run is always silent.** The whole schedule is written into the
database without notifications — otherwise you would be greeted by a dozen
messages about matches played weeks ago. Notifications start from the next
poll.

### 6. Check the bot answers

Send it `/menu`. A menu with buttons should come back. (In `DRY_RUN` the bot
still talks to you — only the *notifications* are held back.)

### 7. Go live

Set `DRY_RUN=false` in `.env` and restart:

```bash
docker compose up -d
```

### 8. Set it up to taste, from the chat

```
/track https://www.hltv.org/team/9565/vitality
/tz Europe/Berlin
/remind 15m
```

Publishing an image of your own, pinning a version, backups and CI are covered
in [docs/deployment.md](docs/deployment.md).

---

## Running without Docker

```bash
python -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

```bash
PYTHONPATH=src .venv/bin/python -m hltv_notify
```

On Windows the paths are `.venv\Scripts\pip` and `.venv\Scripts\python`.

Settings are read from the environment, so either export them yourself or use
an `.env` loader of your choice — the service does not read `.env` itself,
Docker Compose does.

It also installs as an ordinary package:

```bash
pip install git+https://github.com/v1p3rrr/hltv-notificator
```

```bash
hltv-notify
```

It is not published to PyPI. For a long-running setup an ordinary systemd unit
(`Type=simple`, `Restart=always`, `EnvironmentFile=`) works fine; there is no
unit file in the repository because Docker is the supported route.

---

## Using the bot

`/menu` (also `/start` and `/help`) opens an inline menu, and **day to day it
is all you need**: status, the running match, what is coming up, your teams,
reminders and the quiet switch. Inside a team's menu every notification type
toggles with a tap — no codes, no ids to remember.

You do not have to memorise any of it. Type **`/`** in the chat and Telegram
pops up the whole list with descriptions — the same list sits behind the
Menu button next to the input field. `/help` prints it as text, and so does any
command the bot does not recognise.

Not everything has a button: a few commands carry a value that has to be typed
— a team link, a timezone. The last column says which is which.

| Command | What it does | Button? |
|---|---|---|
| `/menu`, `/start`, `/help` | the menu; `/help` and `/start` also print this whole list | — |
| `/status` | health of the service, the sources and the live feed | yes |
| `/live` | what is happening in a running match, and which source the data came from | yes |
| `/next` | upcoming matches as the service sees them | yes |
| `/teams` | which teams you follow, and what is muted for each | yes |
| `/track <team link>` | start following a team — paste the link to its HLTV page, or just its numeric id | **no** — it needs the link |
| `/untrack <id>` | stop following it (history is kept) | yes |
| `/mute <id> <E5,E9>` | mute notification types for one team | yes |
| `/unmute <id>` | clear that team's mutes | yes |
| `/remind 15m` | remind 15 min before a match; `/remind rm 15m` removes it | partly — 10, 15, 30 min, 1 h and 2 h are buttons, any other interval is typed |
| `/tz Europe/Berlin` | your timezone | **no** |
| `/pause`, `/resume` | go completely quiet / start receiving again | yes |
| `/check` | read the schedule now instead of waiting for the next cycle, which is up to 30 min when nothing is due | **no** |
| `/whoami` | your numeric `chat_id` — the value that goes into `TELEGRAM_CHAT_ID` | **no** |
| `/verbose on`, `/verbose off` | turn debug logging on and off without restarting the container. It changes the service's log only, never what arrives in the chat. **Main chat only** — it is a setting of the whole service | **no** |

`/mute` wants the codes from [Event codes](#event-codes) above: `/mute 9565
E5,E9` keeps the team but drops its map-start and multikill messages. Muting by
button needs no codes at all. `E8`, the "service has gone blind" alarm, is
deliberately not mutable — silencing it means never learning that the service
stopped seeing anything.

`/pause` silences everything, including the live card and the service's own
alarms. Nothing is queued while you are paused — the point is silence, not
deferred delivery.

---

## Following teams

The list of teams lives in the database and is edited **through the bot**.
`TEAM_ID` in `.env` is used only for the very first seed, when the list is
still empty.

```
/track https://www.hltv.org/team/9565/vitality
```

Filtering is strictly by **numeric team id**, never by name: names change,
repeat each other and pick up `ex-` prefixes. That is why `/track` takes a link
rather than a name — the id is in the link.

Adding a team records its current schedule silently, so you do not get a burst
of "new match" notifications about fixtures that already exist.

If two teams you follow play **each other**, it stays one match: one
notification per subscriber, with the score turned around to face whichever
team that person follows. Multikills are the exception — a 4k belongs to the
player's own team and goes to the people following that team.

---

## Several people, one service

One instance can serve several Telegram accounts. Each has its own team list,
its own mutes, its own reminders and its own timezone.

Allowed accounts are listed in **one** variable, ids separated by commas:

```
TELEGRAM_CHAT_ID=123456789,987654321,-1001234567890
```

The **first id is the main chat**: the team from `TEAM_ID` is seeded there, and
that is where messages go while nobody has subscribed yet. Group and channel
ids are negative — that is normal.

With `TELEGRAM_WHITELIST_ONLY=true` (the default) the bot ignores everyone
else completely — no command answers a stranger, not even `/whoami`, because a
reply confirms the bot is there and gives them something to lean on. Their
`chat_id` goes to the log, which is where you take it from to widen the list;
they can also read it off [@userinfobot](https://t.me/userinfobot) themselves.
Turn the whitelist off only deliberately: a Telegram bot has a public address,
and without it anyone who finds yours can command it.

An allowed account becomes a subscriber on its first message to the bot.

---

## Settings

All settings are environment variables; [.env.example](.env.example) documents
every one of them together with the reasoning behind its default. The ones you
are most likely to touch:

**Required**

| Variable | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the token from @BotFather |
| `TELEGRAM_CHAT_ID` | allowed chat ids, comma separated; the first is the main one |

**Behaviour**

| Variable | Default | Meaning |
|---|---|---|
| `DRY_RUN` | `true` | notifications go to the log instead of Telegram |
| `TZ_DISPLAY` | `Europe/Moscow` | timezone used in messages |
| `LIVE_MESSAGE` | `true` | the live score card during a map |
| `LIVE_EDIT_SECONDS` | `10` | how often that card is edited (do not go below 5) |
| `REMINDERS` | `15` | default pre-match reminders for a new subscriber, in minutes |
| `MULTIKILL_ALERTS` | `true` | alert on a big round by one of your players |
| `MULTIKILL_THRESHOLD` | `4` | how many kills counts as one |
| `PHASE_ALERTS` | `false` | alert on half time and on each new overtime |
| `COMEBACK_ROUNDS` | `9` | swing that counts as a comeback; `0` removes the line |
| `TELEGRAM_WHITELIST_ONLY` | `true` | answer only the listed chats |

**Polling** — the ceiling of **one request every 30 seconds** is hardcoded, and
these values cannot raise it.

| Variable | Default | When it applies |
|---|---|---|
| `POLL_IDLE_SECONDS` | `1800` | nothing due within the next half hour |
| `POLL_PREMATCH_SECONDS` | `180` | shortly before a scheduled start |
| `POLL_LIVE_SECONDS` | `60` | a match is running and there is no live feed |
| `POLL_LIVE_WITH_FEED_SECONDS` | `300` | a match is running and the feed works |
| `PREMATCH_WINDOW_MINUTES` | `30` | how early pre-match mode turns on |
| `LATE_START_GRACE_MINUTES` | `60` | how long an overdue match keeps pre-match mode |

**Storage and health**

| Variable | Default | Meaning |
|---|---|---|
| `DB_PATH` | `data/hltv.db` | the SQLite file |
| `OUTBOX_KEEP_DAYS` | `90` | how long sent-queue rows are kept |
| `EVENTS_KEEP_DAYS` | `365` | how long the journal of sent notifications is kept |
| `DEGRADED_ALERT_SECONDS` | `300` | how long before reporting that the service is blind |

**Proxy** — the standard names are honoured: `HTTP_PROXY`, `HTTPS_PROXY`,
`ALL_PROXY`, `NO_PROXY`, with `http://`, `https://`, `socks5://` and
`socks5h://` schemes. Uppercase works. See "Proxy" in
[docs/operations.md](docs/operations.md).

The rest — event thresholds, retries, log retention — is in
[.env.example](.env.example) and [docs/operations.md](docs/operations.md).

---

## How it works

Three sources, each answering a different question:

* **the team page** — the schedule: what is coming, when, against whom;
* **the match page** — the state of a match: is it live, what is the score by
  maps, what is the format;
* **HLTV's live feed** (`scorebot-lb.hltv.org`, Engine.IO) — the round-by-round
  detail while a map is being played: the score, the round state, the kills.

Polling adapts: half-hourly when nothing is happening, every three minutes
before a start, and while a match is live the feed does the work, so the page is
polled rarely.

**Notifications are born only on state transitions**, never on "what the page
says right now". Every notification carries an idempotency key that includes its
recipient, is written to a journal in the same transaction as the outgoing
message, and is refused if that key was already sent. That is what makes a
restart, a reconnect or a duplicate reading of the same page harmless — and it
is why the database matters more than it looks.

The service is a polite client: **one request every 30 seconds, hardcoded**, no
parallel connections, no proxy rotation. A `403` is treated as "back off", not
as something to retry through. The live feed's held connection is not frequent
polling and does not count against that budget.

The details — and the reasoning behind the parts that look over-engineered
until you have watched a real match go by — are in
[docs/architecture.md](docs/architecture.md).

---

## Data and backups

Everything lives in one file: `data/hltv.db`. It holds the state **and the
journal of sent notifications**.

Losing it is not fatal — the first run on an empty database is silent — but you
lose the history and the per-map scores. Keep it out of harm's way, and do not
delete the volume when you update.

```bash
docker compose stop && tar czf hltv-backup-$(date +%F).tar.gz data/ && docker compose start
```

The database runs in WAL mode, so `-wal` and `-shm` files sit next to it — copy
the whole directory, not just `hltv.db`.

---

## Updating

```bash
docker compose pull && docker compose up -d
```

If you build from source, pull the sources instead:

```bash
git pull && docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

The schema migrates itself on startup: new columns are added, old ones are
never dropped, so rolling back to an earlier image does not break the data.

---

## When something goes wrong

Ask the bot `/status` first — it reports the health of each source, the live
feed and the queue. The service also tells you itself when it goes blind (E8)
and when it recovers (E8R).

| Symptom | Likely cause |
|---|---|
| the bot does not answer at all | your `chat_id` is not in `TELEGRAM_CHAT_ID`; the log prints the id it refused |
| no notifications, but the log looks fine | `DRY_RUN` is still `true`, or you are `/pause`d |
| `403` on HLTV pages | you are being filtered or rate-limited; the service backs off on its own |
| everything times out | a proxy is set and unreachable — check `NO_PROXY` too |
| "the parser returned zero matches" | HLTV changed its markup; this is reported as a source failure, not as "no matches" |

Each of these is expanded in [docs/operations.md](docs/operations.md), which
also covers the healthcheck, the degradation alarm and revoking someone's
access.

---

## Development

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt pytest
```

```bash
python -m pytest
```

The tests run against **fixtures of real pages and recordings of the live
feed** in `docs/recon/fixtures`, never against the live site: matches are rare,
and an overtime, a dropped connection or a multikill cannot be reproduced on
demand.

You can replay a recorded match through the state machine and watch the
notifications it produces:

```bash
PYTHONPATH=src python -m hltv_notify.replay docs/recon/fixtures/scorebot-2397053-forze.jsonl.gz --team-id 12857 --match-id 2397053 --twice
```

`--twice` replays the same dump a second time; nothing should be emitted on the
second pass, which is the idempotency guarantee under test.

New fixtures are collected with `scripts/fetch_fixtures.py` and
`scripts/record_scorebot.py` — see [CLAUDE.md](CLAUDE.md).

---

## What it deliberately does not do

Other games and other sites. A web interface, dashboards or metrics endpoints.
Faster polling to "keep up" — if we are not keeping up, that is an argument for
leaning on the live feed, not for hammering HLTV.

There are also real limitations worth knowing before you rely on it: a match
that HLTV recreates under a new id looks like a cancellation plus a new match;
per-round detail exists only while a match is live, so an outage during a map
cannot be recovered afterwards; a comeback line can come out understated if the
service restarts mid-map. The full list, with the reasoning, is in
[docs/known-limitations.md](docs/known-limitations.md).

---

## Documentation

| Document | About |
|---|---|
| [docs/architecture.md](docs/architecture.md) | how it works and why it is built this way |
| [docs/operations.md](docs/operations.md) | running it: polling, health, failures, access |
| [docs/deployment.md](docs/deployment.md) | deployment, CI, publishing and signing the image |
| [docs/known-limitations.md](docs/known-limitations.md) | what is deliberately not handled |
| [docs/recon/](docs/recon/) | source recon: what HLTV serves, and how it behaves |
| [CLAUDE.md](CLAUDE.md) | working rules and the traps found the hard way |

---

## License

[MIT](LICENSE). This is an unofficial personal project, not affiliated with or
endorsed by HLTV. It reads the same public pages a browser does, at a
deliberately low rate; if you fork it, please keep it that way.
