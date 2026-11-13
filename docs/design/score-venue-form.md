# Score- and venue-adjusted xG form (#40)

Uses H1 to H5, H7, H8 of [research-harness.md](research-harness.md).

## Goal

Test whether xG form that removes score effects predicts better than the raw `xg_form`.
A trailing team shoots more, so raw xG share mixes team strength with game state.

## Two phases

### Phase 1: MoneyPuck's adjusted xG (ready after the MoneyPuck situations table)

[moneypuck-situations.md](moneypuck-situations.md) stores `scoreVenueAdjustedxGoalsFor` and `...Against` for each team game.
MoneyPuck computes them with its own score and venue weights.

Register feature `xg_form_sva`: the same reader and the same `FORM_WINDOW` share as `xg_form`, on the adjusted columns of the `all` situation.
Add a reader `mp_team_sva_form_before(conn, team, cutoff, count)` in `store/reads.py`, in the shape of `mp_team_form_before`.

Compare, with the reference `rest` plus each form (H3, `reference_features: ["xg_form", "rest"]`):

1. `xg_form, rest` (reference)
2. `xg_form_sva, rest`
3. `xg_form, xg_form_sva, rest`

The adjusted values come from MoneyPuck's weights, fitted on all seasons. `MONEYPUCK_LEAK_NOTE` applies, and the report says so.

### Phase 2: our own weights (after #35 and #50)

Phase 2 needs a per-shot xG from [shot-model.md](shot-model.md) (#50) on the events of [pbp-shift-dataset.md](pbp-shift-dataset.md) (#35).
Aggregate game xG cannot be split into events, so phase 2 does not start before both exist.

1. For each unblocked attempt at 5v5, the state is `(score difference before the event, capped at +3 and -3, from the shooting team's view; home or away)`. Use the score before the event: a goal's own state is the score before it.
2. Conventional weights (McCurdy): for each state `s`, the home weight is `share_all / share_home(s)`, where `share` is the home share of the xG of the training seasons. The away weight uses the away share in the same way.
3. Talent-controlled weights: fit `statsmodels` `GLM` Poisson on 5v5 xG per team-stint, with `exposure` = stint seconds, columns for each state, and an `offset` of the team's pregame `rating_xg` from [team-ratings.md](team-ratings.md). The weight of state `s` is `exp(-beta_s)`.
4. Shrink a state with fewer than `MIN_STATE_SECONDS` of exposure toward the next state nearer to a tie.
5. Normalize so the weighted league total equals the raw league total in the training seasons.
6. Fit the weights on the seasons before the scored season. Never use a full-season weight in the season it scores.

Features: `xg_form_sv_ours` (conventional) and `xg_form_sv_talent` (talent-controlled), each a `FORM_WINDOW` share as today.

## Home ice

The Elo already carries home ice. A venue weight changes home and away xG in opposite ways.
Run one ablation with the venue part removed (score weights only), and report both.

## Tests

- A goal uses the score before it.
- Swap home and away in a game: the adjusted shares swap.
- Normalized totals equal the raw totals on the training seasons.
- A state with no exposure gets the parent weight.
- A missing adjusted value is NULL and gives `NEUTRAL_FORM` and a `DegradedInput`, never zero xG.
- Future-invariance (H5).

## Calls made

- Phase 1 first, because MoneyPuck already ships an adjusted column, and the test costs one reader.
- Phase 2 waits for a local per-shot xG, so no aggregate value is split into events by invention.
