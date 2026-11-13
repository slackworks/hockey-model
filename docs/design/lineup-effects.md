# Line and pair effects (#59)

Uses H1, H4, H5, H8 of [research-harness.md](research-harness.md).
Needs [pbp-shift-dataset.md](pbp-shift-dataset.md) (#35), [player-forecasts.md](player-forecasts.md) (#53), and [toi-forecast.md](toi-forecast.md) (#55).

## Goal

Test whether a forward line or a defense pair that plays together often does better or worse than its members predict.
This is a research study. A live lineup source (such as Daily Faceoff lines) is out of scope and needs its own source review.

## Units

- A forward unit: the sorted ids of the three forwards on the ice in a 5v5 stint of one team. A defense unit: the sorted ids of the two defensemen.
- Stints with a different count of forwards or defensemen are out of the unit data, and counted.
- Unit identity ignores the order of the players.

## Model

5v5 stints only. For each stint and team, the response is the team's xG for per hour, with weight equal to the stint length, and a second fit for xG against.
Use the per-shot xG of [shot-model.md](shot-model.md) (#50) when it exists, else the shot attempts per hour, labeled as such.

1. Members model (a RAPM-style fit): `sklearn.linear_model.Ridge` on a sparse design with `+1` for each skater of the team on ice (offense column) and `+1` for each opposing skater (defense column), plus a home column. Choose `alpha` by 5-fold cross-validation on training stints grouped by game (`sklearn.model_selection.GroupKFold`).
2. Unit model: the members model plus one column for each unit with at least `MIN_UNIT_SECONDS = 3600` of 5v5 time in training, with a penalty `UNIT_PENALTY_FACTOR` times larger than the member penalty (scale the unit columns, as in [goalie-team-confounding.md](goalie-team-confounding.md)). A unit below the minimum has no column, so it is its members only.
3. Fit on the seasons before the scored season.

## Test

On the scored season's stints, predict each stint's xG rates with the members model and with the unit model, and compare the weighted squared error, paired by game, with `block_bootstrap_mean_ci`.
The units are the actual units of the stint, so this is a test conditional on the lineup. It is not a pregame forecast. The report says so.
Slices: units seen in training, units not seen, and units with a changed member.

## Tests

- The unit id is the same for every order of the players.
- A stint with 4 forwards is left out and counted.
- A unit under `MIN_UNIT_SECONDS` has no column.
- Simulated data with a true unit effect: the unit model finds it. With no unit effect: the unit coefficients shrink to near zero.
- Future-invariance (H5).

## Calls made

- A ridge regression on stints, the standard public method for on-ice effects, with sparse input through scikit-learn.
- A conditional test on actual units, because no lineup history exists for a pregame test.
