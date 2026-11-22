# Unattended run report: 2026-11-22

The branch is `unattended/local-main`.
The base is `origin/main`.
The branch holds 235 commits, and all of them are signed (`%G?` is `G` for each one).
Nothing was pushed before this report.

## Rules of the run

1. Each sprint had its own branch `feat/NNN-slug` or `fix/NNN-slug`, cut from `unattended/local-main`.
2. Each sprint ran the strict local review: semgrep, over-engineering, silent failures, security, property-test gaps, and variant bugs, as the diff needed.
3. Each finding got its own fix commit, an issue, or a dismissal with the evidence.
4. Each sprint passed the full gate before the merge: `verify.sh`, the per-module coverage floor, and the mutation shard check.
5. The merge into `unattended/local-main` was a merge commit (`sprint-merge.sh`).
6. No issue was closed. The pull request closes the done issues.
7. A change that needs a live run or an owner decision became a comment on the issue and went on the blocked list.

## Work merged

The run made 29 sprints.
Issue numbers are the ones in the `Closes` lines of the pull request.

Hourly prepare and report (#281, #282, #284, #285, #162, #280):

- A second prepare for a prepared day skips the backtest and the fit. The report skips when its inputs did not change.
- The prepare and the report run every hour.

Speed of the prepare (#283, #286, #289, #304, #305, #306, #287, #288):

- The backtest and the fit run in worker processes, sharded by season.
- A finished season is read from the season row cache when its key did not change.
- A golden backtest check holds the results fixed.

Live feed and store (#278, #279, #302, #303, #294, #297, #292, #291, #73):

- Stall errors, abandoned feeds, and the notes of the deletion history are named in the output.
- A deleted record that never ran keeps its number.

Model and validation (#70, #71, #93, #92, #143, #144, #200, #220, #212, #257):

- The look schedule is summable, with the `withdrawn` status.
- The daily report JSON has a version and a schema, and the release notes have a win-chance summary.

TLA+ and tests (#261, #262, #263, #189, #191, #202, #119, #133, #207, #209, #274):

- Three specs run as shards: the daily prepare, the store upgrade, and the experiment registry.
- The registry spec (sprint 29) found four gaps in the registry checks, and the fixes are in this run: every commit of a record is compared with its registration commit, the history reads one path with no rename following, a squash merge keeps the verdict about a deletion, and a merge commit does not hide a side branch.

## Work not done

The run stopped at the request of the user, with six implementable issues open.

| Issue | State | Why |
| --- | --- | --- |
| #309 | Open | Small review items of sprint 29 (inline a helper, one property test, a cohort check, a test helper). |
| #308 | Open | A squash merge can fail the registry check on main. The note recommends to accept it until it happens. |
| #307 | Open | The registry check does not require a record to be committed before its run. |
| #264 | Open | TLA+ spec of the read log, the as-of cutoffs, and goalie membership. Size L. |
| #176 | Open | Shard the heavy mutation modules by function. Size L. It needs CI times. |
| #31 | Open | Kill surviving mutants in the parsers and the rate math. Size L. |

Twenty issues wait for the user, and each has a comment that states the open question.
They need a live GitHub run (#301, #300, #299, #293, #260, #188, #177, #171, #170, #154, #99), an admin or a license decision (#104, #102, #101, #97), or an owner decision (#296, #295, #82, #78, #8).

## Review of the whole branch

Each sprint was reviewed when it merged.
The whole-branch review of Phase 4 did not run, because the user asked to stop and wrap up.
The evidence that stands in for it: the full gate passes on the final commit, and each sprint kept its own findings list.

Sprint 29 ran six analyzers on its diff.
Semgrep found nothing, and it had no ruleset for `.tla` files.
The other analyzers gave 2 P1 and 11 P2 findings, and 5 more came from the silent-failure review.
Fixed: the parser properties, the empty-repository guard, the merge history, the shard covers, and the single deletion log.
Filed as #309: the helper inline, the cohort check, the outlived docstring and test, the test helper, and two spec nits.
Dismissed with the reason: the first-four-characters compare of `outlived` is guarded by `RECORD_STEM`, and the `.split()` of names drops only names that are not record ids.

## Lessons

1. A model that has a ghost identity for a record gives false alarms. Key the ghost state by file name, and say what the check cannot see.
2. A model of a history needs a bound on the commits and no action that repeats on one commit. An amend action made the state space 100 times larger.
3. A history check must read every commit, not the head, because a run sees the record of its own commit.
4. A path log hides a side branch that a merge discarded. Use `--full-history`.
5. Rename detection in git is a guess by likeness. A check must not depend on it.
6. Start a long TLC run in the background, and read its progress line. Three runs timed out at 10 minutes before the state space was cut.

## What the user must do

1. Unlock the signer if a later step asks for it. The commits are already signed, so `resign.sh` is a no-op.
2. Review the pull request. `+vince` is in the first comment.
3. After the push, check that the `tla` workflow runs the `registry` and `upgrade` shards and that all shards pass.
4. Read the 20 blocked issues. Each has a comment with a recommendation.
5. Decide #308: accept the squash-merge case, or run the check on a squashed tree.
6. Pick the next issue from the six open ones.
