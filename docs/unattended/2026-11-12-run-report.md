# Unattended run report: 2026-11-12

The branch is `unattended/local-main`.
The base is `main` of `origin`.
The branch holds the commits of five sprints and one review-fix branch, on top of the base.
All commits on the branch are signed, because the signer stayed unlocked during the run.
`scripts/resign.sh` is not needed.
Nothing is pushed.

## Rules of the run

1. The user asked for five `dev-sprint` iterations, then a wrap-up. The run did not continue until the issue count was 0.
2. Each sprint used its own branch, and a composite issue named the sprint.
3. The full gate ran before each merge: ruff check, ruff format, ty, pytest with coverage, and the per-module coverage floor.
4. Each sprint ran `nbl-dev:pre-pr-review` in strict mode, with no push and no pull request.
5. A finding was fixed, or an issue was filed for it. Two findings were dismissed with a reason (see the review section).
6. A final review of the whole branch ran at the wrap-up. Its fixes are in the branch `fix/wrapup-review`.
7. An issue that needs an owner decision got a comment with a recommendation, and `open-issues.sh --block`.

## Work merged

The composite issues #197, #199, #201, #203, and #206 name the sprints.

### CI and tooling (sprint 1, #197)

- #98: a `smoke` CI job builds the wheel, installs it alone in a clean venv, and predicts one game from a seeded store. A probe drops a file from the wheel, and the job fails if the test still passes.
- #100: a zizmor hook in prek and a `zizmor` CI job, at one version. A test keeps the two versions equal. All jobs now have a timeout.

### Experiment registry (sprint 2, #199)

- #115: the promotion bar scales with the look number. A confirmation record must set `look_number` to one more than the earlier confirmation records. The rule is in `docs/design/promotion-bar.md`.
- #116: a run fails before it writes a file when it scored a different count than the cohort. The payload notes a moved season floor. `data_cutoff` means "data through this Eastern game day".
- The two ends of the bootstrap interval now leave the same count of means outside.

### Tests and docs (sprint 3, #201)

- #156: a property test of `check_partition` over generated shard tables.
- #145: a store-level test that a candidate over the imputation limit is vetoed and the Elo run is unchanged.
- #118: git-history property tests for the one-shot check and the frozen fields.
- #107: `AGENTS.md` names `uv run ty server`.

### Refresh (sprint 4, #203)

- #126: `hockey-model refresh --retry-skipped` reads the skipped units again once.
- #136: the MoneyPuck reads stop on the time budget after the first file of a run.
- #134: a game that a boxscore write leaves missing is named in the notes, and one game is read once in a refresh.

### Report gate (sprint 5, #206)

- #127: the publish decision of `scripts/report-gate.sh` is in `report_gate.py`, with a `report-decide` command and property tests. The script validates the payload of that command.

## Work not done

| Issue | State | Why |
| --- | --- | --- |
| #200 | Blocked, filed in this run | Owner decision: harmonic or summable look schedule. The harmonic schedule holds a weaker limit than 5 percent across looks. The issue holds three options and a recommendation. |
| #162 | Blocked | Owner decision on the trigger hours. A comment holds a recommendation. |
| #196 | Blocked | The author is not the owner, a member, or a collaborator. |
| #174 | Blocked | The author is not the owner, a member, or a collaborator. |
| #181 | Not worked | The open pull request #195 on `fix/issue-181-valid-report-dates` already holds the fix. |
| #170, #171, #177 | Not worked | They need measurements or the branch protection check (#104) first. |
| #137 | Not worked | It needs the end of the 2026-27 season. |
| The other open issues | Not worked | The run stopped after five sprints, as the user asked. |

## Review of the whole branch

Each sprint ran these analyzers: `nbl-dev:semgrep-scanner`, `nbl-dev:overengineering-reviewer`, `pr-review-toolkit:silent-failure-hunter`, `nbl-dev:variant-bug-hunter`, and `nbl-dev:property-test-gap-finder`.
Sprint 1 and sprint 2 also ran `nbl-dev:security-audit`.
Sprint 3 ran no silent-failure, security, or property analyzer, because it changed tests and one document line.
The whole-branch review ran semgrep, the silent-failure hunter, the variant hunter, and the security audit.
Semgrep found nothing in every run. It does not scan Markdown or TLA+ files.

The findings, by severity. These counts are the merged reports of the file-based analyzers. The inline reports of the silent-failure hunter add more findings, and they are not in the counts.

| Review | P0 | P1 | P2 |
| --- | --- | --- | --- |
| Sprint 1 | 0 | 2 | 24 |
| Sprint 2 | 0 | 2 | 17 |
| Sprint 3 | 0 | 1 | 11 |
| Sprint 4 | 0 | 3 | 10 |
| Sprint 5 | 0 | 0 | 9 |
| Whole branch | 0 | 2 | 5 |

The fixes that mattered:

- Sprint 1: the negative probes passed on any non-zero exit. They now require exit 1, and exit 2 means a setup failure.
- Sprint 2: the look schedule was called Bonferroni. It is harmonic, and the document now says so. The upper bootstrap index was one rank off.
- Sprint 4: a game that never resolves would have held the backfill open for ever, and an old unresolved game counted as progress. Both are fixed.
- Whole branch: the cutoff split the Eastern game day at 20:00 Eastern. It now ends at the close of the Eastern game day.

Dismissed with a reason:

- The `game_records.py` split in sprint 2: the budget rule requires the extraction from `runner.py`.
- The side-path rewrite of the wheel in `smoke_wheel.py`: two reviewers disagreed, and the safe form stays.

Filed as issues: #198, #200, #202, #204, #205, and #207.

## Lessons

1. The commit-subject hook allows 72 characters, and a subject in `type: summary (#N #M)` form often needs a retry.
2. A relative import between test modules does not work here. A shared helper module in `tests/` does.
3. A `sed -i` range such as `0,/re/` does not work on macOS, so a mutation by line number is safer.
4. A reviewer found a real regression in code that the new tests did not cover. A mutation check of each new test is worth its cost.
5. A guard hook blocked a shell command that held the word "clone" in an issue body. Reword the text, or write the command to a file.
6. Create the sprint branch before the first edit. Two sprints started editing on the integration branch, and the move was safe only because nothing was committed yet.

## What the user must do

1. Read `docs/design/promotion-bar.md`, then answer #200.
2. Answer #162, which has a recommendation.
3. Check that the cutoff change does not break the plan for record `0001-v1-forward-test`: its cohort now starts at the close of the Eastern game day 2026-10-31.
4. Say whether to push `unattended/local-main` and open one pull request. The commits are already signed.
5. Review the CI changes in the pull request, because the `smoke` and `zizmor` jobs run only on GitHub.
6. After the merge, add the `smoke` check name to #104.
