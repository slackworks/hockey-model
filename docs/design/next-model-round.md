# Next model round (#78)

## Goal

Run one joint confirmation test of the features that passed exploration, under the #38 protocol, and hand the per-game export to sportsbettr for the market comparison.
The owner target is to beat the market, not to match it (issue comment, 2026-11-03).
hockey-model reads no price data. The Kalshi and DraftKings comparison and the blend gate live in phaedrus1992/sportsbettr#1420 and #1419.

## Prerequisites

1. H2 and H3 of [research-harness.md](research-harness.md): the feature registry and the reference comparison.
2. #224, #225, #226, and #227 (library swaps) either merged before the round's freeze, or held until after its look. A swap during a round changes the numbers between registration and scoring.
3. The data fixes the issue names (#65, #66, #68, #84) closed or written as known limits in the record.

## Steps

1. Candidates. List each research issue whose exploration record shows a paired interval against the reference `xg_form, rest` with an upper end below zero. The likely first ones need no new data: #45 (`goalie_gsax_eb`), #43 (`rating_xg`), #40 phase 1 (`xg_form_sva`), #41 phase 1 (`strength_xg`), and #39.
2. Rank them by the exploration effect size and the cost to run live. Keep at most four, so one joint fit stays small.
3. Register one confirmation record: `cohort: "confirmation"`, `reference_features: ["xg_form", "rest"]`, `candidate_features` = the reference plus the kept features, `look_number` per `docs/design/promotion-bar.md`, `min_games` set before the look, `data_cutoff` = the registration day. Commit the record before any confirmation game is scored.
4. Freeze the code commit and the fit in the record's `baseline`.
5. At the look, run `hockey-model experiments run <id> --games-out games.csv`. The CSV goes to sportsbettr for the market comparison. Record the sportsbettr scorer's commit and the matched-game exclusions in a comment on this issue.
6. Promotion changes `FEATURES` only when the record is `promoted`. A rejected or inconclusive result stays in the registry.

## Done when

The record holds a result, and the issue holds the sportsbettr market result or an owner decision to stop.
A coded feature, a win over the Elo, or a ready API alone does not close the issue.

## Calls made

- One joint record per round, so the round spends one look (H7).
- At most four new features, so the joint fit does not dilute a real effect with noise features.
