---------------------------- MODULE daily_prepare ----------------------------
(* A model of the backfill in scripts/daily-prepare.sh and store/refresh_backfill.py.
   Design and mapping to the code: docs/design/daily-prepare-tla.md *)
EXTENDS Naturals, FiniteSets

CONSTANTS
    NWindows,       \* windows in the backfill; window 1 is the newest
    DaysPerWindow,  \* score days in one window
    Budget,         \* attempts one run makes before out_of_time() is true
    MaxFails,       \* MAX_UNIT_FAILURES, scaled down
    AbandonReads,   \* ABANDON_AFTER_FAILED_READS
    AbandonTicks,   \* ticks in ABANDON_MIN_SPAN_DAYS
    RetryTicks,     \* ticks in RETRY_FAILED_AFTER
    OpenDays,       \* days that are not closed at the start; a tick closes them one by one
    BrokenScore,    \* score days whose every read fails
    BrokenBox,      \* days whose boxscore read always fails
    OtherBroken,    \* TRUE when a source after the NHL steps always fails
    ArrivalsCount,  \* TRUE: a day that closed since the last run counts as progress (old rule)
    BacklogFirst    \* TRUE: read the oldest window first, so the backlog comes before the open days

Days == 1..(NWindows * DaysPerWindow)
Kinds == {"score", "box"}
Urls == [kind : Kinds, day : Days]
Score(d) == [kind |-> "score", day |-> d]
Box(d) == [kind |-> "box", day |-> d]

ASSUME /\ NWindows >= 1 /\ DaysPerWindow >= 1 /\ Budget >= 1 /\ MaxFails >= 1
       /\ AbandonReads >= 1 /\ AbandonTicks >= 1 /\ RetryTicks >= 1
       /\ OpenDays \subseteq Days /\ BrokenScore \subseteq Days /\ BrokenBox \subseteq Days
       /\ OtherBroken \in BOOLEAN /\ ArrivalsCount \in BOOLEAN /\ BacklogFirst \in BOOLEAN

\* TRUE when a run can read every day that is open at the start and still have budget for one
\* more unit. Each tick only closes days, so the condition stays true after a tick.
BudgetCovers == Cardinality(OpenDays) < Budget

Min(a, b) == IF a < b THEN a ELSE b
Oldest(S) == CHOOSE d \in S : \A e \in S : d <= e

(* The work order of one run. Window 1 comes first when BacklogFirst is FALSE, and it comes last
   when BacklogFirst is TRUE. In a window, read_days takes the days oldest first, then
   read_boxscores takes the games oldest first. *)
Steps == NWindows * 2 * DaysPerWindow
WindowAt(p) == ((p - 1) \div (2 * DaysPerWindow)) + 1
OffsetAt(p) == (p - 1) % (2 * DaysPerWindow)
KindAt(p) == IF OffsetAt(p) < DaysPerWindow THEN "score" ELSE "box"
\* k is the window index with the oldest window as 1.
DayAt(p) == LET k == IF BacklogFirst THEN WindowAt(p) ELSE NWindows - WindowAt(p) + 1
            IN (k - 1) * DaysPerWindow + (OffsetAt(p) % DaysPerWindow) + 1
UnitAt(p) == [kind |-> KindAt(p), day |-> DayAt(p)]
StageStart(p) == OffsetAt(p) = 0 \/ OffsetAt(p) = DaysPerWindow
BoxStageOf(p) == (WindowAt(p) - 1) * 2 * DaysPerWindow + DaysPerWindow + 1
LastOfWindow(p) == OffsetAt(p) = 2 * DaysPerWindow - 1

VARIABLES
    okRead, streak, span, firstAge, lastAge,  \* source_reads, per URL
    marker,                                   \* the .backfilled file
    open,                                     \* days that are not closed yet
    arrived,                                  \* days that closed since the last run ended
    calm,                                     \* TRUE: every read that is not broken works
    phase, pc, gaps, noted, due, fails, reads,
    progress,  \* what the code counts: a finished unit, as ArrivalsCount sets it
    backlog,   \* ground truth: a finished unit that is not an arrival
    clean, stopped, errors,  \* one run
    exitCode

