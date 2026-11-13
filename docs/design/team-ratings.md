# Opponent-adjusted offense and defense ratings (#43)

Uses H1 to H5, H7, H8 of [research-harness.md](research-harness.md).

## Goal

Fit an offense and a defense rating for each team from earlier games, with the opponent and home ice in the model.
Compare it with the unadjusted `xg_form` on the winner and on goal forecasts.

## Data readiness

Ready now: final games (`latest_final_games`) and `mp_team_games`.

## Model

The Maher (1982) and Dixon and Coles (1997) model, with ridge.
For each team-game, the attacking team `i` against the defending team `j`:

`log E[y] = c + h * home + o_i - d_j`

- `y` is the team's on-ice goals (variant `goals`) or its MoneyPuck xG (variant `xg`). On-ice goals leave out the shootout increment.
- Fit with `statsmodels.api.GLM(y, X, family=sm.families.Poisson(), freq_weights=w).fit_regularized(alpha=a, L1_wt=0.0)`. A non-integer `y` (the xG variant) is a quasi-Poisson fit, which statsmodels accepts.
- `alpha` is a vector: 0 for `c` and `h`, `RIDGE` for every `o` and `d`.
- `w = exp(-XI * days_before_checkpoint)`, the Dixon and Coles time decay.
- Teams are franchises (`sources.teams.franchise`), so a relocation keeps its history. A new franchise has no rows and gets `o = d = 0` from the ridge.
- The ridge pulls the ratings toward zero, so the solution is unique and centered. Report the sums of `o` and `d` at each checkpoint, which should be near zero.

Choose `RIDGE` and `XI` on a grid on `SELECT_TRAIN`, by the count log score of team goals at weekly checkpoints.

Refit every `REFIT_EVERY_DAYS = 7` on every game before the checkpoint. A replay feature (H2 rule 5) holds the rows and the newest fit.
The first weeks of a season use the decayed earlier seasons, so a disconnected early schedule needs no special case.

## Features

- `rating_goals`: `(o_home - d_away) - (o_away - d_home)` from the goals variant.
- `rating_xg`: the same from the xG variant.

Each is a log-rate difference. It holds no `h` term, because the Elo already has home ice. An ablation adds `h` to show the double count.

The same fit gives goal means: `exp(c + h + o_home - d_away)` for the home team. The count report (H4) compares them with today's means.

## Report

1. The paired winner log loss of `xg_form, rest` against `rating_xg, rest`, against `xg_form, rating_xg, rest`, and the same with the goals variant (H3).
2. The count log score of the team goals from the ratings against today's means.
3. Do not report rank correlations as evidence. They are not a forecast score.

## Tests

- Simulated league with known `o` and `d` and an unbalanced schedule: the fit recovers the order of the teams better than the raw goals do.
- A relabel of every team code gives the same ratings under the new labels.
- The ridge gives a unique fit when one team has no games.
- A shootout adds no goal to `y`.
- Future-invariance (H5).

## Calls made

- A Poisson GLM with a log link, so a goal mean can never be negative.
- The statsmodels ridge with a zero penalty on the intercept and home terms.
- Weekly refits, for cost.

## Sources

- Maher, M. J. (1982). Modelling association football scores. Statistica Neerlandica 36(3).
- Dixon, M. J. and Coles, S. G. (1997). Modelling association football scores and inefficiencies in the football betting market. Applied Statistics 46(2).
