# R3 — The schedule source

Date of observation: 2026-08-29. **Variant B (HTML)** was chosen; recon of
the mobile endpoint, which needs the app's traffic proxied through mitmproxy,
was deliberately deferred. The mobile API remains a candidate for a second
source should the HTML turn out to be brittle.

## Access: the TLS fingerprint is confirmed as a real barrier

Measured over four pages, the same URL, back to back:

| Page | `urllib` (an ordinary client) | `curl_cffi` (`impersonate="chrome"`) |
|---|---|---|
| `/team/12857/forze-reload` | **403** | **200** (1,149,545 bytes) |
| `/matches/2397053/...` (live) | **403** | **200** (547,715 bytes) |
| `/matches/2397340/...` (upcoming) | **403** | **200** (482,958 bytes) |
| `/matches/2397047/...` (finished) | **403** | **200** (475,727 bytes) |

The headers in the control request looked correct — they are not the issue.
Permuting `User-Agent` and other headers is useless, the filtering is by the
client's TLS fingerprint. Reproducible with `scripts/fetch_fixtures.py`.

**No Cloudflare challenges or captchas were observed** — with the right
fingerprint the page is served straight away. Captcha solvers and proxy rotation
are neither needed nor used.

## Home-route TLS-group compatibility, 2026-11-01

The production page client used curl_cffi 0.16.2, `impersonate="chrome"`
(resolved by the library to `chrome150`), and the existing HTTP proxy. Proxy
logs showed HLTV taking the direct outbound route. Of 214 production team-page
GETs since 2026-10-31T00:00:00Z, 208 timed out and six returned HTTP 200.

Separate probes of the real HLTV team page timed out with the default profile.
Keeping that profile but constructing `AsyncSession` with
`curl_options={CurlOpt.SSL_EC_CURVES: b"X25519:P-256:P-384"}` returned HTTP 200
in 0.35 seconds (1,252,480 bytes). Certificate verification stayed enabled.
The same override worked on a control host where the baseline timed out.
`chrome120` reached the control host but returned 403 on HLTV, so a profile
downgrade is not a verified substitute.

The permanent `HltvHttp` change was also exercised from the development host
through the same HTTP proxy: the team page was fetched and parsed in 0.61
seconds, 1,250,808 UTF-8 bytes, with 16 matches and the team name FORZE Reload.
No upcoming matches were listed at that observation. Tests continue to use
fixtures or an intercepted transport rather than the live source.

These probes establish a dependency on the offered TLS groups on this route.
They do not distinguish DPI from fragmentation/MTU or another path issue,
and they do not establish prolonged stability. Proxy rules, certificate
verification and the running service were not changed by the probes.

## The team page — the schedule

`https://www.hltv.org/team/12857/forze-reload` is server-rendered, everything
needed is in the HTML (not built by JS). The structure:

- two `table.match-table` tables — "Upcoming matches for …" and "Recent results
  for …" (tell them apart by the nearest preceding `.standard-headline`);
- match rows are `tr.team-row`; tournament separator rows are
  `tr.event-header-cell`;
- the time is the **`data-unix` attribute, epoch in milliseconds** (not the
  text!), so timezones and DST are resolved without parsing human-readable
  dates;
- the match link is `a[href*="/matches/"]`, the id is extracted with the regex
  `/matches/(\d+)/`.

The verified sample (fixture `fixtures/team-12857-forze-reload.html`):

```
-- Upcoming matches for FORZE Reload
   unix=1787994300000 id=2397053 teams=['FORZE Reload', 'Color']     score=-:-  event=GLuck Moscow Cyber Games 2026 Closed Qual…
   unix=1788015600000 id=2397340 teams=['FORZE Reload', 'ex-RUSTEC'] score=-:-  event=Kibertochka Season 2
-- Recent results for FORZE Reload
   unix=1787943000000 id=2397337 teams=['FORZE Reload', 'DONSTU']    score=2:0  event=Kibertochka Season 2
   unix=1787412600000 id=2397026 teams=['FORZE Reload', 'Nemiga']    score=0:2  event=GLuck Moscow Cyber Games 2026 Closed Qual…
```

Two useful properties confirmed on the sample: **our own team always comes
first** in the row regardless of whether it is team1 or team2 on the match page,
and **the score is given from its point of view** (`0:2` in the match lost to
Nemiga). Those can be relied on, but the parser still matches the opponent by
id.

The depth of "Recent results" is noticeably more than two weeks (matches from
late February appear in the sample); "Upcoming" shows the nearest ones. That is
enough for E1-E3.

## The match page — state, format, per-map scores

Fixtures: `match-2397340-upcoming.html`, `match-2397053-live.html`,
`match-2397047-finished.html`.

| What | Selector | Values across the three fixtures |
|---|---|---|
| Start time | **`.timeAndEvent [data-unix]`** | 1788015600000 / 1787994300000 / 1787856300000 |
| State | `.countdown` | `4h : 57m : 28s` / `LIVE` / `Match over` |
| Tournament | `.timeAndEvent .event a` | the name + `/events/<id>/<slug>` |
| Format | `.preformatted-text` | `Best of 3 (LAN)` / `Best of 3 (Online)` + notes |
| Maps | `.mapholder` → `.mapname`, `.results-team-score`, `.results-center-half-score` | see below |
| Series score | `.team1-gradient .won` / `.team2-gradient .won` | on the finished one — `2` |
| Live feed | `#scoreboardElement[data-scorebot-id]` | **only on the live page** |

The maps across the fixtures:

```
upcoming : map1 TBA    scores=[]          map2 TBA    …   (TBA before the veto)
live     : map1 Mirage scores=['13','10'] halves=( 5 : 7 ; 8 : 3 )
           map2 Dust2  scores=['-','-']   map3 Ancient scores=['-','-']
finished : map1 Mirage scores=['13','10'] halves=( 8 : 4 ; 5 : 6 )
           map2 Dust2  scores=['13','10'] halves=( 9 : 3 ; 4 : 7 )
           map3 Nuke   scores=['-','-']            (the decider was not played)
```

Hence the first version of the map-completion rule: **a `.mapholder` gained a
numeric score
instead of a dash ⇒ the map is played**. It does not depend on round arithmetic,
so it survives an overtime, while an unplayed decider naturally stays with a
dash and no E6 is born for it.

> This rule was later refined: a **running** map has a numeric score too, so the
> `.results-stats` link had to be added as the actual completion signal. See
> [known-limitations.md](../known-limitations.md) and the "Trap two" section in
> [architecture.md](../architecture.md).

## A trap that is easy to get burned by

An `[data-unix]` selector **without a scope** picks up the wrong thing. On a
match page the first thing in the DOM is the "featured matches" widget
`.fbw-vp-header-time` carrying **other** matches' times (on the finished-match
fixture there are two of them: 11:00 and 18:30). An unqualified selector would
have given 1787994000000 instead of the correct 1787856300000 — that is, a
foreign match's time, and the service would have sent false "time changed" E2
events.

**`.timeAndEvent [data-unix]` is mandatory.** There is a fixture-based test for
it.

## Brittleness and the plan B

The HTML path breaks on a markup redesign. The sign of a breakage is the parser
returning zero matches on an HTTP 200; that is treated as a source failure and
goes to E8 rather than to "there are no matches". If it starts breaking often,
we return to the question of the mobile JSON endpoint (variant A, which requires
proxying the app's traffic).
