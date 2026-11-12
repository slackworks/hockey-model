# Unattended run report: 2026-11-03

The branch is `unattended/local-main`.
The base is `main` of `origin`.
The branch holds 56 commits on top of the base, with 17 merge commits.
The commits are not signed yet.
`scripts/resign.sh` signs them again before the first push.

## Rules of the run

1. Each sprint used its own branch, which came from `unattended/local-main`.
2. The full gate ran before each merge: ruff check, ruff format, ty, pytest with coverage, and the per-module coverage floor.
3. Each sprint ran the local `nbl-dev:pre-pr-review` in strict mode.
   Every finding was fixed or filed as an issue.
4. Nothing was pushed, and no PR was opened.
5. No gated feature was needed.
   The backtest decides which features are live, and the result is in the report below.
6. Issues by a stranger or assigned to someone else are blocked by default.
   The run found none.

## Work merged

Data and sources:

- #14 NHL API source and its store writes.
- #15 MoneyPuck source and its store writes.
- #16 ESPN source and its store writes.
- #17 Daily Faceoff source.
- #18 Refresh across all sources, and the `refresh` command.
- #26 A total time limit for slow response bodies.
- #27 Game team codes checked against one shared NHL code set.

Model and backtest:

- #19 As-of reads, player matching, and goalie resolution.
- #20 Elo and game features.
- #21 Logistic fit, `NhlGameModel`, and the `fit` and `game` commands.
- #22 Backtest metrics.
- #23 Backtest runner and the `backtest` command.
- #24 Real-data backtest and the report in `docs/backtest/2026-11-nhl-game-model.md`.
- #64 Each fit is certified by the gradient of its loss, and the saved fit carries its diagnostics.
- #7 The tracking issue for the game model, data layer, and backtest.

Quality:

- #2 Mutation testing with mutmut, with an advisory CI job.
- #29 Property tests for HTTP pacing, Game validation, and Retry-After.
- #30 The sprint for #26, #27, and #29.
- #32 The unused MoneyPuck team goals columns are gone.
- #33 SQL views replace the repeated newest-row joins.
- #34 Names for a never-read source, a clamped goal rate, and an ambiguous injury.

### Backtest result

The joint test passed on 2,792 games from the 2024-25 and 2025-26 seasons.
The live model uses expected-goals form and rest.
Its log loss is 0.6763.
The results-only Elo has 0.6876.
The 90% interval (5th to 95th percentile) of the difference is -0.0144 to -0.0083.
The guess run vetoed goalie goals saved above expected.
MoneyPuck data may leak into the result, because MoneyPuck's own model may have trained on the scored games.
The run did not score the model against a betting market.
A later review did: the model is level with the DraftKings line in 2024-25 and worse in 2025-26.
The matched-game table is in `docs/backtest/2026-11-nhl-game-model.md`.

## Work not done

| Issue | State | Why |
| --- | --- | --- |
| #8 | Blocked | The missing-regulars feature needs about one season of stored ESPN injury reads. The collection began on 2026-11-03. |
| #31 | Blocked | The first pass cut the surviving mutants from 564 to 524. The owner must decide how to treat error-message and equivalent mutants. Option 1 asserts full messages. Option 2 marks equivalents with `# pragma: no mutate`. |
| #35 | Blocked | A new play-by-play and shift source needs the owner's approval and a storage policy. |
| #36 | Blocked | The owner must choose how to store predictions and fit provenance. The comment recommends fit provenance with replay. |
| #38 | Blocked | The owner must choose how to confirm a predictive change, because no untouched evaluation data exists. |
| #37, #40, #41, #47, #48, #50, #51, #57, #58, #59 | Blocked | The work needs shot-level or shift-level data that the store lacks, and the protocol decision of #38. |
| #39, #42, #43, #44, #45, #46, #49 | Blocked | The data is in the store. The protocol decision of #38 is the only blocker. |
| #52, #53, #54, #55, #56, #60, #61 | Blocked | The work needs a player-projection or season-simulation design that the issue does not give. |
| #62, #63 | Blocked | The work needs NHL EDGE data or licensed tracking data, and the owner must approve the source. |

The owner filed #35 to #64 during the run.
They have no milestone, and each says it is not part of the v1 scope.
The run closed #64 and blocked the others with a comment that names the decision.

## Review of the whole branch

Seven analyzers ran over the whole branch before the final sprints: semgrep, the overengineering reviewer, the silent-failure hunter, the security audit, the property-test gap finder, the variant bug hunter, and the code reviewer.
One P0 finding was real: the goalie guess run used the boxscore starter, which leaks the result.
The fix is `use_boxscore` in goalie resolution.
The other fixes are in the commits of `feat/review-fixes`, which hold error names, a renamed-key check in the ESPN and Daily Faceoff pages, the `read1` deadline, Eastern game days, and the empty injury list.
Three follow-ups became #32, #33, and #34, and all three are done.
The run did not count findings by severity, so this report gives no counts.

The sprint for #64 ran the same checks inline, because the base detection of the review skill picked `origin/main` and would have reviewed the whole run again.
Semgrep with the `p/python` rules found nothing.
The inline review found three gaps in tests, and all three are fixed.
One test checks the conditioning measure.
One property test checks the gradient of random fits.
One property test checks the round trip of the saved diagnostics.
The analyzer sub-agents did not run for #64.
The evidence in their place is the semgrep run, the inline review of the full diff, and the full gate.

The last gate ran on the final commit of #64 before this report: 625 tests passed, total coverage was 98.45%, and ruff and ty were clean.

## Lessons

1. A step-size stop rule is not a convergence test: a clipped step is zero while the gradient is large.
2. The backtest takes about one hour, and the live fit takes about 30 minutes. Start them detached and wait with one loop.
3. A Bash tool call stops after 10 minutes, so each wait needs a loop that ends before then.
4. The review skill picks `origin/main` as the base on an integration branch. Pass the diff range by hand for a small sprint.
5. A heredoc with quotes can break the command guard. Write the script to a file and run the file.
6. New issues arrive during a long run. Read `open-issues.sh --count` again before the run ends.
7. A commit subject has a limit of 72 characters, and the limit includes the issue reference.

## What the user must do

1. Read the backtest report in `docs/backtest/2026-11-nhl-game-model.md`.
2. Answer the evaluation protocol on #38, because it applies to 27 issues.
3. Answer the mutant policy on #31.
4. Decide the source policy for play-by-play on #35.
5. Unlock the signer, then run `scripts/resign.sh`.
6. Approve the push and the single PR.
