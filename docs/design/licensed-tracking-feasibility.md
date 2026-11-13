# Licensed entry, exit, and passing data: feasibility (#63)

## Goal

Decide whether an authorized source of manually tracked zone entries, exits, and passes exists for this project.
The deliverable is a feasibility report. No code reads licensed data before the owner approves the terms.

## Known sources (2026-11-13)

| Source | What | Access | Terms known |
| --- | --- | --- | --- |
| All Three Zones (Corey Sznajder) | Zone entries and exits, passes, scoring chances, retrievals, for a subset of games each season since 2013-14 | Patreon subscription (7 dollars a month), spreadsheets behind a password | No written terms for use, storage, or redistribution found. A 2016 statement allowed any use with credit, for that season's data. |
| Sportlogiq | Full event tracking | Commercial contract | Commercial, terms by contract |
| NHL EDGE | Puck and player tracking summaries | Public site, see [edge-feasibility.md](edge-feasibility.md) | Not checked |

## Steps

1. The project owner asks the All Three Zones owner for written terms: may the data be stored in a private store, used in a model, and shown in a public report as aggregates? Record the answer in the issue. Do not subscribe or download before the owner of this project approves the cost and the answer.
2. If the terms allow use: list the seasons, the share of games tracked in each season, the teams tracked (tracked games are not a random sample), and the time between a game and its data.
3. Write the report with a go or no-go.

## If the answer is go

- An adapter reads the spreadsheets into a private table that never goes into a fixture, a public report row, or a release. Fixtures use synthetic data.
- Rates use the right denominator: exits per retrieval, entries per possession, chance assists per pass attempt.
- Tracked counts, coverage fractions, and any imputed season estimate stay in separate fields.
- A model of which games are tracked (team, opponent, month) goes with every estimate, because the sample is not random.
- The prediction test then follows [player-forecasts.md](player-forecasts.md) (#53) and H3 of [research-harness.md](research-harness.md), on matched coverage.

A clear no-go is a complete result for this issue.

## Status (2026-11-13)

On hold. The owner talks to the All Three Zones owner first. Do not subscribe, download, or write to the source until the owner records the result in #63.
