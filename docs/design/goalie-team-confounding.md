# Goalie and team-defense effects (#46)

Uses H1 to H5, H7, H8 of [research-harness.md](research-harness.md).
Starts after [goalie-shrinkage.md](goalie-shrinkage.md) (#45), because it uses its exposure and its result as the baseline.

## Goal

Test whether a goalie's goals saved above expected is partly the team in front of the goalie, and whether a split into a goalie effect and a team-season effect predicts better than the goalie effect alone.
Post Hockey reports no added value from such a split. A negative result is the expected result, and it is a valid one.

## Data readiness

Ready now: `mp_goalie_games` (xGA, goals against, team) and `mp_team_games`.

## Model

For each goalie game `k` of goalie `g` on team-season `s`, the response is `y_k = xga_k - ga_k` with variance near `xga_k`.
The model is `y_k = xga_k * (a_g + b_s) + e_k`.

Fit it as a weighted ridge regression with `sklearn.linear_model.Ridge(alpha=1.0, fit_intercept=False)` on a `scipy.sparse.csr_matrix`:

- One column for each goalie, with the value `xga_k / sqrt(lambda_goalie)` in the row of game `k`.
- One column for each team-season, with the value `xga_k / sqrt(lambda_team)`.
- `sample_weight = 1 / xga_k`, skipping a game with `xga_k = 0`.
- The fitted coefficient times `1 / sqrt(lambda)` gives `a_g` and `b_s`.

The column scaling gives each group its own penalty with one `alpha`.
Ridge shrinks both groups toward zero, which also centers them.

Three models on the same folds:

| Model | Columns |
| --- | --- |
| G | goalies only (close to #45 with no decay) |
| T | team-seasons only |
| GT | both |

Choose `lambda_goalie` and `lambda_team` from a grid of 7 values each (log-spaced, 10 to 10,000 xGA) by the next-season log-likelihood of `y` on `SELECT_TRAIN`.

Refit at weekly checkpoints: every 7 days (`REFIT_EVERY_DAYS`), on every game before the checkpoint. A game between two checkpoints uses the fit of the earlier one. This keeps the backtest cost to about 30 fits per season.
Implement it as a replay feature (H2 rule 5) whose state holds the rows so far and the newest fit.

Feature `goalie_gt`: home starter `a_g` minus away starter `a_g`, from model GT. The team part `b_s` is already in the team's own form and Elo, so the feature uses only the goalie part.

## Report

1. The paired winner log loss of `goalie_gt` against the reference `xg_form, rest`, and against `xg_form, rest, goalie_gsax_eb`.
2. The next-season log-likelihood of `y` for G, T, and GT.
3. Identification: build the bipartite graph of goalies and team-seasons with `scipy.sparse.csgraph.connected_components`. Report the count of components and the share of goalie games in the largest one. A goalie seen behind one defense only cannot be separated from it.
4. Slices: starters (40 or more games in the season) against backups, and goalies who changed teams, before and after the trade.
5. The correlation between `a_g` and the team's `b_s`.

## Tests

- Simulated data with known `a` and `b` and goalies who move between teams: GT recovers both with a correlation above 0.8.
- Simulated data where each goalie plays for one team only: the report marks every goalie as not identified, and GT does not beat G.
- A relabel of the team codes does not change any `a_g`.
- Future-invariance (H5).

## Calls made

- Ridge with group scaling instead of a mixed model: it needs no `pandas` formula, it takes sparse input, and a fixed penalty is a Gaussian random effect with a fixed variance.
- Weekly refits instead of a refit before each game, for cost.