store == <<okRead, streak, span, firstAge, lastAge, marker, open, arrived>>
run == <<phase, pc, gaps, noted, due, fails, reads, progress, backlog, clean, stopped, errors>>
vars == <<store, calm, run, exitCode>>

\* abandoned_urls(): enough failed reads since the last good read, far enough apart.
Abandoned(u) == streak[u] >= AbandonReads /\ span[u]
\* _day_complete(): a good read after the day closed.
Complete(d) == okRead[Score(d)] /\ d \notin open
\* games_missing_boxscore(): the score read found a final game with no boxscore.
WantBox(d) == okRead[Score(d)] /\ d \notin open /\ ~okRead[Box(d)]
\* failed_since(now - RETRY_FAILED_AFTER) in read_boxscores.
RecentFail(u) == streak[u] > 0 /\ lastAge[u] < RetryTicks

\* A unit is done when a read worked or when it is a known gap. No later run undoes it.
Done(u) == okRead[u] \/ Abandoned(u)
ScoreDone(d) == Done(Score(d))
\* A box is needed only for a final game, and the score read finds the game.
BoxDone(d) == ~WantBox(d) \/ Abandoned(Box(d))
WindowDays(k) == {d \in Days : (NWindows - k) * DaysPerWindow < d
                                /\ d <= (NWindows - k + 1) * DaysPerWindow}
\* A day that is still open is not required to be complete: the model does not record a good
\* read of an open day, and a failed or skipped read leaves the run unclean, so no marker.
WindowDone(k) == \A d \in WindowDays(k) : (d \in open \/ ScoreDone(d)) /\ BoxDone(d)

Broken(u) == IF u.kind = "score" THEN u.day \in BrokenScore ELSE u.day \in BrokenBox

Init == /\ okRead = [u \in Urls |-> FALSE]
        /\ streak = [u \in Urls |-> 0]
        /\ span = [u \in Urls |-> FALSE]
        /\ firstAge = [u \in Urls |-> 0]
        /\ lastAge = [u \in Urls |-> 0]
        /\ marker = FALSE
        /\ open = OpenDays
        /\ arrived = {}
        /\ calm = FALSE
        /\ phase = "idle" /\ pc = 1 /\ gaps = {} /\ noted = {} /\ due = {}
        /\ fails = 0 /\ reads = 0 /\ progress = FALSE /\ backlog = FALSE
        /\ clean = TRUE /\ stopped = FALSE /\ errors = FALSE
        /\ exitCode = 0

TypeOK ==
    /\ okRead \in [Urls -> BOOLEAN]
    /\ streak \in [Urls -> 0..AbandonReads]
    /\ span \in [Urls -> BOOLEAN]
    /\ firstAge \in [Urls -> 0..AbandonTicks]
    /\ lastAge \in [Urls -> 0..RetryTicks]
    /\ marker \in BOOLEAN /\ calm \in BOOLEAN
    /\ open \subseteq Days /\ arrived \subseteq Days
    /\ phase \in {"idle", "nhl", "other", "finish"}
    /\ pc \in 1..(Steps + 1)
    /\ gaps \subseteq Urls /\ noted \subseteq Urls /\ due \subseteq Days
    /\ fails \in 0..MaxFails /\ reads \in 0..Budget
    /\ progress \in BOOLEAN /\ backlog \in BOOLEAN
    /\ clean \in BOOLEAN /\ stopped \in BOOLEAN /\ errors \in BOOLEAN
    /\ exitCode \in {0, 1, 75}

(* Time passes only between runs. One tick is ABANDON_MIN_SPAN_DAYS. A run takes much less
   time than one tick, so every read of a run has the same time. A tick also closes the oldest
   open day. That day is an arrival: it closed since the last run, and the code leaves it out
   of the backlog (BACKLOG_BEFORE). *)
