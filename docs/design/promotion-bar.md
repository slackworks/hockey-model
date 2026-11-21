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

The promotion bar for look `k` is the interval from the `BASE_TAIL / (k * (k + 1))` percentile to the `1 - BASE_TAIL / (k * (k + 1))` percentile of the block bootstrap.
A record promotes only when the upper end of that interval lies below zero.
The names `BASE_TAIL`, `MIN_TAIL_RESAMPLES`, and `MAX_LOOKS` are in `src/hockey_model/experiments/looks.py`.

## The schedule

The tail of look `k` is `BASE_TAIL / (k * (k + 1))`.
The tails of the first `n` looks sum to `BASE_TAIL * n / (n + 1)`, which is below `BASE_TAIL` for every `n`.
By the union bound, the chance that any look promotes a feature with no effect is therefore below `BASE_TAIL`.
The schedule needs no look count fixed before the first look, as a Bonferroni correction does.
The bound is loose, because the looks score overlapping games.
A test sums `promotion_tail_exact(k)` for every supported look with `fractions.Fraction` and asserts the limit, so float rounding cannot hide a total that is over.
The first look uses half of `BASE_TAIL`, which is stricter than the harmonic schedule (`BASE_TAIL / k`) that it replaced.
The harmonic schedule summed to `BASE_TAIL` times the `n`-th harmonic number, which is above the limit for two looks and more (#200).

A registered record counts as a look when it exists, whether or not it has run.
This counts more looks than the runs that happened, so it errs on the strict side.
Deleting a registered record that never ran fails the check when a higher confirmation record existed in the commit that deleted it, because a new record could then take its number (#291).
A deletion from before any higher record existed is allowed, so the old history stays valid.
Withdraw such a record instead (see Withdrawn records).

`MAX_LOOKS` caps the look number.
It is the last look whose tail leaves at least `MIN_TAIL_RESAMPLES` bootstrap means outside each end, at the current `BOOTSTRAP_RESAMPLES`.
A smaller tail would rest on too few resamples, and the bootstrap refuses a tail that cuts no resample.
More resamples raise the cap.
The registry has no further confirmation look past the cap: raise `BOOTSTRAP_RESAMPLES`, or run an exploration record, which cannot promote.

## Withdrawn records

A record with the status `withdrawn` never ran and never will.
It keeps its file and its place in the history, so the registry shows that the plan changed.
It spends no look, holds no result, and cannot return to another status.
The checker enforces these rules:

- A withdrawn record holds no result.
- A commit that stored the status `withdrawn` fixes it for every later commit.
- Only a registered record can be withdrawn. A record that was `inconclusive` ran and saw the games, even when it wrote no result, so its look is spent.
- A withdrawn record has no `experiments/results/<id>-games.csv`, in the working tree or in any commit, because a run writes that file.
- `hockey-model experiments run` refuses a withdrawn record of either cohort.

Withdraw a record before any later confirmation record counts it.
A later record sets its `look_number` from the records before it, and `look_number` is frozen.
Withdrawing an earlier record afterwards fails the look check of the later record, and the withdrawal cannot be undone.
The look message names the withdrawn records, so the cause is visible.
Run `hockey-model experiments check` before the commit that withdraws a record.

The checker cannot tell a record withdrawn before it ran from a run that was reverted before the commit and left no file.
That limit is the cost of a manual commit after a run.

Record 0001 was withdrawn when the schedule changed from harmonic to summable, and record 0002 replaces it as look 1 with the summable bar.
Only `status` changed in 0001, so its frozen fields still match its registration commit.

A JSON file in the registry whose name is not a record id (`NNNN-slug.json`) is an error, because the look count would skip it.

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

## Power rule
A run is inconclusive when the half-width of its interval is above the absolute `expected_effect`, even when the upper end lies below zero.
A later look uses a smaller tail, so its interval is wider.
A later look is therefore held to a stricter power bar than the first look.
This follows from the rule and is not a separate choice.

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
