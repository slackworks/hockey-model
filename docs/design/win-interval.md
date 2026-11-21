# The win-chance range

## Problem

The report gave a win chance as one number.
A reader could not tell a firm 58% from a soft one (#212).

## Options

1. A range from the goal projection. This is a range of goals, not of win chance, and the owner rejected it.
2. A bootstrap range from many refits. It costs a refit for each game and each run.
3. A range from the uncertainty of the fitted coefficients, by the delta method on the log-odds scale.

## Choice

Option 3, as the owner decided on #212.

The fit keeps the inverse Hessian of its penalized loss at the solution, in the scaled space where the penalty acts.
For a game, the design row is scaled with the saved means and scales.
The standard error of the log odds is `sqrt(x' S x)`, where `S` is the inverse Hessian.
The range is `sigmoid(z ± q * se)`, where `q` is the normal quantile of the report level.
The away range is `1 - high` to `1 - low`.

## What the range leaves out

The range is model uncertainty only.
It leaves out noise in the Elo rating, noise in the feature values, and drift between seasons.
The point win chance already holds the randomness of the game, so the range is narrow by design.
The Markdown footer and the summary header say so beside every range.

## Where it lives

- `game_model/logistic.py` keeps the covariance in `FitDiagnostics`. A fit saved before this change has none.
- `game_model/win_interval.py` computes the range for one feature row.
- `store/fits.py` saves the covariance, and checks its shape, symmetry, and scaling on save and load.
- `game.py` carries `GamePrediction.home_win_interval`, and `matchup.check_win_interval` refuses a range that does not hold the point.
- `report_interval.py` writes the JSON object, the table cell, and the footer notes.
- `report_summary.py` writes the lines that open the release notes.

## Edge cases

- A fit with no covariance has no range. The report JSON holds `null`, and the summary says `no interval: Elo only` or says to fit again.
- A range that does not hold the point, or a covariance that gives a negative variance, raises. The run stops, as it does for a bad win pair or a bad goal projection, so a report never prints a range that is wrong.
- A range is optional in the schema, so `schema_version` stays 1. See `report-contract.md`.
- The covariance is not part of the model identity hash, because it changes no prediction. The range changes `inputs_sha256`, because a reader sees it.

## Tests

The true win probability is never seen, so no test checks coverage against outcomes.
The tests check that the range holds the point and stays inside (0, 1), that the width falls as the rows grow, that the stored covariance is the inverse of an independently built Hessian, that a hand-worked fit matches, and that the standard error has the size of the spread of refits on bootstrap samples.
