# R2 — The team identifier

Date of observation: 2026-08-29. A worked example on one team; the conclusion
it reaches is general — teams are identified by number, never by name.

| | |
|---|---|
| HLTV internal id | **12857** |
| Canonical name | `FORZE Reload` |
| Page URL | https://www.hltv.org/team/12857/forze-reload |
| Country | Russia |

## Why we filter by id and not by name

Derived and identically named teams turn up on HLTV regularly: in this
organisation's match history `FORZE Reload` sits alongside `forZe` on the scene
(a different, main team with a different id). On top of that there is the
site-wide practice of an `ex-` prefix when a roster breaks up (`ex-RUSTEC`,
`ex-RUBY`, `ex-Zero Tenacity` all appeared in the schedule on the day of the
recon). A team's name can change; its id cannot.

**Conclusion: every filter in the service is built on the numeric `team_id`.**
The name is kept only for rendering messages and is refreshed from the latest
observation. It is also why `/track` takes a link to a team page rather than a
name: the id is in the link.

## Tournament tier

By "Recent results" on the day of the recon the team plays in qualifiers and
regional leagues: GLuck Moscow Cyber Games 2026 Closed Qualifier, Kibertochka
Season 2, CCT 2026 Contenders Europe Series 8, Exort Fiesta Series 1 Closed
Qualifier, European Pro League Series 5, Exort Meteor Season 2.

That is the level at which the live feed was most in doubt — it might well
have existed only at top-tier events. Checked separately — see
[R1](R1-live-data-availability.md): the feed is there.