Tick ==
    /\ phase = "idle"
    /\ firstAge' = [u \in Urls |-> IF streak[u] > 0
                                   THEN Min(firstAge[u] + 1, AbandonTicks) ELSE 0]
    /\ lastAge' = [u \in Urls |-> IF streak[u] > 0
                                  THEN Min(lastAge[u] + 1, RetryTicks) ELSE 0]
    /\ open' = IF open = {} THEN open ELSE open \ {Oldest(open)}
    /\ arrived' = IF open = {} THEN arrived ELSE arrived \cup {Oldest(open)}
    /\ UNCHANGED <<okRead, streak, span, marker, calm, run, exitCode>>

GoCalm ==
    /\ ~calm
    /\ calm' = TRUE
    /\ UNCHANGED <<store, run, exitCode>>

(* daily-prepare.sh runs a backfill only while the marker is missing. score_gaps() and
   read_boxscores() read the gaps before any read of this run, and note them. *)
StartRun ==
    /\ phase = "idle" /\ ~marker
    /\ phase' = "nhl"
    /\ gaps' = {u \in Urls : Abandoned(u)}
    /\ noted' = {u \in Urls : Abandoned(u)}
    \* In calm weather a re-read resolves every game, so no day is due again.
    /\ \E D \in SUBSET {d \in Days : okRead[Score(d)]} : due' = IF calm THEN {} ELSE D
    /\ UNCHANGED <<store, calm, pc, fails, reads, progress, backlog, clean, stopped, errors,
                   exitCode>>

\* A finished unit that the run counts. An arrival is not backlog, so the old rule (ArrivalsCount)
\* is the only case where an arrival counts as progress.
Finished(u, ok) == ok /\ ~okRead[u] /\ u.day \notin open
CountsAsProgress(u, ok) == Finished(u, ok) /\ (ArrivalsCount \/ u.day \notin arrived)
CountsAsBacklog(u, ok) == Finished(u, ok) /\ u.day \notin arrived

\* run.attempt(): one read in one transaction. It records the result in source_reads.
\* A read finishes a unit for good when it works and the unit had no good read before.
\* An open day never finishes, because the next run reads it again.
Attempt(u, ok) ==
    /\ reads' = reads + 1
    /\ progress' = (progress \/ CountsAsProgress(u, ok))
    /\ backlog' = (backlog \/ CountsAsBacklog(u, ok))
    \* A good read of a day that is still open does not complete it (day_is_complete needs a
    \* read after the close). streak and the ages still reset, as in source_reads.
    /\ okRead' = [okRead EXCEPT ![u] = @ \/ (ok /\ u.day \notin open)]
    /\ IF ok
       THEN /\ streak' = [streak EXCEPT ![u] = 0]
            /\ span' = [span EXCEPT ![u] = FALSE]
            /\ firstAge' = [firstAge EXCEPT ![u] = 0]
            /\ lastAge' = [lastAge EXCEPT ![u] = 0]
       ELSE /\ streak' = [streak EXCEPT ![u] = Min(@ + 1, AbandonReads)]
            /\ span' = [span EXCEPT ![u] = IF streak[u] = 0 THEN FALSE
                                           ELSE @ \/ firstAge[u] >= AbandonTicks]
            /\ firstAge' = [firstAge EXCEPT ![u] = IF streak[u] = 0 THEN 0 ELSE @]
            /\ lastAge' = [lastAge EXCEPT ![u] = 0]
    /\ UNCHANGED <<open, arrived>>

Outcomes(u) == IF Broken(u) THEN {FALSE} ELSE IF calm THEN {TRUE} ELSE BOOLEAN

\* Move to the next unit. A stop jumps to the box stage of the window, or ends the NHL steps.
Advance(stopNow, failsNext) ==
    /\ pc' = IF stopNow /\ KindAt(pc) = "score" THEN BoxStageOf(pc)
             ELSE IF stopNow THEN Steps + 1
             ELSE IF stopped /\ LastOfWindow(pc) THEN Steps + 1
             ELSE pc + 1
    /\ fails' = failsNext

