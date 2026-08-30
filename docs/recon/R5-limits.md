# R5 — Source restrictions

Date of observation: 2026-08-29. Source: https://www.hltv.org/robots.txt

This is not a legal analysis but a record of what was observed, so that
decisions are made deliberately.

## robots.txt — what is disallowed (extract of the relevant sections)

```
User-agent: *
Disallow: /matches?*
Disallow: /results?*startDate*   (and ~8 more /results?* filters)
Disallow: /stats?*  /stats*?*    (a broad set of filters)
Disallow: /forums/*
Disallow: /blog/*
Disallow: /download/demo*
Disallow: /legacystats/*
Disallow: /fantasy/*/league/*?*
Disallow: /transfers?*
Disallow: /events/archive  /events/archive?*
Disallow: /search/?*offset*  /search/?*variant*
Disallow: /valve-ranking/teams/details/*
Disallow: /skins/*?<filters>
Allow: /ads.txt
Sitemap: https://www.hltv.org/sitemap_index.xml
```

## What that means for the service

| Page | Status | Do we use it? |
|---|---|---|
| `/team/12857/forze-reload` | not disallowed | **yes** — the primary schedule source |
| `/matches/<id>/<slug>` | not disallowed | **yes** — match state, per-map scores |
| `/matches` (no query) | not disallowed | no need |
| `/matches?...` (filters, including by team) | **disallowed** | **no** |
| `/results?...` (filters) | **disallowed** | no |
| `/stats...` (filters) | **disallowed** | no |

The tempting `/matches?team=12857` gives a ready-made selection — and it is
disallowed. So the schedule is taken from the team page, where exactly the same
data sits in the allowed area.

robots.txt on the site says nothing about `scorebot-lb.hltv.org`; that is a
separate host and no robots.txt of its own was observed for it. It serves the
same widget that works for an ordinary visitor with a tab open.

## Recommended polling rates

The goal is to be indistinguishable from a user keeping a tab open.

| Mode | Interval |
|---|---|
| Background (no matches within the next 30 min) | 30 min |
| Pre-match (30 min before the start) | 3 min |
| Active (a match is running, no live feed) | 60 s |
| Active with scorebot working | 5 min |

The ceiling hardcoded in the code: **no more often than 1 request every 30
seconds** globally. Requests are strictly sequential (one in flight), jitter is
±20%, there are no parallel pools and no proxy rotation. One websocket
connection per active match.

A day with no matches is roughly 48 requests. A day with a single BO3 is roughly
150.
