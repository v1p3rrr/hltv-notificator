# hltv-notificator

Telegram notifications about CS2 team matches on HLTV — at a granularity the
site's own subscriptions do not offer.

HLTV's built-in subscriptions can do "the match started" and "the match
finished". This service does the one thing missing there: **the end of every
map, with the score**, and at the moment of the winning round rather than when
HLTV updates its maps section (which lags — see
[docs/known-limitations.md](docs/known-limitations.md)).

One process, one user, no web interface: the interface is a chat with the bot.

## What arrives in the chat

| | Event | Source |
|---|---|---|
| E10 | Reminder N minutes before the match | schedule |
| E1 | A new match in the schedule | team page |
| E2 | The match time changed | team page |
| E3 | The match was cancelled or postponed | team page |
| E4 | The match started, the map lineup and whose pick each is | match page |
| E5 | A map started | live feed |
| E11 | A map point: somebody is one round from taking the map | live feed |
| E6 | **A map finished, with the score** | live feed (instantly), the page confirms |
| E7 | The match finished, the result by maps | live feed by the map count, the page confirms |
| E8 | The service has gone blind: the source is silent, the feed will not come up, the queue is not draining | watchdog |
| E8R | Recovered | watchdog |
| E9 | A player of a tracked team took 4+ kills in a round | live feed |

Plus a **live message per map** — one message, updated with the current score
as the game goes on and frozen on the final one. Where the live message is on,
it is also the map's card: it carries the map start itself, so E5 does not
arrive separately. Two messages about one thing would have been in the wrong
order anyway — the live message goes straight to Telegram while events wait in
the queue.

```
✅ Map 2 finished
Dust2 — 13:10
FORZE Reload — Color
Series score: 2:0
GLuck Moscow Cyber Games 2026 Closed Qualifier
```

## Quick start

```bash
cp .env.example .env
```

Fill in `TELEGRAM_BOT_TOKEN` (from [@BotFather](https://t.me/BotFather)) and
`TELEGRAM_CHAT_ID` (from [@userinfobot](https://t.me/userinfobot)). Leave
`DRY_RUN=true` — the first run should send nothing.

```bash
docker compose up -d --build
docker compose logs -f
```

Once the logs look sensible, set `DRY_RUN=false` and restart.

Details in [docs/deployment.md](docs/deployment.md) and
[docs/operations.md](docs/operations.md).

## Buttons or commands

Everything is available both as buttons and as text. `/menu` (as well as
`/start` and `/help`) opens the inline menu: status, the running match, what is
coming up, the team list, reminders and the quiet switch. In a team's menu every
notification type toggles with a tap — no need to remember event codes or team
ids.

The text commands have not gone anywhere: they are shorter when you know exactly
what you want, and convenient to send from a script.

## Bot commands

| Command | What it does |
|---|---|
| `/teams` | which teams you follow |
| `/track <link>` | start following a team |
| `/untrack <id>` | stop following |
| `/mute <id> <E5,E9>` | mute event types for a team |
| `/unmute <id>` | clear the mutes |
| `/remind 15m` | remind 15 minutes before, `/remind rm 15m` removes it |
| `/tz Europe/Berlin` | your timezone |
| `/pause`, `/resume` | go quiet / start sending again |
| `/whoami` | your `chat_id` |
| `/status` | the health of the service, the sources and the live feed |
| `/live` | what is happening in a running match and which source the data came from |
| `/next` | upcoming matches as the service sees them |
| `/check` | poll the schedule immediately |
| `/verbose on\|off` | verbose logging |

## Who receives notifications

The service serves **several Telegram accounts**: each with its own team list
and its own mutes. Allowed accounts are listed in a single variable, separated
by commas (`TELEGRAM_CHAT_ID=123456789,987654321`); the first one is the main
chat. By default (`TELEGRAM_WHITELIST_ONLY=true`) the bot answers only them and
stays silent to everyone else.

A person learns their own `chat_id` with `/whoami` — it answers everyone, so
there is something to put on the whitelist. [@userinfobot](https://t.me/userinfobot)
works too. For groups the id is negative, which is normal.

## Tracked teams

The list lives in the database and is edited **through the bot**: `/track`,
`/untrack`, `/teams`. `TEAM_ID` from `.env` is used only for the first seed,
when the list is empty.

Filtering goes strictly by numeric id, not by name: team names change, repeat
themselves and pick up `ex-` prefixes. That is why `/track` takes a link to the
team page rather than a name.

If tracked teams play **each other**, the match stays one match: the
notification arrives once per subscriber, with the score turned around to face
whichever team that particular person follows. The exception is multikills: a 4k
by a player of either team is its own highlight and goes to those following that
player's team. The reasoning is in [docs/architecture.md](docs/architecture.md).

## Documentation

| Document | About |
|---|---|
| [docs/architecture.md](docs/architecture.md) | how it works and why it is built this way |
| [docs/deployment.md](docs/deployment.md) | deployment, CI, publishing the image |
| [docs/operations.md](docs/operations.md) | operations, polling rates, what to do on failures |
| [docs/known-limitations.md](docs/known-limitations.md) | what is deliberately not handled |
| [docs/recon/](docs/recon/) | source recon: what HLTV serves data through, and how |
| [CLAUDE.md](CLAUDE.md) | context and rules for further work |

## Installing as a package

Besides Docker, the project installs as an ordinary package, straight from the
repository:

```bash
pip install git+https://github.com/v1p3rrr/hltv-notificator
hltv-notify
```

It is not published to PyPI: the image is the main distribution channel.

## Development

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt pytest
```

```bash
python -m pytest
```

The tests run against **fixtures of real pages and recordings of the live feed**
from `docs/recon/fixtures`. A live source is no good for tests: matches are
rare, and an overtime, a dropped connection or a multikill cannot be reproduced
on demand.

Replaying a recorded match through the state machine:

```bash
PYTHONPATH=src python -m hltv_notify.replay docs/recon/fixtures/scorebot-2397053-forze.jsonl.gz --team-id 12857 --match-id 2397053 --twice
```
