# Ice time forecast (#55)

Uses H1, H4, H5, H8 of [research-harness.md](research-harness.md).
Replaces `expected_toi` of [player-forecasts.md](player-forecasts.md) (#53).

## Goal

Forecast each skater's ice time in the next game apart from the per-minute rate, so a role change moves the count forecast through the exposure.

## Scope

- Phase 1 (ready now): total TOI from `skater_games`.
- Phase 2 (after #35): TOI by strength from the stints of [pbp-shift-dataset.md](pbp-shift-dataset.md).
- Whether a player dresses is out of scope. The store has no lineup history, and boxscore absence is not injury evidence (#8). Every forecast is conditional on the player dressing, and the report says so.

## Phase 1 model

For player `i` before the cutoff, over the player's own dressed games:

1. `ewma_i`: exponentially weighted mean TOI with half-life `H` games.
2. `n_i`: weighted count of games.
3. Shrunk: `toi_hat_i = (n_i * ewma_i + N0 * mean_p) / (n_i + N0)`, with `mean_p` the position mean of the training seasons.
4. Team scaling: for a team's 18 dressed skaters, scale the `toi_hat` so they sum to `TEAM_SKATER_SECONDS`, the average summed skater TOI per team game in the training seasons. Apply it only when the caller gives the full roster. A single-player call skips it.
5. Spread: the residual standard deviation by position and by `n_i` band on the training seasons. Predictive: normal with that spread, truncated at 0.

Choose `H` and `N0` on `SELECT_TRAIN` by the MAE of next-game TOI.

Baselines: last game, mean of the last 10 games.

## Phase 2

The same model for each strength class (`EV`, `PP`, `SH`) from stint time. Team scaling per class uses the team's expected class time from [strength-rates.md](strength-rates.md) (#41).

## Code

- `player_model/exposure.py`: `expected_toi(conn, nhl_id, cutoff, *, roster=None) -> ToiForecast` with `mean`, `sd`, and (phase 2) `by_strength`.
- #53's `expected_toi` calls it.

## Report

1. MAE and the normal log score of next-game TOI, against both baselines, by position.
2. Slices: players whose TOI moved by 3 minutes or more in the last 5 games (role change), fewer than 10 games, traded players.
3. Downstream: the #53 count log score with the new exposure against the old one.

## Tests

- A promotion (TOI from 12 to 18 minutes over 5 games): the forecast moves toward 18 at the rate the half-life gives.
- A player with no game gets the position mean.
- Team scaling: the 18 forecasts sum to `TEAM_SKATER_SECONDS`.
- Zero PP time in phase 2 gives a PP forecast near zero, not the position mean, once `n_i` is large.
- Future-invariance (H5).

## Calls made

- Conditional on dressing, because no pregame lineup history exists.
- A shrunk EWMA, because it reacts to a role change and needs two tuning numbers.
