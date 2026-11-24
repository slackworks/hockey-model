# Goalie-guess veto rerun, November 2026

The veto of `goalie_gsax` holds on the #66 goalie definition.
This run is exploratory.
It uses seasons that earlier runs already spent, so it is not a confirmation and it does not count as a look.
The record is `experiments/0003-goalie-guess-rerun.json`, cohort `exploration` (#143).
An exploration record holds no result, so the registered record stays as it was and this report holds the result.

## Why this run

#66 changed the goalie differential.
A one-sided goalie now gives `NEUTRAL_FORM`, not the rate of the other side.
The first veto report (`2026-11-nhl-game-model.md`) was run before that change.
That report stays as it is.

## What was run

- The command was `hockey-model backtest --workers 4 --games-out PATH` on a copy of the real store.
- The candidates were the original three: `xg_form`, `goalie_gsax`, and `rest`.
- The plan was the default plan: the selection validation seasons are 2022-23 and 2023-24, and the scored seasons are 2024-25 and 2025-26.
- The veto rule is the rule of the original spec: a candidate is vetoed when its guess-mode validation log loss is above the Elo alone by more than the veto tolerance.
- The run took 1350 seconds with 4 workers.

## Result

Log loss on the validation seasons (lower is better). The old columns are from `2026-11-nhl-game-model.md`.

| Model | Old actual | New actual | Old guess | New guess |
| --- | --- | --- | --- | --- |
| elo | 0.66276 | 0.66276 | 0.66276 | 0.66276 |
| elo + xg_form | 0.66112 | 0.66112 | 0.66112 | 0.66112 |
| elo + rest | 0.66161 | 0.66161 | 0.66161 | 0.66161 |
| elo + goalie_gsax | 0.66425 | 0.66422 | 0.66339 | 0.66326 |

The veto decision did not change.
The guess-mode log loss of `elo + goalie_gsax` is above the Elo alone, by 0.00050.
The guess-mode loss fell by 0.00013 from the old run, so the #66 change helps a little and does not close the gap.
The live features stay `xg_form` and `rest`.

The scored result is the same as the old one.
The 2792 scored games give a log loss of 0.67633 for `model_actual` and for `model_guess`, against 0.68760 for Elo.
The 90% interval of the difference to Elo is -0.01445 to -0.00834.
A game by game comparison with the old per-game file shows the same games in the same order.
The largest difference in any win probability is 2.2e-16, which is rounding.
This agrees with the old report: no live feature reads the starting goalie, so the #66 change does not reach the scored runs.

## What this result does not show

- The seasons are spent. The first veto, the first scored result, and this run all read the same games, so no run here is a fresh test.
- The result is exploratory. It gives a reason to keep the veto. It does not confirm the veto.
- The MoneyPuck caveat of the old report still applies. MoneyPuck data is of today's model and may have trained on later games.

## Inputs

- The code of the run is local commit `f05bd69`, tree `8eb67382802c299c6ad130fadca81f73c4b3a759`. The tree stays the same when the commits are signed again, and the commit hash does not.
- The store is a copy of the real store file that was last written on 2026-11-03. The copy was upgraded from schema version 1 to the current version with `hockey-model store upgrade`. The original file was not changed.
- The SHA-256 of the original file is `5050eed3273cbe43cff5b602094f7960898a7f0448cf26ad7035c35529f5e450`.
- The SHA-256 of the upgraded copy is `cdb7d5d24a8be46d535b50e10677828a6d6a57209b0b80876bb453eff9f8530a`.

## Artifacts

The files are in `~/.cache/hockey-model/results/` on the machine of the run.

| File | SHA-256 |
| --- | --- |
| `goalie-guess-rerun-backtest.json` | `489556cf4a746728115a007b9668ee4ae1c0c871c18f8cfe673735846ed2597d` |
| `goalie-guess-rerun-games.csv` | `2080b80f1e923247ff477f3b5af6a25568ead445b556d1f32ee727a0e847ab6e` |

## Next step

The veto did not flip, so no issue for a confirmation look is needed.
If a later change flips the veto, do not change the live features.
That change needs a confirmation look under #38, so file an issue for it then.
