# R6 — The veto simulation on the match page

Date of observation: 2026-11-05. Measured on two live pages:
[fixtures/match-2400063-prediction.html](fixtures/match-2400063-prediction.html)
(upcoming BO3) and
[fixtures/match-2400291-prediction-done.html](fixtures/match-2400291-prediction-done.html)
(upcoming BO3, one day later).

## What HLTV added

The match page gained a **"Veto simulation"** block next to the real map
line-up: three alternative simulated vetoes ("Likeliest", "2nd", "3rd"),
switched by CSS-only radio inputs. Each alternative is rendered with the
**same `.mapholder` markup as the real maps**, and its `.mapname` carries a
percentage — `Mirage - 86%`, `Cache - 59%`.

So on a BO3 page an unfiltered `.mapholder` select returns **12 holders**: the
3 real ones (TBA before the veto) plus 9 simulated ones. The bot read them as
the line-up and announced matches of ten maps.

## How to tell them apart

Not by the holder itself — the markup is identical. Only by the ancestors:

* the real maps sit in a column with the class `real-veto-only`;
* the simulated ones sit under ancestors whose class starts with
  `simulated-veto` — and **the exact token differs from page to page**. On
  2400063 the box carries `veto-box simulated-veto simulated-veto-only`; on
  2400291 there is no bare `simulated-veto` at all, only
  `simulated-veto-only` and `simulated-veto-alt simulated-veto-alt--N` per
  alternative.

Hence the parser's filter is a **prefix match over every ancestor's class
list** (`_is_simulated`), not an exact-class lookup — an exact match on
`simulated-veto` silently sees nothing on the second page.

The names differ too (`Mirage - 86%`), but relying on the percentage would
break the day HLTV drops it; the structural filter does not care.

## What it does to a naive parser

Without the filter, everything derived from `maps` is poisoned: the map
line-up in E4 (`picks()`), the live-map detection, the series score, the
progress fingerprint. The fix lives in `parse()` itself so every consumer is
covered at once.
