# League scoring environment (#42)

Uses H1, H4, H5, H8 of [research-harness.md](research-harness.md).

## Goal

Test a slow league-wide term that scales goal rates when league scoring moves, estimated from past games only.
Keep it separate from team, shooter, and goalie effects.

## Which quantity

The goal means of `NhlGameModel._expected_goals` come from MoneyPuck xG.
MoneyPuck fits one xG model on many seasons, so its xG can sit above or below the goals of one season.
So the environment term is the league's finishing ratio: on-ice goals divided by xG, over recent games.

`E(t) = (PRIOR_XG * E_prior + sum_k w_k * goals_k) / (PRIOR_XG + sum_k w_k * xg_k)`, a ratio of goals to xG. `PRIOR_XG` is the prior weight, in units of xG:

- `goals_k` and `xg_k` are the on-ice goals and the MoneyPuck xG of both teams in league game `k` before the cutoff. On-ice goals leave out the shootout increment (`regulation_score` of [predictive-distributions.md](predictive-distributions.md), plus an OT goal).
- `w_k = 0.5 ** (games_since_k / HALF_LIFE_GAMES)`, over league games.
- `E_prior` is the ratio of the previous complete season. For the first season of the store, it is 1.

The adjusted goal mean is `mu * E(t)`.
A season opener uses the prior almost alone. The final average of the current season is never used.

Choose `HALF_LIFE_GAMES` and `PRIOR_XG` from small grids on `SELECT_TRAIN` by the count log score of the team goals (H4), with a script under `scripts/research/`.

## What it can change

`E(t)` multiplies both teams' means, so it moves the totals and the tie chance, and it barely moves the winner.
So the test is a count report (H4), not a winner record:

1. Count log score and RPS of team goals and of the game total, M0 (today's means) against M0 times `E(t)`.
2. Calibration of the mean total by season and by month, with early-season slices (the first 100 league games of each season).
3. The winner log loss of both, to show that it does not move. A change in the winner is a sign of a bug.

A split by strength waits for [strength-rates.md](strength-rates.md) (#41).

## Code

- `game_model/environment.py`: a replay state (H2 rule 5) that holds the weighted sums, with `update(game)` and `ratio() -> float`. It reads the MoneyPuck xG of the game from the store when the game becomes final.
- `hockey-model research environment`: the H4 report.

## Recentering rule

#51 and later latent models MAY recenter player or team effects.
When a model recenters, it MUST move the same amount into `E(t)` or the intercept, so that every predicted shot probability is the same before and after.
The test in that issue asserts the probabilities equal to `1e-12` across a recentering step.

## Tests

- An empty league history gives `E_prior`.
- Tied start times: games with the same start update in game id order, and the result does not depend on that order (the sums commute).
- A postponed game never enters the sums.
- A shootout game adds the on-ice goals only.
- Simulated league drift: goals rise by 10% over a season, and `E(t)` follows it with the lag the half-life gives.
- Future-invariance (H5).

## Calls made

- The goals-to-xG ratio instead of goals per game, because the means already come from xG, and the ratio is the part that xG does not carry.
- A count report instead of a winner record, because a common factor cannot change the winner much.
