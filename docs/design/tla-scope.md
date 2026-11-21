# TLA+ scope: which module earns a spec

Tracks issue #261.

## Problem

The repository has one TLA+ model, `specs/daily_prepare.tla`.
It covers the backfill in `scripts/daily-prepare.sh`, `store/refresh_backfill.py`, and the exit codes (see [daily-prepare-tla.md](daily-prepare-tla.md)).
Three more candidates have their own issues: #262, #263, and #264.
This note states the rule that decides what to model next.
Without the rule, the answer drifts to "another spec each time a module is added", or to "one model forever".

## The test

A module earns a spec when all three conditions hold.

1. **A state machine over stored state.** The module reads state from a store, a file, or a release, and it writes the next state back. A model needs states and steps to check. A pure function has neither.
2. **A property that spans more than one run.** The rule is about a sequence: a repeated run, an interrupted run, or runs that overlap. A property of one call is a unit test or a property test, and a test checks it cheaply.
3. **A failure that reads back clean.** A wrong stored value looks right when a later run reads it. A failure that raises is found by any example test. A model finds the silent one, because it searches every sequence and not the examples that someone thought of.

A module that fails one condition does not earn a spec.
A module that passes all three earns one only when its value is larger than the cost below.

## Result by area

| Area | State machine | Spans runs | Silent failure | Result |
| --- | --- | --- | --- | --- |
| `store/refresh_*`, `scripts/daily-prepare.sh` | Yes: windows, the abandon rule, the marker | Yes: a backfill takes several runs | Yes: a marker that is written too early | Passes. Modelled in `specs/daily_prepare.tla` (#185, #188). |
| `store/upgrade.py` | Yes: lock, copy, one transaction for each version, a record row | Yes: a repeated or interrupted upgrade | Yes: a half-applied version reads back as a valid store | Passes. #263. |
| `experiments/registry.py`, `history.py`, `looks.py`, `withdrawn.py` | Yes: the record statuses and the look chain | Yes: the rules read git history across commits | Yes: a reset or deleted record reads clean | Passes. #262. |
| `store/source_reads.py`, `staleness.py`, `reads.py`, `membership.py` | Yes: the read log and its as-of views | Yes: the abandon rule and the active-failure window span runs | Yes: #126, #127, #130, and #131 were interaction bugs that a person found by hand | Passes. #264. |
| `report_gate.py`, `report_trigger.py` | Partly | Partly | Partly | Fails as written. The gate is a pure decision of the facts that one run read. The publish rule spans runs through the release, and that part is a scheduling model (#241). The pure part is an exhaustive table test (#245). |
| `game_model/*`, `backtest/*` | No | No | No | Fails. These are pure functions of stored rows. |
| `sources/*` | No | No | No | Fails. Each is one call, and an error raises. |
| `json_schema.py`, `report_schema.py` | No | No | No | Fails. These validate one document. |
| CLI | No | No | No | Fails. The CLI has no state of its own. |

## What one more spec costs

The numbers come from the backfill spec (#185, #188).

- **Files:** a spec file (`specs/daily_prepare.tla` is 334 lines), one or more `.cfg` files, and a design note (the note for the first spec is 865 lines).
- **A constants test:** `tests/test_tla_constants.py` keeps the `.cfg` values in step with the code constants. Each spec needs its own entries.
- **One break test for each property:** a person changes the spec so that the property must fail, runs TLC, and records the result in the design note. These tests are manual today.
- **CI time:** the full backfill config has 27,181,712 distinct states and took 29 minutes 21 seconds in the #186 run, which is an upper bound. The `tla` workflow took 22 to 40 minutes, from creation to the last update, in the six successful runs of 2026-11-20. That figure includes queue time.
- **State growth:** one new step, the arrival step, multiplied the states of the full config by about 6.8, from 4,000,218 to 27,181,712. A spec grows fast with each variable.
- **Shared parts:** `scripts/tlc.sh`, its tests, and `.github/workflows/tla.yml` exist once. Each spec adds a configuration to them and not a new tool.
- **Upkeep:** a change to a modelled rule means a change to the spec, its constants, its break tests, and the note. A spec that falls behind the code gives false confidence, which is worse than no spec.

A spec must fit the CI limit, or it needs a smaller config as in #185.
The break tests are the check that a property is not empty, so a spec with no break test does not count.

## Order

1. **#263, the store upgrade.** It has the smallest model: one store, a chain of versions, and a few crash points. A bug here corrupts the one store that every run shares, and #81 found the gap by hand. The value for each modelled state is the highest.
2. **#264, the read log.** The issue lists four past bugs (#126, #127, #130, #131) that a person found by hand, which shows that the interaction class occurs here. The model is larger than the upgrade because it has the as-of views and several windows. The evidence of past bugs puts it before the registry.
3. **#262, the registry.** It has the highest modelling cost, because it models git history: commits, renames, deletions, and the status chain. The rules are also covered by example tests against a real git repository, so a spec adds less than in the other two places. The withdrawn status (#200) adds transitions to the model: `registered` to `withdrawn`, and nothing out of `withdrawn`.

Each place has its own issue.
The order is a recommendation for the owner, who sets the milestone of each issue.

## A new candidate

Apply the three conditions to the new module first, in this order, and stop at the first "no".
Then file one issue for the module, with the property list, and place it in the order above by the same two questions: how large is the model, and how often did this class of bug occur.
Do not write the spec in the issue that finds the candidate.

## Not in scope

- Writing any of the specs. Each one is its own issue.
- The state-space reduction, which stays in #185 and #188.
- A proof that the Python code matches a spec. A spec checks the design, and tests check the code.
