# R1 — Is there live data for FORZE Reload's matches

Date of observation: 2026-08-29. Method: connecting to scorebot with the
protocol from [R4](R4-scorebot.md) plus parsing match pages.

## Matches checked

| Match | Tournament | Tier | Live scoreboard | Per-round history |
|---|---|---|---|---|
| `2396935` Vitality–9z | BLAST Open Porto 2026 | LAN, top | **yes** | **yes** |
| `2397229` ex-RUSTEC–Entropy | CCT 2026 Europe Series 8 Closed Qualifier | online qualifier | **yes** | **yes** |
| `2397053` Color–**FORZE Reload** | GLuck Moscow Cyber Games 2026 Closed Qualifier | online qualifier | **yes** | **yes** |
| `2397047` FORZE Reload–Black Phoenix (finished) | GLuck Moscow Cyber Games 2026 Closed Qualifier | online qualifier | n/a | per-map and per-half scores on the page |

The key check is the third row: that is a match of **the tracked team itself**,
at a qualifier, i.e. the riskiest scenario according to the spec. The feed is
complete.

An illustrative fragment of state captured from FORZE's live match (the moment
the first map ended):

```json
{ "mapName": "de_mirage", "currentRound": 23, "currentRoundState": "ended",
  "live": true, "frozen": true,
  "ctTeamId": 12857, "ctTeamName": "FORZE Reload", "ctTeamScore": 10,
  "tTeamId": 13973,  "tTeamName": "Color",         "tTeamScore": 13,
  "regulationHalfLength": 12, "overtimeHalfLength": 3,
  "ctMatchHistory": { "firstHalf": [12 rounds], "secondHalf": [11 rounds] } }
```

In the ex-RUSTEC–Entropy match the per-round history arrived expanded, with
outcome types: `Target_Bombed`, `Target_Saved`, `CTs_Win`, `Terrorists_Win`,
`lost`, each with `roundOrdinal` and `survivingPlayers`.

## Conclusions

- **E5 and E6 are feasible.** The live feed is available at the tier FORZE
  Reload plays at, not only at top LANs.
- **E6 should nonetheless not be taken from the feed directly** — the feed does
  not report the end of a map (see below). The match page was chosen as the
  source of truth for map completion, with the feed providing the speed-up.
  Details in the plan, decision D7.
- Match-page polling remains a full-fledged fallback: per-map and per-half
  scores are visible on finished matches too.

## An important caveat about the end of a map

At the moment a map ends the feed does **not** move into any "map over" state:
`live` stays `true` while `currentRoundState` goes to `ended` — but that is the
**round's** state, not the map's, and it takes the same value at the end of any
ordinary round. The score in the feed is for the current map; there is no series
score (by maps) in the feed at all.

The naive rule "the score reached 13, so the map is over" breaks on overtimes
and on non-standard formats. That is why the end of a map is detected from the
match page — see [R3](R3-schedule-source.md), the `.mapholder` section.

## The map boundary

The mechanic has been found: the `log` carries
**`{"MatchStarted":{"map":"de_dust2"}}` at the start of every map in the
series**, not once per match. Caught on FORZE's match moving to its second map.
That is the signal for E5 and the marker of a map boundary.

What remains is to capture a full dump across a boundary for the replay tests —
`fixtures/scorebot-2397053-forze.jsonl.gz`.

## Why the live feed is needed for more than E5

The maps section on the match page updates by halves and with a delay (see
`docs/known-limitations.md`). So page polling gives a correct but late E6. The
live feed knows the round score at the moment rounds end, so at stage 4 it
becomes the primary source of speed while the page confirms the score.
