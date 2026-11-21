# Attempt-form leak test: frozen protocol

This document fixes the test and the decision rule before the run. It answers issue #77.

## Question

The first backtest scored the model with `xg_form`, a form built from MoneyPuck expected goals.
MoneyPuck's expected-goals model may have trained on the scored games.
This test replaces `xg_form` with a form built from raw shot-attempt counts, which have no model behind them.

## Hypothesis

The gain over Elo comes from the shot-attempt share as much as from the expected-goals share.

## Fixed inputs

- Candidate set: `attempt_form`, `goalie_gsax`, `rest`. Only `xg_form` is replaced.
- `attempt_form` is the Corsi share: `shotAttemptsFor / (shotAttemptsFor + shotAttemptsAgainst)`, averaged over the same last 10 games as `xg_form`.
- Everything else is unchanged: the seasons, the refit before each scored season, the guess veto, and the success rule.
- The joint test stays one test. The v1 success rule does not change.
- The scored seasons do not pick between the two forms.

## Reference

The first run gave a model minus Elo log-loss difference of -0.0113, with CI -0.0144 to -0.0083, on 2,792 games (2024-25 and 2025-26).

## Decision rule

Let `d` be the model minus Elo log-loss difference of the attempt run, on the same games.
Let `d_xg` be the difference of the xG run on the rebuilt store, which must match -0.0113.

- Recovers most: `d <= (2/3) * d_xg`. The xG gain is real.
- Collapses: `d > (1/3) * d_xg`. The leak did the work.
- Between the two: the result is inconclusive and is reported as such.

The rule applies to the point estimate.
The CI is reported next to it and does not change the call.

## Not in scope

- Fenwick counts (`unblockedShotAttemptsFor` and `unblockedShotAttemptsAgainst`) are stored, but no feature uses them in this run.
- `goalie_gsax` still uses MoneyPuck expected goals. A collapse therefore cannot be read as the whole leak, because that feature may carry the same leak.
- The Vince comparison against the DraftKings no-vig line runs outside this repository.

## Result

Both runs used the same 2,792 games, the same seasons, and the same code, and differed only in the form feature.
The rebuilt store reproduces the first run: the xG run gives a log loss of 0.676326 against 0.687598 for Elo.
Its difference is -0.011272, with CI -0.014446 to -0.008337.

The attempt run gives a log loss of 0.675631 against 0.687598 for Elo.
Its difference is -0.011967, with CI -0.015398 to -0.008810.
The attempt difference is 1.06 times the xG difference.

Decision rule: the attempt gain is at least two thirds of the xG gain, so the outcome is "recovers most".
The xG gain is not explained by the leak that the test checks for.

On the validation seasons (2022-24), Elo scores 0.662759, Elo with `xg_form` scores 0.661119, and Elo with `attempt_form` scores 0.659134.

`goalie_gsax` was vetoed in both runs, so neither tested model used a MoneyPuck goalie feature.
This test therefore says nothing about that feature.

Limits of this result:

- The attempt counts come from the same MoneyPuck file as the xG values. The test removes the MoneyPuck expected-goals model, not the MoneyPuck data source.
- The two differences were not tested against each other with a paired interval. The point estimates decide the call, as the protocol says.
- This result does not change the separate comparison with the DraftKings no-vig line, which runs outside this repository.

Outputs: `~/.cache/hockey-model/results-attempts/` (`xg.json`, `attempt.json`, and the per-game CSVs).

## Reproduce

The attempt candidate set and its CLI option were removed after this result (#93).
Commit `9e15efb` is the last revision of `main` that holds them.
Check out that commit, rebuild the store as the protocol says, and run `hockey-model backtest --candidates attempt --games-out PATH` to repeat the attempt run.
The same command with `--candidates xg` repeats the xG run.

The result files are on the machine of the owner, in `~/.cache/hockey-model/results-attempts/`.
Their SHA-256 sums are:

| File | SHA-256 |
| --- | --- |
| `xg.json` | `6541a135089037dbf3656a9d5d202d2c1514df23f1a88c07e89ea4f220e5954c` |
| `attempt.json` | `aa1efcf3089ea43b24a0f69f44b183747bacd05121833009f2406732f4ba910e` |
| `xg-games.csv` | `2080b80f1e923247ff477f3b5af6a25568ead445b556d1f32ee727a0e847ab6e` |
| `attempt-games.csv` | `9a7672d98090f3b752ea432a5dd94bd7f8c5bf54e032fd530e250d6e55636b46` |
