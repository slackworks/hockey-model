# Aging adjustments (#54)

Uses H1, H4, H5, H8 of [research-harness.md](research-harness.md).
Needs [player-forecasts.md](player-forecasts.md) (#53).

## Goal

Estimate how a skater's goal and assist rates change with age, with players who leave the league in the analysis, and test whether an age term improves the #53 forecasts.

## Data readiness

The store has no birth date.
The NHL player landing endpoint (`https://api-web.nhle.com/v1/player/{player_id}/landing`) returns `birthDate`, on the host the store already reads.
The owner approved this endpoint on 2026-11-13.

- New source `nhl_player` in `sources/nhl.py`: parse `birthDate` (ISO date) and `position`. A missing or malformed date raises `SourceParseError` with the full message.
- New table `player_bio (nhl_id, birth_date, position, fetched_at)` through `store upgrade`. One read per player that has a `skater_games` row and no `player_bio` row: about 3,000 requests once, then a few per week.
- Age at a date: whole years, with the birthday counted on its own date. Write `age_on(birth_date, day) -> float` as years plus the fraction of the year since the last birthday.

## Method

1. Player-seasons with at least `MIN_TOI_HOURS = 5` of ice time. Rate = count / hours, per stat.
2. Pairs of consecutive seasons of one player. The change is `log((Y2 + 0.5) / E2) - log((Y1 + 0.5) / E1)`, with weight `1 / (1 / E1 + 1 / E2)` (the harmonic exposure).
3. Fit the change on age at the first season with `statsmodels` WLS on a `sklearn.preprocessing.SplineTransformer(n_knots=5, degree=3)` basis of age, by position group. This is the delta method in a smooth form.
4. Attrition bounds. A player who plays a season and not the next has no pair. Add each such player twice in two sensitivity fits: once with a next-season rate at the 10th percentile of the position (lower bound), once with no change (upper bound). Report the curve of the main fit and of both bounds.
5. Fit on the seasons before each scored season only.

## Forecast test

Add an age multiplier to the #53 posterior mean: `rate * exp(curve(age_next) - curve(age_now))`, applied at a season boundary only.
Compare with #53 alone on the next-season count log score, by stat and position.
A stat where the age term does not help, or where its sign differs between leave-one-season-out fits, keeps the no-age forecast. Write a decision for each stat.

The #53 decay already discounts old seasons. Report the result with and without the decay, to show any double count.

## Tests

- `age_on` the day before, on, and after a birthday.
- A missing birth date gives the no-age forecast and a note.
- A simulated league with a known age curve and attrition of weak old players: the main fit is biased, and the bounds hold the true curve.
- Future-invariance (H5).

## Calls made

- A spline delta method with attrition bounds, instead of a full model of who leaves, because the bounds show the size of the survivor bias with no extra model.
