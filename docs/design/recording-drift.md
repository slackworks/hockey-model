# Recording-era drift audit (#37)

Uses H1, H4, H6 of [research-harness.md](research-harness.md).

## Goal

Measure how the recorded data changes by season and by venue, separate recording changes from hockey changes where the data allows, and measure what each choice of training window costs the model.

## Part A: aggregate audit (ready now, and more after the MoneyPuck situations table)

For each season from 2014-15, compute per game, from `mp_team_games` and, when it exists, `mp_team_situations`:

- goals, xG, goals per xG
- shots on goal, missed shots, blocked attempts, and the missed share of unblocked attempts
- rebounds per unblocked attempt
- the share of team games with a NULL in each column

Per venue (the home team of the game) and season: the missed share and the rebounds share against the league share of that season. Report the venues more than 3 standard errors from the league.

Breakpoints, on training data only:

1. For each metric, take the per-game values of the `SELECT_TRAIN` seasons.
2. For each season boundary `b`, fit `statsmodels` OLS of the metric on a step at `b`, with standard errors clustered by game day (`cov_type="cluster"`).
3. Report the boundary with the smallest residual sum of squares and its t value. One boundary per metric. A second boundary is out of scope.
4. A boundary inside the scored seasons is reported as seen, and it is never fitted there.

## Part B: event audit (after #35)

From the events of [pbp-shift-dataset.md](pbp-shift-dataset.md): the share of events with NULL coordinates, by type, season, and venue; the distribution of shot distance by season (quantiles 10, 50, 90); and the share of attempts with no `attack_x_sign`.

## Part C: training window cost

Run the backtest of the live features with different first fit seasons, through `SeasonPlan(fit_first=...)`:

1. All seasons from `FIT_FIRST` (today).
2. The seasons from the part A boundary of goals per xG, when one exists.
3. The 3 and the 5 newest seasons before each scored season. This needs a new `SeasonPlan` field `fit_window: int | None`. `_fit_windows` uses it.

Report the paired winner log loss of 2 and 3 against 1, by scored season, as exploration.

## Output

- `hockey-model research recording-drift` writes the JSON and Markdown report (H4).
- A JSON Schema next to the JSON writer, and the writer validates before it prints (rules/model-facing-output.md).

## Tests

- A simulated series with a step at a known boundary: the search finds it.
- A constant series: no boundary has a t value above 2.
- An empty or all-NULL season is reported as such, and it does not raise.
- Adding a scored season changes no part A result.

## Calls made

- An exhaustive one-step search over season boundaries with statsmodels, instead of a change-point package. `ruptures` has no CPython 3.14 wheel, and a season grid has only about ten boundaries.
