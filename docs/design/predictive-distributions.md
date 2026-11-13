# Goal distributions and joint roster uncertainty (#52)

## Goal

Replace the plug-in Poisson goal interval with a tested predictive distribution of the game score, and give the draws that [outcome-contracts.md](outcome-contracts.md) needs.
Then give a joint roster total that keeps the dependence between teammates.

Uses H1, H4, H5 of [research-harness.md](research-harness.md).

## What exists

`NhlGameModel._expected_goals` gives each team a mean (the average of its own xG for and the opponent's xG against over `FORM_WINDOW` games).
`_projection` turns the mean into a Poisson interval.
The two teams are independent, and the rate has no uncertainty.

## Data readiness

The store already gives the regulation score of every final game:

- `last_period = 'REG'`: the final score is the regulation score.
- `last_period = 'OT'`: the regulation score was tied at the loser's score. The winner scored one OT goal.
- `last_period = 'SO'`: the regulation and OT score was tied at the loser's score. The final score adds one for the shootout winner.

Write `regulation_score(game: GameRow) -> tuple[int, int, str]` in a new module `game_model/score_model.py`, and test the three cases.
A game with an unknown `last_period` is left out and counted in the report.

## Part A: game score

Fit on seasons before the scored season (H4 folds). Score the regulation score of each game.

| Model | Regulation score | Fit |
| --- | --- | --- |
| M0 | independent Poisson with the current means | none (today's model) |
| M1 | independent negative binomial with the current means | one dispersion `alpha`: `statsmodels.discrete.discrete_model.NegativeBinomial` with the log mean as `offset` and only a constant |
| M2 | diagonal-inflated bivariate Poisson (Karlis and Ntzoufras 2003) | `lambda3` (shared component) and the inflation weight `p`, by maximum likelihood with `scipy.optimize.minimize` on the joint log-likelihood |

M2 is the main candidate. Published NHL data shows more regulation ties than independent Poisson gives (Birnbaum: about 18% in theory against 20% to 25% in practice), and a diagonal inflation is the standard fix for excess draws.
For M2, the joint PMF is `(1 - p) * BP(x, y; l1, l2, l3) + p * D(x) * [x == y]`, with `D` a Poisson PMF on the tied count with the mean `(l1 + l2) / 2`, and `l1 = mu_home - l3`, `l2 = mu_away - l3`.
Use `scipy.stats.poisson.pmf` terms and sum the shared component up to `min(x, y)`.
Truncate every count at `MAX_GOALS = 15` and check that the mass lost is under `1e-9`.

Anchor: before the draws, scale the two means by `exp(+d)` and `exp(-d)` so that the model's `P(home wins)` equals `home_win` of the winner model. Solve `d` with `scipy.optimize.brentq`. The OT and shootout step below is part of that win share.

Overtime and shootout:

- Regular season OT: `P(home wins OT | tied)` from a logistic fit of the OT winner on `log(mu_home / mu_away)`, with `statsmodels.api.Logit`, on tied games of the training seasons. `P(OT decides | tied)` is the training share of tied games that OT decided.
- Shootout: a home share fitted as one constant on training shootouts.
- Playoff OT: sudden death with no shootout. `P(home wins)` from the same logistic fit, on playoff OT games of the training seasons.

Scores for the report (H4):

1. Joint log score of the regulation score.
2. RPS of the total and of the home margin.
3. Regulation-draw calibration: predicted against observed share, by season.
4. 80% interval coverage and width of each team's goals, by game type.
5. The winner log loss of the anchored draws. It MUST equal the winner model's log loss within Monte Carlo error.

Draws: `simulate_game(mu_home, mu_away, params, game_type, rng, n) -> GameDraws` draws the regulation score from the chosen model, then OT and the shootout for tied draws.

Done for part A: the report exists. If M1 or M2 beats M0 on the joint log score with a paired interval fully below zero (exploration folds), the issue asks the owner to set `game_score` and `regulation_result` to supported in the capability matrix. Else they stay unsupported, and the report says why.

## Part B: roster totals

Part B needs the player model of [player-forecasts.md](player-forecasts.md) (#53). Start it after #53 merges.

For a roster `R`, a stat `s`, and a horizon of `h` games, in each draw `j`:

1. Draw each player's rate from the gamma posterior of #53: `rate_ij ~ Gamma(shape_i, scale_i)`.
2. Draw each game's team factor: `g_jk ~ Gamma(1 / v, v)` for game `k`, which has mean 1 and variance `v`. All players of one team share it in that game.
3. Draw the count: `count_ijk ~ Poisson(rate_ij * exposure_ik * g_jk)`. `exposure` comes from #55 when it exists, else from #53's recent ice time.
4. The roster total of draw `j` is the sum over players and games.

Fit `v` by maximum likelihood on training team-games: the team count is negative binomial with the variance `m + v * m^2` given the sum of player means `m`.
Return a `Projection` with the point as the mean of the totals and the interval from the empirical quantiles of the totals.
`JointModel.project_total` uses this. Never sum player intervals.

Scores for part B: count log score, RPS, and 80% coverage of roster totals, with `h` of 1 and of 10, on players with at least one earlier season.

## Code

- `game_model/score_model.py`: `regulation_score`, the M0 to M2 PMFs, the OT and shootout fits, `simulate_game`.
- `player_model/joint.py`: part B.
- `hockey-model research score-model`: the report command of H4.
- Every random draw uses `numpy.random.default_rng(seed)`.

## Tests

- `regulation_score` for `REG`, `OT`, `SO`, and an unknown `last_period`.
- M2 with `l3 = 0` and `p = 0` equals M0 to `1e-12` on a grid of counts.
- The truncated PMF of each model sums to 1 within `1e-9`.
- The anchored draws give `P(home wins)` within 3 Monte Carlo errors of the winner model, for 50 random mean pairs.
- A playoff draw never has `decided_in == "SO"`.
- Part B: with `v = 0` and fixed rates, the total is Poisson with the summed mean (compare the PMF). With `v > 0`, the covariance of two teammates' counts is above zero.
- The same seed gives the same draws.
- Future-invariance (H5) for the fitted parameters of a fold.

## Calls made

- M2 as the main candidate, because the excess of regulation ties over Poisson is documented and a diagonal inflation is the standard model for it.
- The anchor to the winner model, so the two capabilities never disagree.
- A minute-by-minute game simulation with empty-net goals is a better model of the game process. It needs goal times from #35. File it as a follow-up issue when part A ends, with the part A result as its baseline.

## Sources

- Karlis, D. and Ntzoufras, I. (2003). Analysis of sports data by using bivariate Poisson models. The Statistician 52(3).
- Birnbaum, P. "Has the extra point for the regulation tie increased the number of overtime games?" (philbirnbaum.com/overtime.pdf).
- Czado, C., Gneiting, T. and Held, L. (2009). Predictive model assessment for count data. Biometrics 65(4).
