# Playoff calibration (#44)

Uses H1, H4, H5, H8 of [research-harness.md](research-harness.md).

## Goal

Test whether playoff games need their own calibration of the model's win chance.
The regular season output MUST NOT change.

## Model

Let `z` be the logit of the home win chance of the frozen model (the Elo or the live fit), for a playoff game.
The calibrated logit is `z' = a + (1 + c) * z`.

Fit `a` and `c` with `statsmodels.api.GLM(won, [1, z], family=Binomial(), offset=z)`. The offset makes `c` the change from a slope of 1.

| Variant | Fit |
| --- | --- |
| P0 | none: `a = c = 0` |
| P1 | `.fit()`: no penalty |
| P2 | `.fit_regularized(alpha=LAMBDA, L1_wt=0.0)`: shrunk toward no change |

Choose `LAMBDA` by leave-one-postseason-out log loss over the postseasons of `SELECT_TRAIN`.
Do not copy the 0.479 coefficient of the Post Hockey PDF.

## Data rules

- A playoff game is `game_type == 3`.
- Leave out the 2019-20 postseason: it was played at neutral sites, so the home flag has no meaning there. Verify this in the stored games, and name it in the report.
- Check the 2020-21 season (division-only schedule) and keep its postseason when the home flag is real.
- A playoff overtime is sudden death with no shootout. The winner is the full-game winner, as today.

## Folds and the report

For each postseason `s` in `EXPLORATION`, fit on the postseasons before `s` and score `s`.
About 85 games each, so the interval is wide. Use a bootstrap over whole postseasons (resample seasons, not games) for the interval of the log-loss difference, with `numpy.random.default_rng`.

Report: log loss and calibration of P0, P1, and P2 by postseason, and the interval of P2 minus P0.
A wide interval is an inconclusive result, and the report says so.

## Promotion

The approved confirmation cohort is the regular season only (#38).
A playoff calibration can only be promoted by a new record with a future postseason cohort, and that cohort needs an owner decision.
This issue ends with the exploration report and that open question.

## Code

- `game_model/playoff_calibration.py`: `fit_playoff_layer(z, won, lam) -> PlayoffLayer`, `apply(layer, z, game_type) -> float`. `apply` returns `z` unchanged for every game type other than 3.
- `hockey-model research playoff-calibration`: the H4 report.

## Tests

- `apply` on a regular season game returns the input exactly.
- `a = c = 0` returns the input exactly.
- Probabilities stay in (0, 1) for `z` of plus and minus 30.
- No earlier postseason gives P0.
- Future-invariance (H5).

## Open question for the owner

Which future postseason may confirm a playoff layer, and with what minimum count of games?
