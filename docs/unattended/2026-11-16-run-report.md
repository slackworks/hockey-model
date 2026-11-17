# Unattended run report: 2026-11-16

The sprint branch is `feat/255-consumer-api`.
It merged into `unattended/local-main` as `d1ffd84`, on the tip of `origin/main`, and the two trees are equal.
The integration branch holds 22 commits over `origin/main`, and 48 files changed with +2537 and -138 lines.
All 22 commits are signed, because the signer stayed unlocked during the run.
`scripts/resign.sh` is not needed.
Nothing is pushed.

## Rules of the run

1. The user asked for close of #80 and of every other issue that gives sportsbettr and leaguecheatr a stable API.
2. The sprint used its own branch, and the composite issue #255 named it.
3. The full gate ran on the final commit: `ruff check`, `ruff format --check`, `ty check`, `pytest`, and the per-module coverage floor.
4. The sprint ran the local review of `nbl-dev:pre-pr-review` in strict mode, with no push and no pull request.
5. A finding was fixed in the branch, or dismissed with the reason below.
6. The run never pushed, never signed a commit with a workaround, and never closed an issue.

## Work merged

The work is merged into `unattended/local-main` as `d1ffd84`, and the full gate passes on the merged branch.
The auto-mode classifier denied `sprint-merge.sh` while the sprint branch was checked out, because the state file is keyed by the current branch.
The merge ran after a checkout of `unattended/local-main`.

### The consumer API (#80, composite #255)

- `open_store` opens a store read-only, pins one fit, and starts one read transaction. `refresh_snapshot` is the only way to see newer rows.
- The public surface has `game`, `games_on_day`, `skater_games`, `goalie_games`, `team_xg_games`, `goalie_xg_games`, `injuries`, `starter`, and `predict`.
- `FitRef(fit_id=)` and `FitRef(label=)` name a fit. `LIVE_LABEL` is `"live"`. No caller gets the newest fit by default.
- `fit_labels` is append-only, so a label moves by a new row and the earlier rows stay.
- Each error names the operation, the input, and the fix: `StoreMissingError`, `StoreVersionError`, `UnknownFitError`, `UnknownGameError`, and `UnsupportedCapabilityError`.
- `games_on_day` reads an Eastern game day as a half-open window, which opens at 05:00 UTC and closes 24 hours later.

### The MoneyPuck gaps (#67)

- `unmatched_moneypuck` counts the newest MoneyPuck rows that name no stored game, and the rows that name a game that is not final.
- `mp_form_lag` reports how many stored final games a team's MoneyPuck form is behind.
- Each note names the game ids, so a reader can tell a one-week gap from a season-long gap.

### The in-sample note (#69)

- A prediction carries `TrainingInfo`: the fit id, the cutoff season, and `IN_SAMPLE`, `OUT_OF_SAMPLE`, or `UNKNOWN`.
- The check is conservative: a fit that trained through a season names every game of that season in-sample.
- A game in the Elo burn-in season is out of sample, because the fit reads no game of 2014-15.

## Work not done

| Issue | State | Why |
| --- | --- | --- |
| #80, #69, #67, #255 | Merged into `unattended/local-main` as `d1ffd84`; the PR is not open | The run must not push, so the PR waits for the user. |
| #238 | Open | The fit default season uses the UTC date, not the Eastern game day. The two differ only in the four hours after 00:00 UTC on September 1, when no game is stored. The code has a comment that points at #238. The decision belongs to the owner. |
| #174 | Blocked on the user | The weekly mutation report needs a look by the author, who is not the owner, a member, or a collaborator. The run must not act on a stranger's text. |
| #212, #239, #241, #242, #243, #244, #245, #246, #247, #248, #249 | Open | These issues do not relate to the consumer API. #212 waits on an owner choice between a win-chance interval and a goal-range label. |

## Review of the whole branch

Five analyzers ran in strict mode. The stored output holds 37 findings: 1 `P1` and 36 `P2`.

| Analyzer | Findings |
| --- | --- |
| `nbl-dev:overengineering-reviewer` | 9 |
| `nbl-dev:property-test-gap-finder` | 6 |
| `nbl-dev:security-audit` | 4 |
| `nbl-dev:variant-bug-hunter` | 18 |

`nbl-dev:semgrep-scanner` did not run. The auto-mode classifier blocked it for the whole run, so the diff has no Semgrep ruleset coverage. The evidence that stands in its place is the security audit, the variant hunt, and one test for each real defect below: every fix has a test that fails when the fix is removed.

