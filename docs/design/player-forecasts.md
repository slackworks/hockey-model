# Multi-season player forecasts (#53)

Uses H1, H4, H5, H8 of [research-harness.md](research-harness.md).
This is the base player model. #52 part B, #54, #55, #56, and #59 build on it.

## Goal

Ship a player model behind the existing `StatModel` protocol for goals, assists, and points of skaters.
It blends earlier seasons with the current season and shrinks a player with little ice time toward a position prior.

## Data readiness

- Ready now: `skater_games` (game, team, player, `toi_seconds`, goals, assists) for every stored final game, and `players` (position, team).
- A stat other than `goals`, `assists`, and `points` raises `MissingProjectionError` that names the stat.
- A goalie raises `MissingProjectionError`. Goalie projections are out of scope.

## Model

For player `i`, stat `k` (`goals` or `assists`), before the cutoff:

- Exposure `E_i = sum_g w_g * toi_g / 3600` in hours, and count `Y_i = sum_g w_g * y_g`, with `w_g = 0.5 ** (games_ago_g / HALF_LIFE_GAMES)` over the player's own games.
- Prior `rate ~ Gamma(alpha_p, beta_p)` by position group `p` in `{F, D}`. A player with no NHL game before the season uses a rookie prior `(alpha_rp, beta_rp)`.
- Posterior `Gamma(alpha_p + Y_i, beta_p + E_i)`.

Fit each prior by maximum likelihood on player-seasons of `SELECT_TRAIN`: a season's count is negative binomial given its exposure, `nbinom(n=alpha, p=beta / (beta + E))`. Use `scipy.stats.nbinom.logpmf` and `scipy.optimize.minimize` over `(log alpha, log beta)`.
Choose `HALF_LIFE_GAMES` from `{41, 82, 164, 246}` by the next-season count log score on `SELECT_TRAIN`. The script is `scripts/research/fit_player_priors.py`.

Expected exposure for `h` future games: `h * toi_hat / 3600`, where `toi_hat` is the mean TOI of the last 10 games, or the position mean for a player with none. [toi-forecast.md](toi-forecast.md) (#55) replaces this later through one function, `expected_toi(conn, player, cutoff) -> float`.

Predictive count for `h` games: negative binomial with `n = alpha_post`, `p = beta_post / (beta_post + exposure)`.
`Projection.point` is the predictive mean. `low` and `high` are `scipy.stats.nbinom.ppf` at `(1 - level) / 2` and `(1 + level) / 2`.

Points are goals plus assists. The two rates share the player's exposure, so draw them together: `numpy.random.default_rng(seed)` draws a goal rate and an assist rate from their posteriors and the counts from Poisson, then adds them. Use `POINT_DRAWS = 20000`. The interval comes from the empirical quantiles.

## Code

- `player_model/rates.py`: `PlayerHistory`, `player_history(conn, nhl_id, cutoff)`, the posterior.
- `player_model/model.py`: `StorePlayerModel(conn, as_of: datetime, seed: int = 0)` implements `StatModel.project(player, stat, horizon_games, level)`. The `as_of` is fixed at construction, because `StatModel.project` has no `as_of` argument.
- `hockey-model research player-forecasts`: the H4 report.
- `README.md`: the player model section, with its stats and its limits.

## Report

For each exploration season, at checkpoints on the 1st of each month from November to March, project each skater's next 10 games and score the observed counts.

1. Count log score and RPS of the model against three baselines: last-season rate only, the rate of the last 20 games, and the position mean (each with the same exposure and a Poisson predictive).
2. 80% coverage and width.
3. Slices: rookies, players with fewer than 10 earlier games, players who changed teams in the last 30 days, and the first 10 games of a season.

## Tests

- A rookie with no game gets the rookie prior.
- More ice time pulls the posterior mean toward the player's own rate.
- A trade does not reset the history.
- `horizon_games = 0` raises the error that `project_player` already raises for a bad horizon.
- A goalie and an unsupported stat raise `MissingProjectionError` with the full message.
- Points: the mean of the draws equals the sum of the goal and assist means within 1%.
- A model built at `as_of` ignores every game after it (H5).

## Calls made

- A gamma-Poisson model by position, the standard empirical-Bayes model for a count rate, with a decay instead of fixed season weights.
- Last-10-games TOI as the first exposure, behind one function that #55 replaces.
