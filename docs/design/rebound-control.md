# Goalie rebound control (#47)

Uses H1 to H5, H7, H8 of [research-harness.md](research-harness.md).
Follows [goalie-shrinkage.md](goalie-shrinkage.md) (#45): the baseline is the shrunk GSAx.

## Goal

Test whether a goalie who gives fewer rebounds than expected allows less in future games, beyond GSAx.
Split-half repeatability is not enough. The test is future xGA and the winner.

## Phase 1: MoneyPuck rebounds (ready after the MoneyPuck situations table)

The goalie file of [moneypuck-situations.md](moneypuck-situations.md) gives `rebounds` and `xRebounds` for each goalie game, by situation.
MoneyPuck's `xRebounds` is the expected count from MoneyPuck's own model.

1. Residual for each game: `xRebounds - rebounds` (rebounds prevented).
2. Shrink with the model of #45, with `xRebounds` as the exposure: `S = sum w (xreb - reb)`, `X = sum w xreb`, posterior mean `(S + K m) / (X + K)`. Fit `m` and `K` by the same likelihood on `SELECT_TRAIN`, in its own script.
3. Feature `goalie_rebound_eb`: home starter minus away starter.
4. Validation before the winner test: does the shrunk residual predict the goalie's next 20 games of xGA per shot on goal, beyond the #45 posterior? Fit `statsmodels` OLS of next-20 xGA rate on both, on `SELECT_TRAIN`, and report the coefficient and its interval.
5. Winner test (H3): reference `xg_form, rest, goalie_gsax_eb` against the same plus `goalie_rebound_eb`.

MoneyPuck's definitions and its xG model apply. `MONEYPUCK_LEAK_NOTE` applies.
Read [recording-drift.md](recording-drift.md) (#37) first: the rebound share moves with the recording of missed attempts, so pool only the seasons that #37 finds stable.

## Phase 2: our own definition (after #35 and #50)

1. An opportunity is a saved or missed unblocked attempt, not in a shootout, not at an empty net.
2. A rebound is the next unblocked attempt by the same team within `REBOUND_WINDOW_S = 3` seconds, in the same period, with no stoppage, faceoff, or goal between. The 3-second rule is the common public definition (War-on-Ice).
3. An opportunity whose next event is missing from the feed is "unlinked", not a non-rebound. Count it apart.
4. Expected rebound: `sklearn.linear_model.LogisticRegression` on the geometry, shot type, and strength of the first attempt, with no post-save field, trained on earlier seasons.
5. Audit 100 opportunities by hand against the play sequence, and write the agreement rate in the report.

## Tests

- An attempt 2 seconds after a save is a rebound, at 4 seconds it is not.
- A stoppage or a faceoff between ends the chance.
- An attempt in the next period is not a rebound.
- A missing next event is unlinked.
- The shrinkage tests of #45 for the rebound prior.
- Future-invariance (H5).

## Calls made

- Phase 1 on MoneyPuck's rebound columns, because they exist for every goalie game now.
- The 3-second window in phase 2, as the common public definition.
