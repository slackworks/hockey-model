# Promotion bar and cohort cutoff

## Problem

A confirmation record promotes a feature when the result interval lies below zero.
A fixed interval lets a team look at the same games again and again until one look clears it.
Every confirmation record scores the games after its own cutoff, in the same open-ended seasons.
Two confirmation records therefore score overlapping games.

## Rule

`look_number` counts the looks at the confirmation games.
A confirmation record MUST set `look_number` to one more than the count of confirmation records with a lower registry number.
An exploration record does not count as a look, and the checker does not read its `look_number`.
The checker and `hockey-model experiments run` refuse a record that breaks this rule.

The promotion bar for look `k` is the interval from the `BASE_TAIL / k` percentile to the `1 - BASE_TAIL / k` percentile of the block bootstrap.
A record promotes only when the upper end of that interval lies below zero.
The names `BASE_TAIL` and `MAX_LOOKS` are in `src/hockey_model/experiments/looks.py`.

This schedule is harmonic, not a Bonferroni correction.
A Bonferroni correction needs a total look count that is fixed before the first look.
The harmonic schedule does not need that count.
Its limit is weaker: by the union bound, the chance that any of `n` looks promotes a feature with no effect is at most `BASE_TAIL` times the `n`-th harmonic number.
That is about one and a half times `BASE_TAIL` at two looks, and about three times `BASE_TAIL` at `MAX_LOOKS`.
The bound is loose, because the looks score overlapping games.
A schedule whose terms sum to `BASE_TAIL` would hold the limit, at the cost of a stricter first look than the registered record 0001 uses.
That choice is open (#200).

A registered record counts as a look when it exists, whether or not it has run.
This counts more looks than the runs that happened, so it errs on the strict side.
Deleting a registered record that never ran is not detected, and the later records then fail the look check.

`MAX_LOOKS` caps the look number.
The bootstrap holds `BOOTSTRAP_RESAMPLES` means, so a tail smaller than `BASE_TAIL / MAX_LOOKS` would rest on too few resamples.
A hypothesis that needs more looks MUST be a new record with a new cohort, not a further look.

## Options not taken

- Refuse a second record on overlapping games.
  This blocks a legitimate second hypothesis on the same season.
- A fixed stricter bar for every later look.
  It does not grow with the look count.
- Alpha spending with a different weight for each look.
  It needs a plan for the total number of looks before the first look.

## Cutoff

`data_cutoff` means "data through this day".
The day is an Eastern game day, as in `game_day.py`.
The candidate may have used any game of that game day.
The cohort holds the games that start after the last microsecond of that game day.
A cutoff before the first confirmation season moves the first scored season to `CONFIRMATION_START`.
The run payload then carries a note that names the cutoff and the season.

## Interval ends

The two ends of the bootstrap interval leave the same count of resampled means outside.
Before this change the upper end left one fewer, so the interval was one rank narrower at the promoted end.
The upper end moved by one rank, so a recorded interval from an earlier run can differ by one resample from a rerun.

## Cohort size check

`min_games` is checked twice.
Before the run, the cohort count must reach `min_games`.
After the run, the games that the model run scored must equal that cohort count.
A run that scored a different count fails before it writes any file, so the look stays unspent.
A run with no interval fails the same way.
