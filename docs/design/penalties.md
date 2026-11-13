# Penalty drawing and taking (#57)

Uses H1, H4 to H6, H8 of [research-harness.md](research-harness.md).
Needs [pbp-shift-dataset.md](pbp-shift-dataset.md) (#35). Feeds [strength-rates.md](strength-rates.md) (#41).

## Goal

Forecast how many power plays and how much power-play time each team gets in a game, from the teams' earlier drawn and taken penalties.
Test the forecast on future power-play counts before any winner test.

## Taxonomy

Read the penalty events of the play-by-play fixtures first. List every `typeCode` and `descKey` with counts by season in the report.
Then map them to these classes. Verify each rule against the NHL rulebook of the season (rules 15 to 23 in recent books), and cite the rule in a comment.

| Class | Minutes | Power play | Ends on a goal |
| --- | --- | --- | --- |
| minor | 2 | yes, unless matched by a coincidental minor | yes |
| double minor | 4 | yes | the current 2 minutes end on a goal |
| major (fights included) | 5 | yes, unless coincidental | no |
| misconduct, game misconduct | 10 or the rest of the game | no | no |
| match | 5 plus ejection | yes | no |
| bench minor | 2 | yes | yes |
| penalty shot | none | no | not applicable |

A coincidental pair (same time, both teams, same class) gives no power play.
A delayed penalty shows in `situation_code` as an extra skater. It is not a power play.

## Opportunities

A power-play opportunity is a span where the `situation_code` gives one team more skaters, as the result of a penalty.
Count opportunities and power-play seconds per team game from the stints, so the counts agree with the strength data by construction.
Check against the penalty events: every opportunity has a penalty event that starts it. A span with none is reported.

## Model

For team `i`, before the cutoff, with exposure in team game seconds:

- Drawn rate `r_i ~ Gamma` posterior: prior `Gamma(alpha, beta)` fit on team-seasons of `SELECT_TRAIN` by maximizing the negative binomial marginal likelihood (`scipy.stats.nbinom`, `scipy.optimize.minimize`). Posterior `Gamma(alpha + drawn, beta + seconds)`, with each game's counts decayed by `0.5 ** (games_ago / HALF_LIFE_GAMES)`.
- Taken rate `t_i` the same way.
- Expected opportunities of the home team: `T * r_home * t_away / league_rate`, with `T` the average game seconds, and a home factor fit on `SELECT_TRAIN` as one constant.

Baselines: B0 the league rate for every team, B1 the team's raw drawn and taken rates over the same window.

## Score

1. Count log score of the opportunity count per team game (negative binomial predictive) for B0, B1, and the model, on exploration seasons (H4).
2. Power-play seconds: the MAE of the forecast.
3. Only if the model beats B0 with an interval fully below zero: feed its expected PP seconds into #41 as `T_PP` per game, and run the #41 winner test again.

Player-level rates are weak evidence (source research). They are out of scope here.

## Tests

- Hand-built timelines: a minor, a coincidental pair, a double minor with a goal at 1:00, a major with a goal, a misconduct, a penalty shot, and two overlapping minors (5 on 3).
- Opportunity counts and seconds agree with the stints.
- A bench minor has no player but counts for the team.
- No penalties gives zero opportunities and no error.
- Future-invariance (H5).

## Calls made

- Opportunities from stints, so the count and the strength time come from one source.
- Team rates only, because the source research finds player PP and PK residuals weak.
