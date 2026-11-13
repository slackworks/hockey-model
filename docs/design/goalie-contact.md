# Goalie contact, freezes, and posts (#48)

Uses H1, H4 to H6, H8 of [research-harness.md](research-harness.md).
This issue is a measurement study first. It ships no feature unless the study finds one that transfers to future games.

## Goal

Find which shot outcomes the feeds can measure (freeze, play stopped, play continued, post or crossbar), and test whether a goalie's residuals on them predict future goals against beyond GSAx.

## Phase 1: MoneyPuck outcomes (ready after the MoneyPuck situations table)

The goalie file of [moneypuck-situations.md](moneypuck-situations.md) gives `freeze`/`xFreeze`, `playStopped`/`xPlayStopped`, and the play-continued pairs for each goalie game and situation.

1. Coverage table: for each season and situation, the count of goalie games and the share with a non-null value of each pair.
2. Residuals: `freeze - xFreeze` and `playStopped - xPlayStopped`, shrunk with the model of [goalie-shrinkage.md](goalie-shrinkage.md) with the expected count as the exposure.
3. Transfer test: on `SELECT_TRAIN`, regress each goalie's next-season GSAx per xGA on the shrunk residual and the #45 posterior mean (`statsmodels` WLS, weight = next-season xGA). Do it for `5on5` and for `all` apart. Report the coefficient, its interval, and the goalies who changed teams as their own slice.
4. A residual whose interval holds zero in both situations stops here, and the report says so.

## Phase 2: posts and crossbars (after #35)

1. Read the `reason` of missed-shot events in the play-by-play fixtures. List every value that the feed uses in each season, with counts, in the report. Do not assume a value list.
2. Map only the values that name a post or a crossbar to `post`. Every other miss reason stays as it is. A miss with no reason is `unknown`.
3. Coverage by venue and season. A venue whose `post` share is far from the league share is a scorer effect, and the report flags it.
4. Expected posts: `sklearn.linear_model.LogisticRegression` on geometry, shot type, and strength from earlier seasons. Residual and transfer test as in phase 1.

## Promotion

Only a residual that passes the transfer test in phase 1 or 2 becomes a feature (`goalie_<outcome>_eb`), and it then goes through the winner test of H3 with the reference `xg_form, rest, goalie_gsax_eb`.

## Tests

- Each mapping of a miss reason, plus an unknown reason.
- A post followed by a goal in the same sequence: the post is a post, the goal is a goal.
- A residual sign: more freezes than expected is a positive residual.
- No exposure gives the prior.
- Future-invariance (H5).

## Calls made

- A measurement study with a transfer gate, because the source research reports a failed all-situations transfer.
- No inferred outcome: an absent field stays unknown.
