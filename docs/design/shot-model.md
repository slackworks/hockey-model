# Local shot-quality model (#50)

Uses H1, H4 to H6, H8 of [research-harness.md](research-harness.md).
Needs [pbp-shift-dataset.md](pbp-shift-dataset.md) (#35). Read [recording-drift.md](recording-drift.md) (#37) part B first.

## Goal

Fit our own expected-goals model, trained for each season on earlier shots only, and compare it with MoneyPuck's xG.
This removes the leak that `MONEYPUCK_LEAK_NOTE` describes, for the seasons it scores.
MoneyPuck stays the live source and keeps its credit until this model earns a replacement.

## Shot rows

From `latest_pbp_events`, one row for each unblocked attempt: `goal`, `shot-on-goal`, `missed-shot`.

- Leave out shootout events, penalty shots (a separate small model is out of scope), and attempts at an empty net (`goalie_in_net` NULL). Count each group in the report.
- Leave out a row with NULL `x`, `y`, or `attack_x_sign`, and count it by season.
- The target is 1 for a goal.

## Features

Normalize the coordinates so the shooter attacks the net at `(89, 0)`: `x' = x * attack_x_sign`, `y' = y * attack_x_sign`.

| Feature | Definition |
| --- | --- |
| `distance` | `hypot(89 - x', y')` |
| `angle` | `atan2(y', 89 - x')` in radians, signed |
| `shot_type` | the feed value, one-hot; a missing value is its own category |
| `strength` | from `situation_code`, from the shooter's view: `EV`, `PP`, `SH`, `OTHER` |
| `rebound` | the previous event is an unblocked attempt by the same team within 3 seconds, same period, no stoppage between |
| `rush` | the previous event is in the shooter's neutral or defensive zone within 4 seconds |
| `since_prev` | seconds since the previous event, capped at 60 |
| `prev_type` | the previous event's type, one-hot |
| `prev_distance` | distance between this event and the previous one, NULL when either has no coordinates |
| `score_diff` | shooting team minus other team before the event, capped at plus and minus 3 |
| `period` | 1, 2, 3, or OT |

No field that reveals the result: no goal flag, no score after the event, no assist.

## Models

Build both with a `sklearn.pipeline.Pipeline` of a `ColumnTransformer` (`OneHotEncoder(handle_unknown="ignore")`, `StandardScaler`, and `SplineTransformer(n_knots=6)` on `distance` and `angle`) and `LogisticRegression(C=C, max_iter=2000)`:

- G: geometry only (`distance`, `angle`, `shot_type`, `strength`).
- C: every feature in the table.

The current `game_model/logistic.py` is a game fit with an Elo term. Do not use it for shots.

For each season `s` from 2015-16, fit on the seasons before `s` and predict `s`.
Choose `C` from `{0.01, 0.1, 1, 10}` on `SELECT_TRAIN` (fit 2014-15 to 2019-20, score 2020-21 and 2021-22).

## Storage

Schema change through `store upgrade`:

- `shot_model_fits (fit_id INTEGER PRIMARY KEY, model TEXT, trained_through INTEGER, params TEXT, created_at TEXT)`. `params` is JSON of the pipeline settings, the encoder categories, and the coefficients, so a fit can be checked without pickle.
- `shot_xg (fit_id INTEGER, game_id INTEGER, event_id INTEGER, xg REAL, PRIMARY KEY (fit_id, game_id, event_id))`.

Do not store a pickle. Rebuild the pipeline from `params` and assert that it gives the same probabilities as the fitted object on 1,000 rows.

## Report

1. Shot log loss, Brier score, and calibration (10 bins) of G and C by season, strength, and era (the #37 boundary).
2. An ablation: C minus each feature group in turn.
3. MoneyPuck comparison: per team game, local xG for and against beside MoneyPuck's. Correlation and mean difference by season.
4. Downstream: feature `xg_form_local`, the `FORM_WINDOW` share from local xG, against `xg_form` (H3, reference `xg_form, rest`).
5. Fit time and store size.

## Tests

- Rotation: a shot at `(-80, 10)` with `attack_x_sign = -1` gives the same distance and angle as `(80, -10)` with `+1`.
- `angle` is 0 straight in front and changes sign across the center line.
- A NULL coordinate removes the row and counts it.
- `rebound` and `rush` on hand-made sequences, with a stoppage between.
- The same data gives the same coefficients (`LogisticRegression` with `lbfgs` is deterministic).
- A shot of a later season does not change an earlier fit (H5).

## Calls made

- scikit-learn for the pipeline: encoders, splines, and a regularized logistic fit are standard parts, and it is fast on a million rows.
- JSON parameters instead of pickle, so a stored model is readable and safe to load.
- Empty-net and penalty-shot attempts out of the model, as their own small groups.
