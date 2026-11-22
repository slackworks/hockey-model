# TLA+ model of the experiment registry

Issue: #262.
The spec is `specs/experiment_registry.tla`.
The config is `specs/experiment_registry.cfg`.

## Problem

The registry keeps two promises that no single test can check from one file.
A record is frozen once it is registered, and a confirmation run happens once.
Both promises are checked against git history, and the checks interact.

1. The registration commit is the oldest commit that touches the path of the record.
2. `check_immutable_fields` compares the record with that commit.
3. `check_one_shot` compares the result with each commit that holds one.
4. `deleted_record_ids` keeps a record that has a result, and a record that a higher record outlived.
5. The look number of a record is one more than the count of earlier records that are not withdrawn.
6. The schema bounds the look number by `MAX_LOOKS`, so the count of records that spend a look is bounded.

The tests of `tests/test_experiments/` call one check at a time.
No test follows a sequence of commits, edits, deletions, renames, and merges.
This model does.

## Options

1. Write more tests of sequences. Each test shows one sequence. It does not show that no other sequence breaks a promise.
2. Model the history and the checks, and let TLC try every sequence up to a bound. This is the choice.

The model checks the checks.
It does not check that the Python code matches the model.
The mapping below is manual, as in `docs/design/daily-prepare-tla.md`.

## Mapping

| Python | TLA+ |
| --- | --- |
| A file `experiments/NNNN-slug.json` | A name in `Valid`, written as `10 * number + slug` |
| `ExperimentRecord` | `Blob`: `id`, `hyp`, `look`, `st`, `res` |
| `IMMUTABLE_FIELDS` | `ImmutableFields` in the config. The model keeps `id`, `look_number`, and `hyp`. `hyp` stands for the other fields. |
| `MAX_LOOKS` | `MaxLooks` |
| Status `registered`, `withdrawn` | `st` is `"reg"` or `"wd"` |
| Status `rejected`, `promoted`, `inconclusive`, and the result | `st` is `"ran"`, and `res` is 1 or 2. Two runs write two different values. |
| A git commit | One tree in `commits`. A tree maps each name to a blob, or to `NONE`. |
| The rename pairs that git reports | `renames[i]` |
| `git log --diff-filter=d -- path` | `HistOf` and `Touches` |
| `registration_commit` | `RegOf`, the oldest commit of `HistOf` |
| `check_immutable_fields` | `ImmutableError` |
| `check_one_shot` | `OneShotError` |
| `status_problem` in `withdrawn.py` | `WithdrawnError` |
| `look_problem` and the schema bound | `LookError`, `Expected` |
| `deleted_record_ids`, `_ever_had_result`, `_was_outlived` | `DeletionError`, `Deleted` |
| `registry_errors` and the loop of `check_all` | `Passes` |
| `run_experiment` refuses a record that cannot run | `RunOK` |
| The checks run on the head of a pull request | `Merge` and `SquashMerge` need `Passes` on the branch |

## The model

A history is a sequence of at most `MaxCommits` trees.
The first `pub` commits are on main.
The others are on a branch.

An author can take these actions.
Each action changes the tree of the head commit.

| Action | What it does |
| --- | --- |
| Add | A new record with any id and any look number. The id may differ from the name, because only the head of a branch is checked. |
| Edit | Flip `hyp`. |
| Withdraw, or restore | Set the status to `wd` or `reg`, and clear the result. |
| Run | `RunOK` must hold. The record gets `st = "ran"` and a result. |
| Delete | Remove a record. |
| Rename | Move a record to a free name. The author can fix the id and can edit `hyp` in the same commit. |

The commit actions are `Commit` (a new commit on the branch), `Merge` (the branch joins main with its commits), and `SquashMerge` (the branch joins main as one commit).
Only commits above `pub` change, so main is never rewritten.

The model has no amend action.
An amend of a commit that is not on main gives the history that a squash of the commit and its amendment gives.

`FollowRenames` is `TRUE` for the history that `git log --follow` reads.
Git reports a rename when a removed file and an added file are alike (`Alike`).
The accepted config sets `FollowRenames` to `FALSE`, as the code reads the history now.
`EveryCommit` is `TRUE` when the check compares each commit with the registration commit.
The accepted config sets it to `TRUE`.

## Properties

Each property holds when main holds a merged state and `Passes` is true.
The ghost variables `frozen` and `seenRes` record, by file name, what the commits on main held when a result first reached main.

| Property | Claim |
| --- | --- |
| `FrozenOnceRun` | A record with a result on main keeps the immutable fields that it had in that commit. |
| `RunsOnce` | A result on main stays, and no other result replaces it. |
| `DistinctLooks` | Two records with a result never share a look number, so the tails of the looks sum below `BASE_TAIL`. |
| `LookCap` | At most `MaxLooks` records spend a look. |
| `WithdrawnNeverRan` | A withdrawn record never had a result on main. |

Two more properties are defined and left out of the config.
They fail on purpose, and each is a gap that has an issue.

| Property | Claim | Issue |
| --- | --- | --- |
| `RegisteredBeforeRun` | The registration commit of a record with a result holds no result. | #307 |
| `MainStaysGreen` | When the checks pass on the branch, they pass on main after a squash merge. | #308 |

## The accepted run

