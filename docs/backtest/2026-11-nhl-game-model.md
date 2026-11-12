# NHL game model backtest, November 2026

The joint test passed.
The model with expected-goals form and rest beats the results-only Elo on log loss.
The model does not use goalie goals saved above expected.
The guess run vetoed that feature.
The result has one caveat: MoneyPuck data may leak (see "What the result does not show").
The model does not beat the DraftKings line (see "Market comparison").

## Setup

The season plan is `DEFAULT_PLAN` in `src/hockey_model/game_model/seasons.py`.

| Phase | Seasons |
| --- | --- |
| Elo burn-in | 2014-15 |
| Selection, fit | 2015-16 to 2021-22 |
| Selection, validation | 2022-23 to 2023-24 |
| Scored | 2024-25 and 2025-26 |

The backtest chose the features on 2015-16 to 2023-24 only.
It refit the weights before each scored season, from all earlier seasons.
It scored 2,792 games.
The games include playoffs and exclude preseason.
The store is a complete backfill from 2014-10-01.
The NHL score feed returned HTTP 500 for 2017-09-19, a preseason day.
That day holds no regular season game, so no scored or fitted game is missing.

## Feature selection

The candidates were `xg_form`, `goalie_gsax`, and `rest`.
Log loss on the validation seasons (lower is better):

| Model | Actual-starter run | Guess run |
| --- | --- | --- |
| elo | 0.66276 | 0.66276 |
| elo + xg_form | 0.66112 | 0.66112 |
| elo + rest | 0.66161 | 0.66161 |
| elo + goalie_gsax | 0.66425 | 0.66339 |

The guess run vetoed `goalie_gsax`.
Its guess-run loss is higher than the Elo alone, by more than 1e-9.
The live features are `xg_form` and `rest`.
The goalie features read no starter, so the actual run and the guess run give the same numbers.

## Scored result

| Run | Games | Log loss | Brier | 90% interval (5th to 95th percentile) of the log loss difference to Elo |
| --- | --- | --- | --- | --- |
| home_rate | 2792 | 0.68973 | 0.24829 | -0.00675 to +0.01103 |
| elo | 2792 | 0.68760 | 0.24679 | not applicable |
| model_actual | 2792 | 0.67633 | 0.24180 | -0.01445 to -0.00834 |
| model_guess | 2792 | 0.67633 | 0.24180 | -0.01445 to -0.00834 |

The interval comes from a block bootstrap with 16-game blocks.
The success test needs the 95th percentile of the difference below zero.
This report is a first look. A later look at the same games uses a smaller tail (see `docs/design/promotion-bar.md`).
The interval of `model_actual` is fully below zero, so the test passed.
The rows `model_actual` and `model_guess` are equal because neither live feature reads the starting goalie.
The equal scores do not show that a wrong goalie costs nothing.
The goalie feature was vetoed, so the guess run had nothing to differ on.

## Calibration

Each row shows the number of games, the mean predicted home win chance, and the home win rate.

| Bin | Elo | Model |
| --- | --- | --- |
| 0.1 to 0.2 | 19, 0.181, 0.316 | none |
| 0.2 to 0.3 | 103, 0.257, 0.340 | 55, 0.265, 0.291 |
| 0.3 to 0.4 | 330, 0.359, 0.485 | 223, 0.361, 0.471 |
| 0.4 to 0.5 | 608, 0.453, 0.508 | 716, 0.455, 0.486 |
| 0.5 to 0.6 | 819, 0.549, 0.523 | 975, 0.549, 0.512 |
| 0.6 to 0.7 | 593, 0.648, 0.612 | 636, 0.644, 0.640 |
| 0.7 to 0.8 | 251, 0.738, 0.645 | 165, 0.736, 0.727 |
| 0.8 to 0.9 | 69, 0.831, 0.739 | 22, 0.814, 0.864 |

The Elo is too confident at both ends.
The model is closer to the win rate in most bins.
Both models still underrate the home side in the 0.3 to 0.5 range.

## Market comparison

A reviewer scored the game CSV against the last pre-game DraftKings line, with the vig removed.
He used regular season games only, and the same games for every run in a season.
The log loss of each run is below.

