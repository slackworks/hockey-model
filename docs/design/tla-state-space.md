# TLA+ state-space reduction (#188)

## Goal

Make the `tla` CI job finish in less than 8 minutes 25 seconds, with no loss of what the model checks.
The full config (`specs/daily_prepare.cfg`) has 27,181,712 distinct states after the arrival step (#186), and the job timeout is 30 minutes.

## Rules that every reduction obeys

1. Keep the unreduced model. Copy `specs/daily_prepare.tla` to `specs/daily_prepare_reference.tla` (module renamed) before the first reduction. CI does not run it. `scripts/tlc.sh --reference` runs it.
2. Each reduction is one commit with four parts: the change, a written preservation argument, the state count and time before and after, and a break test.
3. A preservation argument shows one of two things. (a) The removed or merged information is never read by an action or a property. (b) Two merged states have the same future behavior and the same property verdicts.
4. Liveness properties (`BackfillFinishes`, `FinishesOrFails`) are never checked under `SYMMETRY`. TLC does not support liveness with symmetry soundly.
5. Measure with the same JVM, the same worker count (`-workers 4`), and the same machine. Run each timing three times and report the median and the peak memory (`/usr/bin/time -l` on macOS, `/usr/bin/time -v` on Linux).

## Candidate reductions, in order

Try them in this order. Keep a candidate only when its argument holds.

### R1. Check liveness at the end only

Pass `-lncheck final` to TLC in `scripts/tlc.sh`.
TLC then checks the liveness properties once, after it builds the state graph, instead of during the build.
The state space does not change, so no argument about states is needed.
Argument: the option changes when liveness is checked, not what is checked. TLC still reports a liveness violation with a trace.
Break test: the existing break test that makes `BackfillFinishes` fail MUST still fail with `-lncheck final`.

### R2. Split safety and liveness configs

Make `specs/daily_prepare_safety.cfg` with `INVARIANTS` and the action `PROPERTIES` (`MarkerOnlyWhenComplete`, `DoneStaysDone`, `NoRereadOfCompleteDay`, `StoppedNeverMarks`, `ExitCodeRule`, `StallExitsOne`, `BacklogStallExitsOne`) on the full constants.
Make `specs/daily_prepare_liveness.cfg` with `BackfillFinishes` and `FinishesOrFails` only.
A safety-only run needs no behavior graph for fairness, so it is cheaper.
The CI job runs both configs, one after the other.
Argument: the union of the two configs checks every property of the old config, each on the full constants.
Break test: each property keeps a break test that fails in the config that now holds it.

### R3. Canonical `lastAge` for score URLs

`lastAge[u]` is read only by `RecentFail(u)`, and only `BoxStep` calls `RecentFail`, with a box URL.
So `lastAge` of a score URL is never read.
Change `Attempt` and `Tick` so that `lastAge[Score(d)]` stays 0.
Argument: case (a) of rule 3. Search the spec for `lastAge` and list every use in the commit message.
Equivalence test: run the reference model on the three sweep configs of sprint #187 with a `VIEW` that maps `lastAge[Score(d)]` to 0 for every day. The distinct state count MUST equal the count of the reduced model on the same config, and every property verdict MUST be the same.
Break test: make `BoxStep` call `RecentFail(Score(u.day))` in the reduced model. The equivalence test MUST then fail. Undo the break.

`firstAge` is not a candidate: `span[u]` becomes TRUE only when `firstAge[u] = AbandonTicks`, and the value stays saturated after that, so a canonical value merges no states.

### R4. Measure each variable before more work

When R1 to R3 do not reach the target, find the variable that splits the most states.
For each variable `v`, run TLC once with `VIEW` set to the tuple of all variables except `v`, on the 1.5 million state config of the sprint #187 sweep.
A `VIEW` run is not a valid check. It only counts states, to rank the variables.
Write the counts in the design note, then propose a reduction for the top variable, with an argument under rule 3.

### R5. A smaller covering config

When the job is still too slow, use a smaller config for CI and keep the full config for a weekly run.
The smaller config MUST reach every disjunct of `ScoreStep` and `BoxStep`, and both branches of every `IF` in `Advance` and `Finish`.
Prove coverage with `-coverage 1`: every action line has a count above zero.
Store the coverage output in the pull request.

## Deliverables

1. `docs/design/daily-prepare-tla.md` gets a section "State space" with a table: reduction, argument (one line and a link to the commit), states before, states after, median time, peak memory.
2. `scripts/tlc.sh` runs the configs that CI needs, and `--reference` runs the reference model.
3. An automated break test script, `scripts/tla_breaks.sh`, applies each break as a `sed` change to a copy in `.tmp/`, runs TLC, and checks that the named property fails. Each break names the property it targets. A break that passes is an error. Use `perl -pi -e` or a Python edit, because macOS `sed -i` differs from GNU `sed -i`.
4. The CI `tla` job time, measured on three runs, in the issue.
5. Item 5 of the issue ("dropping score ages needs a reason beyond `Done(u)`") is answered by R3: it drops only the ages that no action reads, and keeps every age that an action or a property can read.

## Calls made

- R1 and R2 first, because they change no state and need no argument about states.
- A weekly full run (R5) only as the last resort, because a smaller CI config can miss a branch that the full config reaches.