The config has three names with three different numbers, and `MaxLooks` is 2.
The cap is reachable, because the model can hold one record more than the cap allows.
`MaxCommits` is 4.
TLC checks 3.8 million distinct states in about 40 seconds.
Run it with `bash scripts/tlc.sh --shard registry`.
The `registry` shard of `specs/shards.json` starts when a file of `src/hockey_model/experiments/`, a record under `experiments/`, or the spec changes.

`tests/test_tla_experiment_registry.py` checks that `MaxLooks` is `MAX_LOOKS`, that `ImmutableFields` is `IMMUTABLE_FIELDS`, that the config has more record numbers than looks, and that the config checks every property except the two known gaps.

## What the model found

The first four findings were gaps in the code.
They are fixed, and each fix has a test in `tests/test_experiments/`.

### 1. A field that changed and changed back

`check_immutable_fields` compared the head with the registration commit only.
Counterexample, with `EveryCommit` set to `FALSE`:

1. Commit 1 adds a record.
2. Commit 2 changes an immutable field.
3. Commit 3 runs the experiment.
4. Commit 4 restores the field.

The head equals the registration commit, so the check passed.
The run in commit 3 saw the changed record.
The check now compares each commit of the record with the registration commit.
The function is `_check_commits_keep_fields` in `history.py`.

### 2. `git log --follow` dropped the commits of a path

Counterexample, with `FollowRenames` set to `TRUE`:

1. A record with a result is on main.
2. A commit deletes the file.
3. A commit adds a new file under another name. The new file holds the id of the old record and holds no result.
4. A commit renames the new file to the old name.

Git sees the last commit as a rename, so it follows the path to the new file and stops there.
The commit that held the result is not in the history.
`check_one_shot` and the registration commit then read a record that never ran, and the check passed.

The history now is the history of one path.
The logs that find deleted records run with `--no-renames`, so a rename is a deletion and an addition.
A record that moves to another name is a deleted record and a new record.
The deleted record with a result stays in the error list.

### 3. A squash merge changed the verdict about a deletion

`_was_outlived` read the tree of the deleting commit.
A branch that deleted a record in one commit and added a higher record in the next passed.
After a squash merge, the deleting commit held the higher record, and main failed.
A higher record now outlives a deletion only when it is in the parent of the commit and in the commit.

### 4. Merge commits, found by the review and not by the model

The model has a linear history.
A path log follows one parent when a merge keeps the other side, so a branch that held a result could drop out of the history of a record.
A merge commit also lists no file names, so a record that only the merge deleted was not a deleted record.
The history of a record now runs with `--full-history`.
The deletion log runs on the first-parent line, where a merge shows the files that it removed.
The model does not cover merge commits that change a record.

### 5. Gaps that remain

Issue #307: the check accepts a registration commit that already holds a result.
The model shows a one-commit history that passes.
The record could have run more than once before the first commit.

Issue #308: a squash merge can fail on main after the branch passed.
The case needs a record number that a pull request deletes and adds again.
Main is red, so the failure is on the safe side.

### 6. Checks that the properties do not need

Removing `WithdrawnError` breaks no property of the model.
`OneShotError` already catches a result that a withdrawal clears.
The check stays: it covers a run that left no result, and the model has no such run.

Removing the outlived rule of `DeletionError` also breaks no property.
`LookError` already fails the records above a deleted record.
The rule gives a clearer error, and it blocks the reuse of a freed number.

## Break tests

Each row changes one thing and runs TLC.
The listed property must fail.
The edit is to the spec or to the config, and a script that applies it is not committed.

| Break | Edit | Violated |
| --- | --- | --- |
| Compare the head only | `EveryCommit = FALSE` | `FrozenOnceRun` |
| Follow renames | `FollowRenames = TRUE` | `RunsOnce` |
| No immutable check | Delete `ImmutableError` from `RecordError` | `FrozenOnceRun` |
| No one-shot check | Delete `OneShotError` from `RecordError` | `RunsOnce` |
| No look count | Delete `T[x].look # Expected(T, x)` from `LookError`, and drop `LookCap` from the config | `DistinctLooks` |
| No look bound | Delete `T[x].look \notin 1 .. MaxLooks` from `LookError` | `LookCap` |
| No deletion check | Delete `DeletionError` from `Passes` | `FrozenOnceRun` |
| No withdrawn check and no one-shot check | Delete both from `RecordError`, and drop `RunsOnce` and `FrozenOnceRun` from the config | `WithdrawnNeverRan` |
| Registered before run | Add `RegisteredBeforeRun` to the config | `RegisteredBeforeRun` |
| Squash merge | Add `MainStaysGreen` to the config | `MainStaysGreen` |

## What the model does not cover

- The statistics of the promotion bar. They stay with `docs/design/promotion-bar.md`.
- The fields of a record other than `id`, `look_number`, and one field for the rest.
- The per-game CSV file, its hash, and the rule that a withdrawn record has no CSV file.
- A run that no commit holds. The check cannot see a run on a laptop that the author does not commit. `docs/design/promotion-bar.md` states the same limit for a run that the author reverts.
- A file whose name is not a record id. It always fails the check.
- Two records with the same number. The names of the model have different numbers.
- Exploration records. They spend no look.
- A shallow clone. The check refuses it.
- A force push to main. Branch protection stops it.
- A status of `inconclusive` with no result. A run writes a result, so only a hand edit makes it.
