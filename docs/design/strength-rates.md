# Even-strength and special-teams rates (#41)

Uses H1 to H5, H7, H8 of [research-harness.md](research-harness.md).

## Goal

Split each team's scoring into even strength, power play, and penalty kill, each with its own ice time.
Match a team's power play with the opponent's penalty kill.

## Phase 1: MoneyPuck situations (ready after the MoneyPuck situations table)

[moneypuck-situations.md](moneypuck-situations.md) gives, for each team game and situation (`5on5`, `5on4`, `4on5`, `other`): `iceTime` in seconds, `xGoalsFor`, `xGoalsAgainst`, `goalsFor`, `goalsAgainst`.
`other` holds 4v4, 3v3, 5v3, 3v5, and empty-net play. Phase 1 treats it as one class at the league rate.

For each team, before the cutoff, over the last `RATE_WINDOW` games:

- `ev_off = (sum xgf_5on5 + K_EV * league_ev) / (sum t_5on5 + K_EV)`, a rate per second. `ev_def` uses `xga_5on5`.
- `pp_off` from `5on4` xGF and time. `pk_def` from `4on5` xGA and time.
- `league_*` is the league rate of the same class over the training seasons. `K_EV` and `K_ST` are prior weights in seconds.

Matched expected goals of the home team in a game:

`mu_home = T_EV * ev_off_h * ev_def_a / league_ev + T_PP * pp_off_h * pk_def_a / league_pp + T_OTHER * league_other`

- `T_EV`, `T_PP`, `T_OTHER` are the league average seconds per team game of each class in the training seasons. Phase 1 does not predict penalties; [penalties.md](penalties.md) (#57) can replace `T_PP` later.
- The away team is symmetric.
- The three times sum to the average game length of the training seasons. Test that.

Choose `RATE_WINDOW`, `K_EV`, and `K_ST` on `SELECT_TRAIN` by the count log score of team goals.

Feature `strength_xg`: `log(mu_home / mu_away)`.
A team with no situation rows gives `NEUTRAL_FORM` and a `DegradedInput`.

Compare (H3, reference `xg_form, rest`):

1. `strength_xg, rest`
2. `xg_form, strength_xg, rest`

Count report (H4): `mu_home` and `mu_away` as goal means against today's means.

## Phase 2: stints (after #35)

With the stints of [pbp-shift-dataset.md](pbp-shift-dataset.md), split `other` into 4v4, 3v3, two-man advantages, and empty net.
Compute time per class from stints (team clock time, never summed player time).
Leave empty-net time out of the strength rates, and model it in [predictive-distributions.md](predictive-distributions.md) instead.
Phase 2 adds classes. It keeps the phase 1 interface.

## Tests

- A hand-made team history with known times and xG gives the hand-computed rates.
- Doubling a team's PP time with the same PP xG halves `pp_off`.
- Swapping home and away swaps `mu_home` and `mu_away`.
- A team with no 5on4 time has `pp_off` equal to the league rate.
- The class times sum to the game length.
- Future-invariance (H5).

## Calls made

- MoneyPuck situation rows for phase 1, because they hold the time on ice by strength that the store lacks, with no new source.
- League average strength times in phase 1, so no realized penalty minutes of the game enter a pregame forecast.
- The multiplicative match (offense times defense over the league rate), the usual form for two rates.
