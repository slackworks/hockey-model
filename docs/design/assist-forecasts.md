# Assists adjusted for teammate finishing (#56)

Uses H1, H4, H5, H8 of [research-harness.md](research-harness.md).
Needs [pbp-shift-dataset.md](pbp-shift-dataset.md) (#35) and [player-forecasts.md](player-forecasts.md) (#53). Uses [shooter-finishing.md](shooter-finishing.md) (#51) when it exists.

## Goal

Test whether a forecast that separates a player's share of teammate goals from the teammates' finishing beats the #53 assist rate.

## Model

From the events and stints:

1. For each goal, the on-ice skaters of the scoring team (the stint that holds the goal, `start < t <= end`).
2. For player `i`, by strength (EV, PP): `G_on` = team goals with `i` on ice, `A1` = primary assists, `A2` = secondary assists.
3. Primary share `s1_i ~ Beta` posterior: prior `Beta(a, b)` by position and strength, fit by maximum likelihood with `scipy.stats.betabinom` on player-seasons of `SELECT_TRAIN`; posterior `Beta(a + A1, b + G_on - A1)`. Secondary share `s2_i` the same, with its own prior. A goal with no assist counts in `G_on` and in neither share.
4. Expected on-ice team goals for player `i` in a game: `xg_on_rate_i * toi_hat_i`, where `xg_on_rate_i` is the team xG per hour with `i` on ice over the decayed history, shrunk toward the team rate. With #51, use the shooter-adjusted xG of the teammates instead.
5. Expected assists = expected on-ice goals times `(s1_i + s2_i)`.

A player with no on-ice goals keeps the prior shares. Nothing divides by zero.

## Draws

In one draw: the team's on-ice goals while `i` is on ice, then for each goal one categorical draw (primary by `i`, secondary by `i`, or neither) with the shares. Then the player's points equal the drawn goals plus the drawn assists of that same draw.
This fits the draw shape of [outcome-contracts.md](outcome-contracts.md).

## Report

1. Next-10-games assist count log score: #53 alone, this model with raw xG, and with #51's adjusted xG.
2. League reconciliation by season and strength: the sum of predicted assists against the actual assists. A gap is reported, and a normalizer from earlier seasons only may fix it.
3. Slices: traded players, players with fewer than 20 on-ice goals.

## Tests

- No teammate goal: the shares are the prior.
- An unassisted goal adds to `G_on` only.
- One assist and two assists.
- No player assists on their own goal.
- A goal at a line change goes to the players before the change.
- Future-invariance (H5).

## Calls made

- A beta-binomial share of on-ice goals, because it separates the player's share from how many goals the line scores.