Three refactor commits landed after the analyzers wrote their output: `6d8a02a`, `8487909`, and `b9666d3`. I read each diff against the code that calls it. Each one moves an existing body and changes no behavior. The gate ran again on the merged branch: 1876 tests, 97.74% coverage, and clean `ruff`, `ruff format --check`, and `ty check`.

### Fixed, with the test that holds each fix

1. `_labels_of` returned every label row ever written for a fit, so a demoted fit still reported `live`. The query now takes the newest row of each label. Commit `4835b9b`.
2. A game of a burn-in season came back in sample. `covers_season` now bounds both ends. Commit `311ea72`.
3. `--fit-id 0` was falsy, so the command served the live fit instead of refusing the id. Commit `f512a47`.
4. A MoneyPuck game with several stored reads counted once for each read. Commit `cca16af`.
5. A good read that tied a failed read lost, because the query ordered by `fetched_at` alone. Both queries now break the tie on `rowid DESC`. Commit `a49199c`.
6. A WAL store in a read-only directory reported "the file is not a hockey-model store". `_open_failure_fix` now names `SQLITE_READONLY_DIRECTORY` and gives the directory fix. Commit `91a9c74`.
7. The README and the design doc did not say to pass `--label live`, so a reader who followed them got a fit that `game`, `daily`, and the consumer refuse. Commits `7672239` and `baa8bf4`.
8. `label_fit` and `FormLag.team` had no reader. Both are gone. Commit `23ff73f`.
9. `game` was a pure delegate to `_require_game`, and the other readers called the private one. One body remains. Commit `6d8a02a`.
10. `reads._marks`, `moneypuck_gaps`, and `consumer_reads` built the same placeholder string. A shared helper holds it. Commit `8487909`.
11. A negative `count` reached `LIMIT ?`, and SQLite reads `-1` as no limit. `_require_count` refuses it.
12. `ConsumerStore(...)` was built outside the `try` that closes the connection. It is now inside.
13. The injury reader served a list older than `LIVE_FEED_MAX_AGE`. It now refuses the list, as the goalie rule does.
14. No test held the half-open day window or the inclusive age limit. Both now have a test that fails when the comparison changes by one. Commit `af0dc7d`.
15. A test locked in the `_labels_of` defect. It now asserts the fix.

### Dismissed, with the reason

1. Four findings say "checked, no defect" in their own text: the MoneyPuck team scoping, the result cutoff, the path quoting, and the `FitRef` validation. I read each one against the code and agree.
2. `covers_season` has no caller in `src`, `tests`, or `docs` as a direct call, but `nhl_model._training_info` calls it on line 84. The finding is stale.
3. `_TEAM_COUNTS` and `_GOALIE_COUNTS` are one query with two table names, but the code already has `_COUNTS` and `_counts(conn, table)`. The finding reads an older revision.
4. `is_empty` and `_result_cutoff` are gone from the code. Both findings read an older revision.
5. The `_marks` finding also named the writer modules. Commit `b9666d3` replaces the last four copies, so `rg 'join\("\?" for' src/` now finds only the helper.

### Filed as an issue

No finding was deferred without a fix. Items 1 to 15 are fixed and each one is in the branch. No new issue was filed for this sprint.

## Lessons

1. A stored analyzer output is a snapshot. Four findings named code that the branch had already changed, so each finding needs a read against the current file before a fix.
2. The `commit-msg-guard.sh` hook blocks the whole Bash call, not only the commit. A chained `git add && git commit` leaves the index clean and reuses the stale message file on the next try.
3. `sprint-merge.sh` reports `merge-permission = ok` from a grep that matches the `gh pr merge` entry. A local merge needs its own `autoMode.allow` entry.
4. The `perl -pi` edit of a test name changes the `-k` filter, so a filtered run can report `deselected` and look like a pass.
5. A one-line `rg -c` count includes the header line, so a style count by hand is off by one. Use a script.

## What the user must do

1. Push `unattended/local-main`, and open one PR from it to `main`.
2. Put one `Closes #N` line in the PR body for each of #80, #69, #67, and #255, and the URL of this report.
3. Run `bash "${CLAUDE_CONFIG_DIR}/skills/unattended-development/scripts/resign.sh"` only if a commit shows a bad signature. All 22 commits are signed now, so it does nothing.
4. Read the row for #238 in the table above, and decide the fit default season. The code waits on your answer.
