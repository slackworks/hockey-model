# Season simulation and playoff odds (#60)

Uses H1, H4, H5, H8 of [research-harness.md](research-harness.md).
The draw shape is the one of [outcome-contracts.md](outcome-contracts.md) (#83).

## Goal

Simulate the rest of a regular season from one cutoff and return each team's playoff qualification chance, with a Monte Carlo error.

## Rules (verify each season)

These are the rules since 2019-20. The builder MUST check them against NHL.com for each season the code supports, and save the links in the module docstring.

1. Points: 2 for a win, 1 for an OT or shootout loss, 0 for a regulation loss.
2. Final standings by points. Ties break in this order: regulation wins (RW), regulation and OT wins (ROW), total wins, points in head-to-head games (with the odd-game rule: when two teams played an odd number of games, the extra home game of the team with more home games does not count), goal difference, goals for.
3. Before 2019-20, ROW came first and RW did not exist. A season before 2019-20 uses its own list.
4. Format since 2013-14: two conferences of two divisions. The top 3 of each division qualify, then the 2 best other teams of each conference (wild cards).
5. 2019-20 (pandemic stop and a 24-team format) and 2020-21 (division-only schedule, 4 per division) raise `UnsupportedSeasonError` that names the season.
6. A tiebreak that needs a field the simulation does not track raises, never guesses.

Put the rules in `season_model/rules.py` as data: `SeasonRules(season, points, tiebreaks, divisions, qualifiers_per_division, wild_cards_per_conference)`.
The divisions of each season come from a table in that module, checked against the stored games (each team plays in one division).

## Inputs at a cutoff

`SeasonCheckpoint`: the season, the cutoff, the final games before it (with `last_period` for RW and ROW), and the games not yet played.
Future games come from the stored schedule. For a live forecast, refresh the score day of each remaining date of the season first (the existing NHL score source, about 180 requests once). A postponed game stays in the remaining list. A game with no date raises.

## One draw

1. Sample team strength once per draw: each team's Elo rating at the cutoff plus `Normal(0, SIGMA_RATING)`. `SIGMA_RATING` is chosen on `SELECT_TRAIN` checkpoints by the qualification log score.
2. For each remaining game in date order: `p` = Elo home win chance from the sampled ratings. Regulation tie chance `t = TIE_RATE` (the training share of games that went past regulation). `q` = the training share of OT and shootout games won by the home team. Then P(home regulation win) = `p - t * q`, P(tie) = `t`, P(away regulation win) = `1 - p - t * (1 - q)`. When a regulation chance comes out below 0 (a very lopsided game), set it to 0 and renormalize, and count it.
3. When #52 supports `regulation_result`, use its regulation and OT probabilities instead of steps 2's constants.
4. Update points, RW, ROW, wins, head-to-head points, goals for and against. For goals, draw each team's regulation goals from Poisson with the model's means, conditioned on the drawn result by rejection (draw until the scores match the result, at most 100 tries, then use 1-0 or a tie at 2-2).
5. Apply the qualification rules.

Qualification chance = qualified draws / valid draws. `mc_error = sqrt(p (1 - p) / n)`.
Use `numpy.random.default_rng(seed)`. Default `n = 10000`.

## API

- `season_model/simulate.py`: `simulate_season(conn, season, cutoff, *, n, seed) -> SeasonDraws` (points, ranks, qualified flags per draw and team).
- `StorePlayoffModel(conn, as_of, seed)` implements `StandingsModel.playoff_odds(team_code, standings, level)`. The `standings` argument is ignored (the store is the source), and the docstring says so. `Projection.point` is the chance, and `low`/`high` are the `level` interval of the Monte Carlo error, labeled as Monte Carlo error only.
- Capability `season` in the matrix of #83 becomes supported when the report below is accepted.

## Report

Checkpoints on the 1st of December to April of each exploration season (except 2019-20 and 2020-21). Per team: the Brier score and log score of qualification, against two baselines: the current points percentage rank (qualified if in the top 16 by points percentage) as a 0-or-1, and a 50% prior.
Calibration of all checkpoints together in 10 bins. The Monte Carlo error of each chance.

## Tests

- A season with no remaining game gives 0 or 1 for each team, and equals the real outcome.
- A clinched team gives 1, an eliminated team 0.
- A three-team tie resolved by each tiebreak in turn.
- The odd-game head-to-head rule.
- 2019-20 raises `UnsupportedSeasonError`.
- The qualified count of every draw is 16.
- The same seed gives the same draws.
- No game after the cutoff enters the checkpoint (H5).

## Calls made

- Elo plus a sampled rating error for strength, because it is the shipped model and its error can be fit on checkpoints. A swap to the live fit is a later change.
- Constant tie and OT rates until #52 gives model-based ones.
