# Source recon

What HLTV actually serves, measured rather than assumed. These are dated field
notes from **August 2026**, kept because most of the service's design decisions
only make sense next to the observation that forced them.

They are a **snapshot, not a contract.** HLTV can change its markup and its
endpoints at any time; when something here stops matching reality, the thing to
do is re-measure and update the note, not to argue with it.

Read them if you are changing a parser, chasing a source failure, or wondering
why some part of the code looks more complicated than the problem. To simply
run the service you need none of this — [../../README.md](../../README.md) and
[../operations.md](../operations.md) are enough.

| Note | What it establishes |
|---|---|
| [R1](R1-live-data-availability.md) | whether a live feed exists at all at the tournament tiers we care about, and what it does *not* say (it never reports "the map is over") |
| [R2](R2-team.md) | why teams are identified by numeric id and never by name |
| [R3](R3-schedule-source.md) | where the schedule comes from: the team page's markup, the `data-unix` attribute, the match page's selectors — and the proof that ordinary HTTP clients get a 403 while `curl_cffi` gets a 200 |
| [R4](R4-scorebot.md) | the live feed's protocol in full: Engine.IO v3 over polling, the closed websocket, the two silent-failure traps, the frame format |
| [R5](R5-limits.md) | what `robots.txt` disallows, and the polling rates chosen to stay indistinguishable from a person with a tab open |

## fixtures/

Real pages and real recordings of the live feed — the basis of the test suite.
Tests never touch the live site: matches are rare, and an overtime, a dropped
connection or a multikill cannot be reproduced on demand.

| File | What it is |
|---|---|
| `team-*.html` | a team page — the schedule |
| `match-*-upcoming.html`, `match-*-live.html`, `match-*-finished.html` | a match page in each of its three states |
| `scorebot-*.jsonl.gz` | a recorded live feed, including a **map boundary** — consecutive identical frames are collapsed and disconnects are marked, which is what the deduplication tests are built on |

Refreshed with `scripts/fetch_fixtures.py` (pages) and
`scripts/record_scorebot.py` (feed). The valuable recordings are the moments
that cannot be staged: a map boundary, an overtime, a dropped connection, a
multikill.
