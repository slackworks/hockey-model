# Mutation shards by function (#176)

## Goal

Every shard of `.github/workflows/mutation.yml` finishes in less than 10 minutes of wall time.
Today a shard holds whole modules, and `experiments.models` alone runs about 65 minutes in series.

## First step: measure again

The `timeout_multiplier` of `[tool.mutmut]` changed from 15 to 3.0 after the run that set the shard sizes.
Run `mutation.yml` by hand once on `main` before any change.
Record the wall time of each shard and the serial time of each function.
The serial time of a function is the sum of `durations_by_key` for its mutants in the merged `.meta` files.
Write a small script, `scripts/mutation_times.py`, that prints that sum by module and by function as JSON, so the sizing step reads numbers and not a log.

If every shard is already under 10 minutes, stop, write the times in the issue, and close it.

## Shard entries

mutmut names a mutant `hockey_model.<module>.x_<function>__mutmut_<n>`.
A method is `hockey_model.<module>.xǁ<Class>ǁ<method>__mutmut_<n>`.
Check both forms on a real `mutants/` tree before the first change, and fix this text if they differ.

A shard entry in `SHARDS` becomes one of two forms:

- `"sources.nhl"`: every mutant of the module.
- `"experiments.models:validate_record,_check_keywords"`: the mutants of the named functions only. A method is written `Class.method`.

A module that has a function entry in any shard MUST also have one rest entry: `"experiments.models:*"`.
The rest entry takes every function of the module that no function entry names.

## Code changes in `scripts/mutation_shards.py`

1. `parse_entry(text) -> ShardEntry` with fields `module`, `functions` (a tuple, empty for a whole module), and `rest` (bool).
2. `shard_patterns(index)` prints one glob for each function: `hockey_model.experiments.models.x_validate_record__mutmut_*`. A rest entry prints the patterns of the rest functions.
3. `module_functions(path) -> tuple[str, ...]` lists the top-level functions and the class methods of a source file with `ast`. The rest entry and the check use it.
4. `check_partition` checks every function of every covered module: it sits in exactly one entry. The error names the function and the shards.
5. `merge` already treats a `null` result as "not run". Add a check: when two shard files give a non-null result for the same key, exit 1 and name the key.

## Sizing

Use the measured serial time of each function.
Assign functions to shards with the longest-first rule: sort by time, then put each function in the shard with the least total so far.
Run the rule once in a script, and paste its result into `SHARDS` by hand, so the table stays the one source of truth and a reviewer can read it.
Target 8 minutes of mutant work for each shard, which leaves time for the setup steps.
Add shards when the total needs more. Each shard is one more matrix entry in `mutation.yml`.

## Tests

In `tests/test_mutation_shards.py`:

- `parse_entry` for the three forms, and a bad entry (`"x:"`, `"x:a,,b"`) raises `ShardError` with the full message.
- A module with a function entry and no rest entry fails `check_partition`.
- A function in two entries fails `check_partition`, and the message names both shards.
- `merge` with two non-null results for one key exits 1.
- A `hypothesis` test (the open #156): for a generated module with functions and a generated split, `check_partition` passes exactly when each function is in one entry.

## Done when

A manual run of `mutation.yml` has every shard under 10 minutes, and the merged result has the same killed and survived counts as a serial `uv run mutmut run` on the same commit.
If the counts differ, that is #155. Record the difference in #155 before the merge.

## Calls made

- Function lists written by hand in `SHARDS`, not computed at run time, so the table stays readable and reviewed.
- Longest-first assignment, because it is simple and is within 4/3 of the best split for this problem.
