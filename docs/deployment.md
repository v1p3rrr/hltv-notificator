# Deployment

The service is meant for a personal mini-server: one process, one user, no
inbound ports and no web interface. It only reaches out — to HLTV and to
Telegram.

## What the server needs

* Docker with the compose plugin (`docker compose version`);
* outbound access to `hltv.org`, `scorebot-lb.hltv.org` and `api.telegram.org` —
  directly or through a proxy (`HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`/
  `NO_PROXY`, see "Proxy" in [operations.md](operations.md));
* a directory for the data — a few megabytes.

No inbound ports need opening: the bot works through `getUpdates` long polling,
not through a webhook.

## The first run on the server

The sources are not needed on the server: the image comes from the registry and
only two files are needed from the repository.

**1. Directory and files.**

```bash
mkdir -p ~/hltv-notify && cd ~/hltv-notify
curl -O https://raw.githubusercontent.com/v1p3rrr/hltv-notificator/main/compose.yaml
curl -o .env https://raw.githubusercontent.com/v1p3rrr/hltv-notificator/main/.env.example
```

**2. The bot and the chat.** Create a bot with
[@BotFather](https://t.me/BotFather) and take the token. Find your `chat_id`
with [@userinfobot](https://t.me/userinfobot) — or ask the bot itself later with
`/whoami`. **Send the bot a `/start`**, otherwise it cannot write to you first.

**3. Fill in `.env`.** The required minimum is three lines:

```
IMAGE=your-docker-hub-login/hltv-notificator:latest
TELEGRAM_BOT_TOKEN=123456:AA...
TELEGRAM_CHAT_ID=123456789
```

There can be several accounts — then list the ids separated by commas,
`TELEGRAM_CHAT_ID=123456789,987654321`. The first one is the main chat: the team
from `TEAM_ID` is seeded there. The separate variable `TELEGRAM_ALLOWED_CHATS`
no longer exists: if it is still in an old `.env`, move its ids here.

`IMAGE` depends on the Docker Hub login CI publishes under. The build itself
prints the exact name: in GitHub Actions open the latest run, the "Published"
section — all the tags and the digest are listed there.

Leave `DRY_RUN` as `true` for now.

**4. If the Docker Hub repository is private** — log in once:

```bash
docker login
```

**5. Start it.**

```bash
docker compose pull
docker compose up -d
docker compose logs -f
```

The logs should show roughly this:

```
first tracked team taken from the config: FORZE Reload (id 12857)
DRY_RUN is on: notifications go to the log, not to Telegram
service started: 1 subscriber(s), 1 team(s) watched (FORZE Reload)
GET https://www.hltv.org/team/12857/forze-reload -> 200 in 0.28s
team 12857 taken under observation: 17 matches recorded silently
```

**The first run is always silent**: the schedule is written into the database
without notifications, otherwise a dozen and a half messages about already
played matches would arrive at startup.

**6. Check the connection.** Send the bot `/menu` — a menu with buttons should
arrive.

**7. Live mode.** Once the logs look sensible, set `DRY_RUN=false` in `.env` and
restart:

```bash
docker compose up -d
```

**8. Configure it to taste** — from the chat now:

```
/track https://www.hltv.org/team/12857/forze-reload
/remind 15m
/tz Europe/Moscow
```

`TEAM_ID` in `.env` is only needed for the first seed, when the list is empty;
after that the teams live in the database.

## Building locally instead of pulling

On a machine with the sources you can build the image yourself — there is a
separate overlay for that, so that `docker compose up` on the server never tries
to build:

```bash
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
```

## Updating

```bash
docker compose pull
docker compose up -d
```

The data volume is left alone in the process — that matters, see below.

To pin a specific version instead of `latest`, use the `IMAGE` variable in
`.env`:

```
IMAGE=your-login/hltv-notificator:0.1.0-build.42
```

To build from source, see "Building locally instead of pulling" above.

## Data and backups

The `./data` directory contains `hltv.db` — both the state and the **journal of
sent notifications**.

**It must not be lost.** Without the journal the service will, on its next
start, treat everything it finds as new and send the notifications again. The
first run on an empty database is silent, so it will not be a catastrophe, but
the history and the per-map scores will be gone.

```bash
# a backup with the service stopped
docker compose stop
tar czf hltv-backup-$(date +%F).tar.gz data/
docker compose start
```

The database is in WAL mode, so `-wal` and `-shm` files sit next to `hltv.db`.
Copy the whole directory.

## Publishing the image through CI

The workflow `.github/workflows/ci.yml` builds and publishes the image on every
push to `main`. The separation of privileges is carried through: the image is
built in a job **without the token** and travels onward as an archive, while the
job that has the token **does not check the repository out at all** — it unpacks
the ready archive and pushes the layers to the registry.

What to set up in the repository settings so publishing works:

| What | Where | Value |
|---|---|---|
| `DOCKERHUB_USERNAME` | Settings → Secrets and variables → Actions → **Variables** | the Docker Hub login |
| `DOCKERHUB_TOKEN` | the same place → **Secrets**, inside the `dockerhub` environment | an Access Token with Read/Write |
| `DOCKERHUB_IMAGE` | **Variables**, optional | if publishing into an organisation |
| the `dockerhub` environment | Settings → **Environments** | where the secret goes; branches are restricted there too |

The token must be an Access Token, not a password, and with Read/Write rather
than Admin.

Until the variables are set, the `tests` and `image` jobs pass while `publish`
fails with the explicit message "The variable DOCKERHUB_USERNAME is not set".
That is a built-in check, not a breakage.

### Image tags

| Event | Tags |
|---|---|
| push to `main` | `<version>-build.<run number>`, `sha-<commit>`, `latest` |
| tag `vX.Y.Z` | `X.Y.Z`, `X.Y` |

The version lives in `src/hltv_notify/__init__.py`. When a tag is released, CI
checks it against that version and fails on a mismatch: otherwise an image
numbered 1.2.3 would report somebody else's number.

The image is signed with keyless `cosign` — the certificate is issued by GitHub
itself for the duration of the step. Verifying the signature:

```bash
cosign verify <image>@<digest> --certificate-identity-regexp '.*' --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

## Rolling back

```bash
IMAGE=your-login/hltv-notificator:sha-<previous commit> docker compose up -d
```

The database schema is backwards compatible: new columns are added by a
migration at startup, old ones are never dropped. Rolling back to a previous
image does not break the data.

## Running without Docker

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=src .venv/bin/python -m hltv_notify
```

Python 3.10+ is required (3.12 in the image). A systemd unit, if you want one,
is an ordinary `Type=simple` with `Restart=always`, `EnvironmentFile=` and the
project's working directory; there is no such file in the repository because
Docker was the chosen route.
