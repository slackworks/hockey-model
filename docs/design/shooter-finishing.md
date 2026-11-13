# Shooter finishing talent (#51)

Uses H1 to H5, H7, H8 of [research-harness.md](research-harness.md).
Needs [shot-model.md](shot-model.md) (#50) for the baseline shot probability. Uses [scoring-environment.md](scoring-environment.md) (#42) for its recentering rule.

## Goal

Test whether a shooter's finishing beyond xG persists, so that a shooter-adjusted xG predicts future goals better.
Each shot is predicted before it updates the shooter's estimate.

## Model

For shot `n` by shooter `i`: `logit p_n = b_n + s_i`, where `b_n` is the logit of the frozen #50 model C, and `s_i` is the shooter effect.

The shooter effect has a normal belief `s_i ~ Normal(m_i, v_i)`, updated one shot at a time with the Laplace (assumed density) step for a logistic likelihood:

1. Predict: `p = expit(b_n + m_i)`. Store `p` as the shooter-adjusted xG of the shot. This is the value that every score uses.
2. Update with the outcome `y`: `v_i' = 1 / (1 / v_i + p * (1 - p))`, then `m_i' = m_i + v_i' * (y - p)`.
3. At each season start: `v_i = v_i + Q` (drift), and `m_i` stays.
4. A new shooter starts at `m = 0`, `v = V0`.

Order the shots of one game by `sort_order`. A shot never sees its own outcome before it is predicted.
Use `scipy.special.expit`.

Choose `V0` and `Q` by maximum predictive log-likelihood of the shots of `SELECT_TRAIN` with `scipy.optimize.minimize` over `(log V0, log Q)`.

Keep four values per shot: the raw MoneyPuck xG where joined, the #50 xG, the environment-adjusted xG (`times E(t)` of #42), and the shooter-adjusted xG.
If a later change recenters the `s_i`, move the mean into the intercept, and test that every `p` stays the same (the #42 rule).

## Features and forecasts

- `xg_form_shooter`: the `FORM_WINDOW` share of shooter-adjusted xG for and against, as `xg_form` does.
- Each shot's adjusted value counts once for the attacking team and once against the defending team, with the same number.
- Player goal forecasts use the posterior `m_i` and the shot volume from [player-forecasts.md](player-forecasts.md) (#53). That part starts after #53.

## Report

1. Shot log loss of model C alone against model C plus the shooter effect, on exploration seasons, with slices: shooters with fewer than 50 earlier attempts, rookies, and shooters who changed teams.
2. The chosen `V0` and `Q`, and the spread of `m_i` at the end of each season.
3. Winner: `xg_form_shooter, rest` against the reference `xg_form, rest` (H3), and against `xg_form_local, rest`.

## Tests

- A shooter with no shots predicts the baseline.
- A run of goals moves `m` up, and a run of misses moves it down. More shots give a smaller `v`.
- Flipping the outcome of shot `n` does not change the prediction of shot `n`.
- Adding a later shot changes no earlier prediction (H5).
- A trade keeps the shooter's belief.
- Probabilities stay in (0, 1).

## Calls made

- A one-pass Laplace filter instead of a full mixed model, because it predicts each shot before the update by construction and costs one pass over the shots.
- Drift once per season, not per day, because it adds one parameter.
