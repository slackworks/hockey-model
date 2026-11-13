# NHL EDGE skating data: feasibility (#62)

## Goal

Decide whether NHL EDGE skating-speed data can be a pregame player feature.
The deliverable is a feasibility report. No ingestion is built before the owner approves the source.

## What public reports say (2026-11-13)

- Community references (Zmalski/NHL-API-Reference issue 69, the fastRhockey R package) list EDGE endpoints on `api-web.nhle.com`, such as `/v1/edge/skater-skating-speed-detail/{player_id}/{season}/{game_type}` and a `/now` form for the current season.
- The skating-speed payload holds bursts over speed thresholds and top speeds, with the time of each burst.
- The values are season totals. No endpoint is known to give a value as it was on a past date.
- One report says an older season of the shot-speed endpoint returns only a top-10 list. The depth of older seasons for skating speed is not known.

None of this is checked yet. The first task checks it.

## Steps

1. Probe, by hand, three players and three seasons (2023-24, 2024-25, current) on the skating-speed and skater-detail endpoints. Save the trimmed payloads under `tests/fixtures/edge/` only after the owner approves the source. Write the field list, the units, the speed thresholds, and the seasons available (`seasonsWithEdgeStats` if present).
2. Check whether any field gives a per-game or per-date value. If none does, past pregame values cannot be built: a season total includes games after the cutoff.
3. Request cost: about 800 skaters for a daily snapshot is about 800 requests a day at the host's spacing.
4. Write the report: fields, history, the leak problem of season totals, cost, and terms of use as stated by the NHL site.

## Decision paths

- No per-date data: the only honest path is a prospective daily snapshot from the approval date. A feature test can then start after one season of snapshots. The report proposes the snapshot schedule and its cost.
- Per-game data exists: design a feature, with the bursts above each threshold per hour of ice time, shrunk by position (gamma-Poisson as in [player-forecasts.md](player-forecasts.md)), and test it against age, TOI, and recent production.

## Open question for the owner

Is the EDGE endpoint family an approved source, and is a daily snapshot of about 800 requests acceptable?
