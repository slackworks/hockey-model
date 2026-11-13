# Playoff rounds and the Cup (#61)

Uses H1, H4, H5, H8 of [research-harness.md](research-harness.md).
Needs [season-simulation.md](season-simulation.md) (#60). Uses [playoff-calibration.md](playoff-calibration.md) (#44) only if it is promoted.

## Goal

From each season draw of #60, play out the bracket and return the chance that each team reaches and wins each round, and the Cup.

## Rules (verify each season)

Since 2013-14, the builder MUST check them against NHL.com and save the links:

1. In each division: the division winner with more points plays the conference's second wild card; the other division winner plays the first wild card. Second and third of each division play each other.
2. The bracket is fixed within a division side. There is no reseeding.
3. The team with more points has home ice in a series (tiebreaks of #60). In the Final, the same rule across conferences.
4. Every series is best of seven, home pattern 2-2-1-1-1.
5. Playoff OT is sudden death with no shootout.

2019-20 and 2020-21 raise `UnsupportedSeasonError`.

## One draw

1. Take the qualifiers, the seeds, and the sampled team ratings of the #60 draw. Do not draw qualification again.
2. For each game of each series: `p` = Elo home win chance from the sampled ratings (with the #44 layer when promoted). Draw the winner.
3. A series ends at 4 wins.

For an in-progress postseason at the cutoff, start each series from its stored result, and skip a finished series.

## Output

Per team: P(qualifies), P(wins round 1), P(wins round 2), P(wins the conference final), P(wins the Cup). Each with its Monte Carlo error.
Name each event "wins round k", never "reaches round k", so no event name is unclear.
A conditional chance (P(wins the Cup | qualifies)) is given only when its denominator is above 0.

Capability `playoff_bracket` of #83 becomes supported when the report below is accepted.

## Report

On exploration postseasons (except 2019-20 and 2020-21): at the start of each postseason, the series log loss of the bracket model's series chances against the actual winners, against a 50% baseline and against a points-rank baseline. About 15 series per season, so the interval is wide.

## Tests

- A fixed set of sampled ratings with one team much stronger: it wins nearly every series.
- The Cup chances of all teams sum to 1 in every run.
- P(wins round k+1) <= P(wins round k) <= P(qualifies) for every team.
- The home pattern 2-2-1-1-1.
- A series at 3-3 at the cutoff ends in one game.
- The same seed gives the same draws.

## Calls made

- The same sampled ratings as #60 in each draw, so the season and the bracket share their uncertainty.