\* One loop pass of read_days().
ScoreStep ==
    LET u == UnitAt(pc)
        f == IF StageStart(pc) THEN 0 ELSE fails
    IN \/ /\ f >= MaxFails
          /\ stopped' = TRUE /\ clean' = FALSE
          /\ Advance(TRUE, f)
          /\ UNCHANGED <<store, reads, progress, backlog, errors>>
       \/ /\ f < MaxFails
          /\ u \in gaps \/ (u.day \notin due /\ Complete(u.day))
          /\ Advance(FALSE, 0)
          /\ UNCHANGED <<store, reads, progress, backlog, errors, clean, stopped>>
       \/ /\ f < MaxFails
          /\ ~(u \in gaps \/ (u.day \notin due /\ Complete(u.day)))
          /\ reads >= Budget
          /\ stopped' = TRUE /\ clean' = FALSE
          /\ Advance(TRUE, f)
          /\ UNCHANGED <<store, reads, progress, backlog, errors>>
       \/ /\ f < MaxFails
          /\ ~(u \in gaps \/ (u.day \notin due /\ Complete(u.day)))
          /\ reads < Budget
          /\ \E ok \in Outcomes(u) :
                /\ Attempt(u, ok)
                /\ clean' = (clean /\ ok)
                /\ errors' = (errors \/ ~ok)
                /\ Advance(FALSE, IF ok THEN 0 ELSE Min(f + 1, MaxFails))
          /\ UNCHANGED <<marker, stopped>>

\* One loop pass of read_boxscores(). A game that is not waiting is not in its list.
BoxStep ==
    LET u == UnitAt(pc)
        f == IF StageStart(pc) THEN 0 ELSE fails
    IN \/ /\ ~WantBox(u.day) \/ u \in gaps
          /\ Advance(FALSE, f)
          /\ UNCHANGED <<store, reads, progress, backlog, errors, clean, stopped>>
       \/ /\ WantBox(u.day) /\ u \notin gaps
          /\ f >= MaxFails \/ reads >= Budget
          /\ stopped' = TRUE /\ clean' = FALSE
          /\ Advance(TRUE, f)
          /\ UNCHANGED <<store, reads, progress, backlog, errors>>
       \/ /\ WantBox(u.day) /\ u \notin gaps
          /\ f < MaxFails /\ reads < Budget
          /\ RecentFail(u)
          /\ clean' = FALSE
          /\ Advance(FALSE, f)
          /\ UNCHANGED <<store, reads, progress, backlog, errors, stopped>>
       \/ /\ WantBox(u.day) /\ u \notin gaps
          /\ f < MaxFails /\ reads < Budget
          /\ ~RecentFail(u)
          /\ \E ok \in Outcomes(u) :
                /\ Attempt(u, ok)
                /\ clean' = (clean /\ ok)
                /\ errors' = (errors \/ ~ok)
                /\ Advance(FALSE, IF ok THEN 0 ELSE Min(f + 1, MaxFails))
          /\ UNCHANGED <<marker, stopped>>

NhlStep ==
    /\ phase = "nhl" /\ pc <= Steps
    /\ IF KindAt(pc) = "score" THEN ScoreStep ELSE BoxStep
    /\ UNCHANGED <<calm, phase, gaps, noted, due, exitCode>>

\* nhl() adds an error when the budget stopped the run before it finished one unit.
EndNhl ==
    /\ phase = "nhl" /\ pc = Steps + 1
    /\ phase' = "other"
    /\ errors' = (errors \/ (stopped /\ ~progress /\ reads >= Budget))
    /\ UNCHANGED <<store, calm, pc, gaps, noted, due, fails, reads, progress, backlog, clean,
                   stopped, exitCode>>

