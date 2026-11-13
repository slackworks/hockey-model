# Designs for the large and extra-large issues

One design file covers each open `size/L` and `size/XL` issue as of 2026-11-13.
Each design names its prerequisites, the data it needs, the exact method, the tests, and the done condition.
A design is a snapshot. When the code must differ for a correctness reason, change the design in the same pull request (rules/engineering-judgment.md).

## Shared designs (build these first)

| Design | What it gives | Build order |
| --- | --- | --- |
| [research-harness.md](research-harness.md) | Libraries (H1), feature registry (H2), reference comparison (H3), count scores and reports (H4), future-invariance kit (H5), data map (H6), look budget (H7), done list (H8) | 1 |
| [moneypuck-situations.md](moneypuck-situations.md) | Strength splits and extra MoneyPuck columns, from files the store already downloads | 2 |
| [payload-archive.md](payload-archive.md) (#82) | Payload revisions and boxscore revisions | 3 |
| [pbp-shift-dataset.md](pbp-shift-dataset.md) (#35) | Events, shifts, stints | 4 |

## Issue map

| Issue | Design | Needs first | Data ready now | Owner question |
| --- | --- | --- | --- | --- |
| #31 | [mutant-survivors.md](mutant-survivors.md) | none | yes | none |
| #35 | [pbp-shift-dataset.md](pbp-shift-dataset.md) | #82 | new source, approved | none |
| #37 | [recording-drift.md](recording-drift.md) | H1 | part A yes | none |
| #40 | [score-venue-form.md](score-venue-form.md) | H2, H3, MoneyPuck situations | phase 1 after MoneyPuck situations | none |
| #41 | [strength-rates.md](strength-rates.md) | H2, H3, MoneyPuck situations | phase 1 after MoneyPuck situations | none |
| #42 | [scoring-environment.md](scoring-environment.md) | H1, H4 | yes | none |
| #43 | [team-ratings.md](team-ratings.md) | H2, H3 | yes | none |
| #44 | [playoff-calibration.md](playoff-calibration.md) | H1, H4 | yes | which future postseason confirms |
| #45 | [goalie-shrinkage.md](goalie-shrinkage.md) | H2, H3 | yes | none |
| #46 | [goalie-team-confounding.md](goalie-team-confounding.md) | #45 | yes | none |
| #47 | [rebound-control.md](rebound-control.md) | #45, MoneyPuck situations, #37 | phase 1 after MoneyPuck situations | none |
| #48 | [goalie-contact.md](goalie-contact.md) | #45, MoneyPuck situations | phase 1 after MoneyPuck situations | none |
| #49 | [starter-mixture.md](starter-mixture.md) | H3 | yes | none |
| #50 | [shot-model.md](shot-model.md) | #35, #37 | after #35 | none |
| #51 | [shooter-finishing.md](shooter-finishing.md) | #50, #42 | after #50 | none |
| #52 | [predictive-distributions.md](predictive-distributions.md) | H1, H4, #83; part B needs #53 | part A yes | none |
| #53 | [player-forecasts.md](player-forecasts.md) | H1, H4 | yes | none |
| #54 | [aging-adjustments.md](aging-adjustments.md) | #53 | no birth dates | approve the NHL player landing endpoint |
| #55 | [toi-forecast.md](toi-forecast.md) | #53 | phase 1 yes | none |
| #56 | [assist-forecasts.md](assist-forecasts.md) | #35, #53 | after #35 | none |
| #57 | [penalties.md](penalties.md) | #35 | after #35 | none |
| #58 | [faceoff-value.md](faceoff-value.md) | #35, #50 | after #35 | none |
| #59 | [lineup-effects.md](lineup-effects.md) | #35, #53, #55 | after #35 | none |
| #60 | [season-simulation.md](season-simulation.md) | H1 | yes | none |
| #61 | [bracket-simulation.md](bracket-simulation.md) | #60 | yes | none |
| #62 | [edge-feasibility.md](edge-feasibility.md) | none | no | approve the EDGE endpoints and a daily snapshot |
| #63 | [licensed-tracking-feasibility.md](licensed-tracking-feasibility.md) | none | no | approve the cost and the contact for terms |
| #78 | [next-model-round.md](next-model-round.md) | H2, H3, the research results | not applicable | none |
| #80 | [consumer-api.md](consumer-api.md) | none | yes | none |
| #82 | [payload-archive.md](payload-archive.md) | MoneyPuck situations (schema order) | yes | none |
| #83 | [outcome-contracts.md](outcome-contracts.md) | #80 | yes | none |
| #138 | [backtest-speed.md](backtest-speed.md) | none | yes | none |
| #176 | [mutation-function-shards.md](mutation-function-shards.md) | none | yes | none |
| #188 | [tla-state-space.md](tla-state-space.md) | none | yes | none |

## Schema versions

Several designs add tables through `hockey-model store upgrade`. Take the versions in this order, so two pull requests never claim one number:

1. Version 3: [moneypuck-situations.md](moneypuck-situations.md).
2. Version 4: [payload-archive.md](payload-archive.md).
3. Version 5: [pbp-shift-dataset.md](pbp-shift-dataset.md).
4. Then [consumer-api.md](consumer-api.md) (`fit_labels`), [shot-model.md](shot-model.md), and [aging-adjustments.md](aging-adjustments.md) (`player_bio`), in the order they merge.

When a design merges out of this order, it takes the next free version and updates this list.

## Library replacements of existing code

These issues move existing hand-written algorithms to libraries: #224 (logistic fit), #225 (log loss, Brier, calibration), #226 (block bootstrap), #227 (Poisson quantile), and #119 (JSON Schema validator).