| Run | 2024-25 (1,228 games) | 2025-26 (1,286 games) |
| --- | --- | --- |
| DraftKings line | 0.6604 | 0.6826 |
| Model (actual starters) | 0.6633 | 0.6911 |
| Results-only Elo | 0.6741 | 0.7035 |

1. The model minus the line is +0.0029 in 2024-25, with a 90% block-bootstrap interval (5th to 95th percentile) of -0.0032 to +0.0091.
   The model is level with the line.
2. The model minus the line is +0.0085 in 2025-26, with an interval of +0.0035 to +0.0140.
   The model is significantly worse than the line.
3. The blend gate fails in both seasons.
   A logistic regression of the result on the market log-odds and the model log-odds gives the model a weight of +0.15 in 2024-25 (interval -0.51 to +0.85) and -0.43 in 2025-26 (interval -1.01 to +0.13).
   The gate needs a weight above zero with an interval fully above zero.
4. The model beats the Elo by about 0.011 to 0.012 in both seasons.
   This gain depends on the leak test of #77.
5. The two-season headline of 0.6763 is low because 2024-25 was easier to forecast for every run.
   The DraftKings line scored 0.6604 in that season.
6. The model is not cleared for staking.
   The sportsbettr follow-up (phaedrus1992/sportsbettr#1419) stays blocked.

## What the result does not show

1. MoneyPuck trained its expected-goals model on games that may include the scored seasons.
   The backtest uses today's MoneyPuck files for past games.
   The `xg_form` gain may look better than it would have in real time.
   The data cannot measure the size of this leak.
   Issue #77 tests it: replace the xG form with a form built from shot-attempt counts, which no model produced.
2. The Elo beats the home rate by 0.0022 log loss only.
   The model gain over the Elo is five times larger.
   Most of that gain comes from the `xg_form` feature.
   The leak in point 1 therefore matters for most of the result.
3. The backtest reads no price data.
   The game CSV (`--games-out`) lets a caller score the model against a closing line.
   A reviewer did this: see "Market comparison".
4. The store holds no ESPN injury history before 2026-11-03.
   The goalie guess run therefore never leaves out an injured goalie, and the live model does.
   The veto of `goalie_gsax` rests on a guess run without that rule.
5. Treat the Elo gain as unconfirmed until the leak test of #77 is done.
   A leak can only shrink the gain.
   The 0.6763 headline spans two seasons with playoffs, and the DraftKings figure of 0.6829 spans one regular season.
   The two numbers are not comparable.
   Use the matched-game table in "Market comparison".
6. The Elo beats the home rate by little, so the baseline of the joint test is weak.
   The market comparison is the stronger bar, and the gate in the spec for the sportsbettr follow-up applies it.
7. The test beat the Elo, and the Elo is worse than the market.
   Beating the Elo is necessary and not sufficient for a bet.
   The gate in the spec for the sportsbettr follow-up applies, and the model fails it.

## Missing inputs

17 goalie reads had no MoneyPuck history (`goalie_form`).
The goalie term counts as zero for that team.
`goalie_gsax` is not a live feature, so these gaps do not change the scored result.

## Data credit

Data from MoneyPuck.com.
The use is non-commercial, and the store reads only the published CSV and ZIP files.

## How to repeat the run

1. Set `HOCKEY_MODEL_DB` to a store that `hockey-model refresh --backfill-from 2014-10-01` filled.
2. Run `hockey-model backtest --games-out games.csv > backtest.json`.
3. Expect about one hour of run time.

The run is a new test each time the store changes.
A failed test is a result and not a reason to change the seasons.

## Live fit

The live fit used `xg_form` and `rest` through the 2025-26 season.
It stored fit 1 in the real store, with 14,508 games.

| Term | Value |
| --- | --- |
| Elo weight | 0.5847 |
| `xg_form` weight | 2.1010 |
| `rest` weight | 0.0908 |
| Intercept | 0.0530 |

The weights act on features that the fit scaled to unit variance.
The weights are not part of this repository.
Run `hockey-model fit --through-season 20252026 --features xg_form,rest` to make them again.
