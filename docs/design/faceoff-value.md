# Faceoff value in context (#58)

Uses H1 to H6, H8 of [research-harness.md](research-harness.md).
Needs [pbp-shift-dataset.md](pbp-shift-dataset.md) (#35), and the per-shot xG of [shot-model.md](shot-model.md) (#50) for the main outcome.

## Goal

Test whether a team's faceoff skill, weighted by where and when its draws happen, adds to the forecast beyond raw faceoff percentage.
Published estimates are small: Desjardins puts an even-strength end-zone win at about 2.5 goals per 100 extra wins. An inconclusive result is likely, and it is a valid one.

## Value of a win, by cell

1. A faceoff record: game, time, zone (from the winner's view: offensive, neutral, defensive), strength (from the winner's view: EV, PP, SH), winner, loser.
2. The window ends at the earliest of the next faceoff, the period end, and `WINDOW_CAP_S = 45` seconds after the draw.
3. The outcome is the xG of the winner's team minus the xG of the other team in the window. With no #50 xG, use unblocked attempts as a fallback, and label it.
4. The value of cell `c` is `v_c = mean(outcome | draw won by the team) - mean(outcome | draw lost by the team)`, from the same team's view, on the training seasons.
5. Shrink each `v_c` toward the zone's value over all strengths with a normal empirical-Bayes weight: `v_c' = (n_c * v_c + K * v_zone) / (n_c + K)`, with `K` fit by the method of moments on `SELECT_TRAIN`.

The two teams see the same draw from two sides. Check the conservation: the winner's outcome is the loser's outcome with the sign changed, in every window.

## Faceoff skill

A Bradley-Terry model of who wins a draw: `logit P(i beats j) = a_i - a_j + z_c`, with `z_c` a zone and strength term.
Fit with `sklearn.linear_model.LogisticRegression` (L2, no intercept) on a sparse design: `+1` in the column of the player who took the draw for the home team, `-1` for the away player, label 1 when home won, plus one-hot cell columns.
Refit weekly (`REFIT_EVERY_DAYS = 7`) on the seasons before plus the current season so far.

Team skill for a game: the mean `a_i` of the team's centers, weighted by each center's share of the team's draws in the last 10 games.

## Feature

`faceoff_value = sum_c E[draws_c] * (2 * P_c - 1) * v_c' / 2`, for home minus away, where:

- `E[draws_c]` is the team's average draws per game in cell `c` over the last 20 games (no future draw counts).
- `P_c` is the team's win chance against the opponent's centers from the Bradley-Terry fit.

Baselines: `faceoff_pct` (the raw FO% difference over the last 20 games), and no faceoff term.

## Report

1. The cell value table with counts and intervals (descriptive).
2. The winner test (H3): reference `xg_form, rest`, against `+ faceoff_pct` and `+ faceoff_value`.
3. The teammate and opponent problem: the outcome after a draw also depends on the players on the ice. Report the correlation of `faceoff_value` with `xg_form`, and say that the cell value is not credit for the center alone.

## Tests

- The window ends at the next faceoff, at the period end, and at the cap.
- The conservation check on a hand-made game.
- An offensive-zone win for one team is a defensive-zone loss for the other.
- A team with no draws gives `NEUTRAL_FORM` and a `DegradedInput`.
- Future-invariance (H5).

## Calls made

- A 45-second cap, near the published windows (20 seconds in one study, a full shift in another), so a long possession after a draw does not count as faceoff value.
- Bradley-Terry with scikit-learn, the standard pairwise model, because it adjusts for the opponent center.

## Sources

- Desjardins, G., as summarized by Arctic Ice Hockey: "Impact of Winning an Offensive Zone Faceoff, Even-Strength vs Power-Play".
- "Winning Is Not Everything: A contextual analysis of hockey face-offs" (2019, MIT Sloan). arXiv 1902.02397.
