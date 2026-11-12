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
This is the Bonferroni correction for `k` looks.
The names `BASE_TAIL` and `MAX_LOOKS` are in `src/hockey_model/experiments/looks.py`.

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
The candidate may have used any game that started on that day.
The cohort holds the games that start after the last microsecond of that day in UTC.
A cutoff before the first confirmation season moves the first scored season to `CONFIRMATION_START`.
The run payload then carries a note that names the cutoff and the season.

## Cohort size check

`min_games` is checked twice.
Before the run, the cohort count must reach `min_games`.
After the run, the games that the model run scored must equal that cohort count.
A run that scored fewer games fails before it saves, so the look stays unspent.