\* MoneyPuck, ESPN, and Daily Faceoff run after the NHL steps. A failure sets refresh exit 1.
OtherSources ==
    /\ phase = "other"
    /\ \E e \in (IF calm /\ ~OtherBroken THEN {FALSE}
                 ELSE IF OtherBroken THEN {TRUE} ELSE BOOLEAN) :
          errors' = (errors \/ e)
    /\ phase' = "finish"
    /\ UNCHANGED <<store, calm, pc, gaps, noted, due, fails, reads, progress, backlog, clean,
                   stopped, exitCode>>

\* The backfill branch of daily-prepare.sh: refresh_rc, then backfill_complete.
\* The run ends, so the arrivals of this run are no longer new for the next run.
Finish ==
    /\ phase = "finish"
    /\ exitCode' = IF errors THEN 1 ELSE IF clean THEN 0 ELSE 75
    /\ marker' = (~errors /\ clean)
    /\ arrived' = {}
    /\ phase' = "idle" /\ pc' = 1 /\ gaps' = {} /\ noted' = {} /\ due' = {}
    /\ fails' = 0 /\ reads' = 0 /\ progress' = FALSE /\ backlog' = FALSE
    /\ clean' = TRUE /\ stopped' = FALSE /\ errors' = FALSE
    /\ UNCHANGED <<okRead, streak, span, firstAge, lastAge, open, calm>>

Next == Tick \/ GoCalm \/ StartRun \/ NhlStep \/ EndNhl \/ OtherSources \/ Finish

Fairness == /\ WF_vars(GoCalm) /\ WF_vars(StartRun) /\ SF_vars(Tick)
            /\ WF_vars(NhlStep) /\ WF_vars(EndNhl) /\ WF_vars(OtherSources)
            /\ WF_vars(Finish)

Spec == Init /\ [][Next]_vars /\ Fairness

-----------------------------------------------------------------------------
\* Safety

\* Checked on the step that writes the marker. A day that is still open then is not required to
\* be complete. Once it closes, the plain refresh after the marker reads the day and its missing
\* boxscores (the non-backfill branch of nhl() reads every missing boxscore), so the model does
\* not require them in a later state.
MarkerOnlyWhenComplete == [][(~marker /\ marker') => \A k \in 1..NWindows : WindowDone(k)]_vars

AbandonedIsNoted == phase /= "idle" => gaps \subseteq noted

DoneStaysDone == [][\A u \in Urls : Done(u) => Done(u)']_vars

ScoreRead(d) == phase = "nhl" /\ reads' = reads + 1 /\ UnitAt(pc) = Score(d)
NoRereadOfCompleteDay == [][\A d \in Days : ScoreRead(d) => (~Complete(d) \/ d \in due)]_vars

Finishing == phase = "finish" /\ phase' = "idle"
StoppedNeverMarks == [][Finishing /\ (stopped \/ errors) => ~marker']_vars
ExitCodeRule == [][Finishing => /\ (exitCode' = 1 <=> errors)
                                 /\ (exitCode' = 75 => ~marker' /\ ~clean)
                                 /\ (exitCode' = 0 => marker')]_vars

\* A run that the budget stops with no finished unit exits 1, not 75.
StallExitsOne == [][Finishing /\ stopped /\ ~progress /\ reads >= Budget
                     => exitCode' = 1]_vars

\* The same rule, judged on the backlog alone. An arrival does not count here. With
\* ArrivalsCount TRUE, a run can count an arrival as progress and still exit 75 with no
\* backlog unit finished. This property fails then, and StallExitsOne does not. With
\* ArrivalsCount FALSE, progress and backlog are equal, so this property is the same check as
\* StallExitsOne. It is not an independent check. It exists as the target of that mutation.
BacklogStallExitsOne == [][Finishing /\ stopped /\ ~backlog /\ reads >= Budget
                              => exitCode' = 1]_vars

\* Liveness

\* A budget that covers the open days lets the backfill finish.
BackfillFinishes == BudgetCovers => <>marker

\* A budget that is too small ends in exit 1 in every run. Between runs, again and again,
\* the marker exists or the last run exited 1. One exit 1 and then endless exit 75 fails.
FinishesOrFails == []<>(marker \/ (phase = "idle" /\ exitCode = 1))
=============================================================================
