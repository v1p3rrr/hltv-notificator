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
Dust2 — 13:11 (overtime)
FORZE Reload — Color
Series score: 2:0
🔥 Comeback: FORZE Reload turned 3:11 around — a swing of 10 rounds
GLuck Moscow Cyber Games 2026 Closed Qualifier
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

Every notification type can be muted individually, per team, from the bot.

| | Notification | Where it comes from |
|---|---|---|
| **E10** | Reminder, N minutes before the match | schedule |
| **E1** | A new match appeared in the team's schedule | team page |
| **E2** | The match time changed | team page |
| **E3** | The match was cancelled or removed | team page |
| **E4** | The match started — with the map lineup and whose pick each map is | match page |
| **E5** | A map started | live feed |
| **E11** | **Map point** — someone is one round from taking the map | live feed |
| **E12** | Half time, and the start of every overtime *(off by default)* | live feed |
| **E6** | **A map finished, with the score** — plus a comeback line when there was one | live feed, confirmed by the page |
| **E7** | The match finished, with the result map by map | live feed, confirmed by the page |
| **E9** | A player of a followed team took 4+ kills in one round | live feed |
| **E8 / E8R** | The service has gone blind / has recovered | internal watchdog |

The `E`-codes are internal shorthand; you never have to type them, and the
bot's menu shows plain names.

### The live map card

While a map is being played you also get **one message per map**, edited in
place with the current score and frozen on the final one:

```
🗺 Map 1: Nuke
FORZE Reload 7:5 Color · round 13 · freeze time
Series score: 0:0
```

That card *is* the map's announcement — it carries "the map started", which is
why E5 does not arrive as a separate message when the card is enabled. Turn it
off with `LIVE_MESSAGE=false` if you would rather have a quiet chat.

### A few more examples

```
🆕 New match
FORZE Reload — ex-RUSTEC
Thunderpick World Championship 2026
🕒 Sat 6 Sep, 18:00
Match page
```

```
🔴 Match started
FORZE Reload — Color
GLuck Moscow Cyber Games 2026 · BO3
Nuke      our pick
Dust2     their pick
Inferno   decider
Match page
```

```
🚨 Map point — Color
Inferno — 11:12
FORZE Reload — Color
One round from taking the map — and the match
```

```
💥 sh1ro — ACE
Mirage, round 14 · score 8:6
FORZE Reload — Color
```

Times are shown in your own timezone (`/tz`); everything is stored in UTC.

---

## Requirements

**With Docker (recommended):** Docker with the Compose plugin
(`docker compose version`), and a machine that stays on — a mini-server, a NAS,
a VPS, a Raspberry Pi. A few megabytes of disk.

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

```bash
git clone https://github.com/v1p3rrr/hltv-notificator.git
```

```bash
cd hltv-notificator && cp .env.example .env
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
first run, or leave the defaults and add teams later from the chat. All three
come straight out of a team's URL:
`https://www.hltv.org/team/`**`12857`**`/`**`forze-reload`**.

Leave the `IMAGE=` line alone. It only matters if you pull a prebuilt image;
its value is ignored when you build locally, but it must be present.

### 5. Start it

Build from the sources you just cloned:

```bash
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
```

Watch it come up:

```bash
docker compose logs -f
```

You should see something like:

```
first tracked team taken from the config: FORZE Reload (id 12857)
DRY_RUN is on: notifications go to the log, not to Telegram
service started: 1 subscriber(s), 1 team(s) watched (FORZE Reload)
GET https://www.hltv.org/team/12857/forze-reload -> 200 in 0.28s
team 12857 taken under observation: 17 matches recorded silently
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
docker compose -f compose.yaml -f compose.dev.yaml up -d
```

### 8. Set it up to taste, from the chat

```
/track https://www.hltv.org/team/12857/forze-reload
/tz Europe/Berlin
/remind 15m
```

If you publish your own image to a registry, see
[docs/deployment.md](docs/deployment.md) — then the server needs only
`compose.yaml` and `.env`, no sources at all.

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

Everything is available **both as buttons and as text commands**. `/menu` (also
`/start` and `/help`) opens an inline menu: status, the running match, what is
coming up, your teams, reminders and the quiet switch. Inside a team's menu
every notification type toggles with a tap — no codes, no ids to remember.

The text commands are shorter when you know exactly what you want:

| Command | What it does |
|---|---|
| `/menu`, `/start`, `/help` | the menu / the command list |
| `/teams` | which teams you follow, and what is muted for each |
| `/track <team link>` | start following a team |
| `/untrack <id>` | stop following it (history is kept) |
| `/mute <id> <E5,E9>` | mute notification types for one team |
| `/unmute <id>` | clear that team's mutes |
| `/remind 15m` | remind 15 minutes before a match; `/remind rm 15m` removes it |
| `/tz Europe/Berlin` | your timezone |
| `/pause`, `/resume` | go completely quiet / start receiving again |
| `/next` | upcoming matches as the service sees them |
| `/live` | what is happening in a running match, and which source that came from |
| `/status` | health of the service, the sources and the live feed |
| `/check` | poll the schedule right now |
| `/whoami` | your `chat_id` |
| `/verbose on`, `/verbose off` | verbose logging |

`/pause` silences everything, including the live card and the service's own
alarms. Nothing is queued while you are paused — the point is silence, not
deferred delivery.

---

## Following teams

The list of teams lives in the database and is edited **through the bot**.
`TEAM_ID` in `.env` is used only for the very first seed, when the list is
still empty.

```
/track https://www.hltv.org/team/12857/forze-reload
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
else, logging their `chat_id` so you have something to add to the list. Anyone
can ask the bot `/whoami` and get their own id back. Turn the whitelist off
only deliberately: a Telegram bot has a public address, and without it anyone
who finds yours can command it.

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
git pull && docker compose -f compose.yaml -f compose.dev.yaml up -d --build
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
