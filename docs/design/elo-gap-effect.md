# The effect of a store gap on the Elo win chance (#220)

## Problem

The model replays the Elo ratings over every final game from the burn-in season.
A game that the store lacks is missing from that replay, so a gap in a training season changes the ratings that later predictions use.
The daily report must say which gaps can matter.

## Rule

A dated gap is on the game rows when its season is in `SCORED`, is the current season, or is the season before the current one.
Every other dated gap goes to the footer and to `training_gaps`.
The footer says how far a gap moved a win chance in the measured case, `OLD_GAP_MEASURED_SHIFT_PP` percentage points.
The season of a score day comes from its date, of a boxscore from the first four digits of its game id, and of a MoneyPuck goalie file from its year.
A URL with no season to read, such as the MoneyPuck team file, stays on the game rows.
The owner decided this rule on 2026-11-20.
Today the season before the current one is in `SCORED`, so the rule first changes a report in the 2027-28 season.

## Measurement

The store held 16,062 final games from the 2014-15 season.
The replay used `EloBook`, with each team at the initial rating on its first game.
The measure is the change of the pre-game home win chance of every game in a later season, when the dropped games do not update the ratings.
The model bound is `0.25 * elo_weight * max |change of the Elo logit|`, with `elo_weight = 0.585` from the live fit.

The first measurement, on the 2017-18 season, showed no effect, because the failed day held preseason games and the store keeps none.
Dropped games in 2023-24 reach the scored games of 2024-25 and of 2025-26:

| Dropped games | Season scored | Max Elo change | Mean Elo change | Model bound |
| --- | --- | --- | --- | --- |
| Busiest day of 2023-24 (14 games) | 2024-25 | 0.89 pp | 0.16 pp | 0.52 pp |
| Busiest day of 2023-24 (14 games) | 2025-26 | 0.31 pp | 0.06 pp | 0.20 pp |
| Last 7 game days of 2023-24 | 2024-25 | 1.12 pp | 0.07 pp | 0.66 pp |
| Last 7 game days of 2023-24 | 2025-26 | 0.34 pp | 0.02 pp | 0.21 pp |
| Last day of 2023-24 | 2024-25 | 0.48 pp | 0.03 pp | 0.28 pp |
| Last day of 2023-24 | 2025-26 | 0.14 pp | 0.01 pp | 0.09 pp |

The boundary case of the rule is a gap two seasons before the current one.
With 2025-26 as the current season, that is 2023-24, and the rows for 2025-26 are the ones to read.
The largest change there is 0.34 percentage points, so `OLD_GAP_MEASURED_SHIFT_PP` is 0.34.
The footer states this case only, and does not state a bound for every old gap.
A gap in the season just before the predicted games moves a win chance by about 1 point or less (Elo) in the rows for 2024-25, and by more when the dropped games are close to the predicted games, so that gap stays on the game rows.

An Elo error fades by about 1 percent for each game, so a gap further back moves less.

## Limits

The measurement used one store, and it dropped whole days.
A gap that drops far more games than a week would move more.
Run the measurement again when the report adds a season, or when the Elo constants change.
