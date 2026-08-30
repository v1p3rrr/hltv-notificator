# R2 — The team identifier

Date of observation: 2026-08-29.

| | |
|---|---|
| HLTV internal id | **12857** |
| Canonical name | `FORZE Reload` |
| Page URL | https://www.hltv.org/team/12857/forze-reload |
| Country | Russia |
| World ranking | #120 |
| Valve ranking (Beta) | #149 |
| Coach | Pavel 'PASHANOJ' Legostaev |
| Average roster age | 24.1 |

## Why we filter by id and not by name

Derived and identically named teams turn up on HLTV regularly: in this
organisation's match history `FORZE Reload` sits alongside `forZe` on the scene
(a different, main team with a different id). On top of that there is the
site-wide practice of an `ex-` prefix when a roster breaks up (`ex-RUSTEC`,
`ex-RUBY`, `ex-Zero Tenacity` all appeared in the schedule on the day of the
recon). A team's name can change; its id cannot.

**Conclusion: every filter in the service is built on `team_id == 12857`.** The
name is kept only for rendering messages and is refreshed from the latest
observation.

## Tournament tier

By "Recent results" on the day of the recon the team plays in qualifiers and
regional leagues: GLuck Moscow Cyber Games 2026 Closed Qualifier, Kibertochka
Season 2, CCT 2026 Contenders Europe Series 8, Exort Fiesta Series 1 Closed
Qualifier, European Pro League Series 5, Exort Meteor Season 2.

This is exactly the level the spec warned about: "at lower-tier tournaments
there may be no feed at all". Checked separately — see
[R1](R1-live-data-availability.md), the feed is there.
