------------------------------ MODULE experiment_registry ------------------------------
\* A model of the experiment registry (src/hockey_model/experiments/): the git history that the
\* checks read, the checks themselves, and the actions an author can take. The mapping from the
\* Python names to the names here is in docs/design/experiment-registry-tla.md.
\*
\* The model checks that the checks, taken together, keep a confirmation run one-shot, keep a
\* record that ran frozen, and keep the look numbers distinct. It does not check the statistics.

EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS
    Valid,            \* the file names of the records: 10 * number + slug
    MaxLooks,         \* MAX_LOOKS in experiments/looks.py
    ImmutableFields,  \* IMMUTABLE_FIELDS in experiments/models.py
    MaxCommits,       \* the longest history that the model builds
    EveryCommit,      \* TRUE compares every commit of the record with the registration commit
    FollowRenames     \* TRUE: the history of a record follows a rename (git log --follow)

ASSUME MaxLooks \in Nat /\ MaxLooks >= 1
ASSUME MaxCommits \in Nat /\ MaxCommits >= 2
ASSUME EveryCommit \in BOOLEAN
ASSUME FollowRenames \in BOOLEAN
\* The model keeps three immutable fields: the id, the look number, and one field (hyp) that
\* stands for every other field of the list.
ASSUME {"id", "look_number"} \subseteq ImmutableFields


\* A file with a name that is not a record id always fails the check, so the model has none.
ASSUME Valid \subseteq Nat
Names == Valid
Num(n) == n \div 10

Looks == 1 .. (MaxLooks + 1)
Statuses == {"reg", "ran", "wd"}

Blob(id, hyp, look, st, res) ==
    [id |-> id, hyp |-> hyp, look |-> look, st |-> st, res |-> res]

Imm(b) == <<b.id, b.hyp, b.look>>
\* A name with no file holds NONE. It has the shape of a blob, so TLC can compare the two.
NONE == Blob(0, 0, 0, "none", 0)
\* No immutable fields yet. A real record has a look number of at least 1.
NoImm == <<0, 0, 0>>
EmptyTree == [n \in Names |-> NONE]

VARIABLES
    commits,   \* the commits of the history, oldest first. A commit is a tree.
    renames,   \* renames[i]: the pairs <<old, new>> that git reports as renames in commit i
    pub,       \* how many commits are on main. Commits above pub are on a branch.
    frozen,    \* ghost, by file name: the immutable fields in the first published commit with a result
    seenRes    \* ghost, by file name: the results that a published commit held

vars == <<commits, renames, pub, frozen, seenRes>>

At(Cs, i) == IF i = 0 THEN EmptyTree ELSE Cs[i]
Tip == At(commits, Len(commits))

\* ---------------------------------------------------------------------------------------
\* What git reports. Git pairs a removed file and an added file as a rename when the two are
\* alike. A record has many fields, so the model calls two blobs alike when at most one of the
\* five fields differs. A rename that also edits a field is then not reported.

Alike(a, b) ==
    Cardinality({f \in {"id", "hyp", "look", "st", "res"} : a[f] = b[f]}) >= 4

Candidates(prev, cur) ==
    {<<q, p>> \in Names \X Names :
        /\ prev[q] # NONE /\ cur[q] = NONE
        /\ prev[p] = NONE /\ cur[p] # NONE
        /\ Alike(prev[q], cur[p])}

\* Git reports a pair only when no other pair shares the old or the new file.
Detect(prev, cur) ==
    IF ~FollowRenames THEN {}
    ELSE LET C == Candidates(prev, cur)
         IN {c \in C : \A d \in C : (d[1] = c[1] \/ d[2] = c[2]) => d = c}

\* The file name that "git log --follow" tracks at commit i, when it starts at x in the head.
RECURSIVE Track(_, _, _, _)
Track(Rs, n, x, i) ==
    IF i >= n THEN x
    ELSE LET p == Track(Rs, n, x, i + 1)
         IN IF \E q \in Names : <<q, p>> \in Rs[i + 1]
               THEN CHOOSE q \in Names : <<q, p>> \in Rs[i + 1]
               ELSE p

\* history._history: the commits that touched the file, and not the commits that deleted it.
Touches(Cs, Rs, x, i) ==
    LET n == Len(Cs)
        p == Track(Rs, n, x, i)
    IN At(Cs, i)[p] # NONE /\ At(Cs, i)[p] # At(Cs, i - 1)[p]

HistOf(Cs, Rs, x) == {i \in 1 .. Len(Cs) : Touches(Cs, Rs, x, i)}

\* history.registration_commit: the oldest commit that touched the file.
RegOf(Cs, Rs, x) == CHOOSE i \in HistOf(Cs, Rs, x) : \A j \in HistOf(Cs, Rs, x) : i <= j

BlobAt(Cs, Rs, x, i) == At(Cs, i)[Track(Rs, Len(Cs), x, i)]

\* ---------------------------------------------------------------------------------------
\* The checks. Each one is true when the check raises an error.

PresentIn(Cs) == {x \in Valid : At(Cs, Len(Cs))[x] # NONE}

\* The number of earlier records that spend a look, as looks.look_problem counts them.
Expected(T, x) ==
    1 + Cardinality({y \in Valid : T[y] # NONE /\ Num(y) < Num(x) /\ T[y].st # "wd"})

\* check._check_look and the schema bound of look_number.
LookError(T, x) ==
    \/ T[x].look \notin 1 .. MaxLooks
    \/ T[x].look # Expected(T, x)

\* history.check_immutable_fields. It compares the head with the registration commit. With
\* EveryCommit it compares each commit of the record too, so a field that changed and changed
\* back around a run is found.
ImmutableError(Cs, Rs, x) ==
    LET T == At(Cs, Len(Cs))
        r == RegOf(Cs, Rs, x)
        base == Imm(BlobAt(Cs, Rs, x, r))
    IN \/ Imm(T[x]) # base
       \/ EveryCommit /\ \E i \in HistOf(Cs, Rs, x) : Imm(BlobAt(Cs, Rs, x, i)) # base

\* history.check_one_shot.
OneShotError(Cs, Rs, x) ==
    LET T == At(Cs, Len(Cs))
    IN \E i \in HistOf(Cs, Rs, x) :
        /\ BlobAt(Cs, Rs, x, i).res # 0
        /\ BlobAt(Cs, Rs, x, i).res # T[x].res

\* withdrawn.status_problem.
WithdrawnError(Cs, Rs, x) ==
    LET T == At(Cs, Len(Cs))
    IN \E i \in HistOf(Cs, Rs, x) :
        \/ BlobAt(Cs, Rs, x, i).st = "wd" /\ T[x].st # "wd"
        \/ T[x].st = "wd" /\ BlobAt(Cs, Rs, x, i).st \notin {"reg", "wd"}

RecordError(Cs, Rs, x) ==
    LET T == At(Cs, Len(Cs))
    IN \/ T[x].id # x
       \/ LookError(T, x)
       \/ ImmutableError(Cs, Rs, x)
       \/ OneShotError(Cs, Rs, x)
       \/ WithdrawnError(Cs, Rs, x)

\* A commit i deleted the file y, and git did not report a rename of it.
Deleted(Cs, Rs, i, y) ==
    /\ At(Cs, i - 1)[y] # NONE /\ At(Cs, i)[y] = NONE
    /\ ~\E p \in Names : <<y, p>> \in Rs[i]

\* history.deleted_record_ids: a deleted record stays when it has a result, or when a record
\* with a higher number was in the commit that deleted it and in its parent.
DeletionError(Cs, Rs, y) ==
    /\ y \notin PresentIn(Cs)
    /\ \E i \in 1 .. Len(Cs) : Deleted(Cs, Rs, i, y)
    /\ \/ \E j \in 1 .. Len(Cs) : At(Cs, j)[y] # NONE /\ At(Cs, j)[y].res # 0
       \/ \E i \in 1 .. Len(Cs) :
            /\ Deleted(Cs, Rs, i, y)
            /\ \E z \in Valid :
                /\ At(Cs, i - 1)[z] # NONE /\ At(Cs, i)[z] # NONE /\ Num(z) > Num(y)

\* registry_checks.registry_errors, and the check of each record.
Passes(Cs, Rs) ==
    LET T == At(Cs, Len(Cs))
    IN /\ \A y \in Valid : ~DeletionError(Cs, Rs, y)
       /\ \A x \in PresentIn(Cs) : ~RecordError(Cs, Rs, x)

\* ---------------------------------------------------------------------------------------
\* The actions of an author. Each action changes the tree of the head commit.

Present(T) == {x \in Valid : T[x] # NONE}

\* check.run_experiment refuses a record whose look is wrong, and a record that ran.
RunOK(T, x) ==
    /\ T[x].st = "reg" /\ T[x].res = 0
    /\ T[x].id = x
    /\ ~LookError(T, x)

\* Add a new record. The author sets the id and the look number. A commit that is not the head of
\* the branch is not checked, so the id may differ from the file name.
Additions(T) ==
    { [T EXCEPT ![q[1]] = Blob(q[2], 0, q[3], "reg", 0)] :
        q \in {v \in Valid : T[v] = NONE} \X Valid \X Looks }

Edits(T) ==
    \* Edit an immutable field.
    { [T EXCEPT ![x].hyp = 1 - T[x].hyp] : x \in Present(T) }
    \* Withdraw a record, or return it to registered. This also clears a result by hand.
    \cup { [T EXCEPT ![x].st = s, ![x].res = 0] :
        <<x, s>> \in {p \in Present(T) \X {"reg", "wd"} : p[2] # T[p[1]].st} }
    \* Run the experiment. The run writes the status and the result.
    \cup { [T EXCEPT ![x].st = "ran", ![x].res = r] :
        <<x, r>> \in {v \in Present(T) : RunOK(T, v)} \X {1, 2} }
    \* Delete a record.
    \cup { [T EXCEPT ![x] = NONE] : x \in Present(T) }
    \* Rename a record to a free name. The author may fix the id and may edit a field too.
    \cup { [T EXCEPT ![q[1]] = NONE,
                     ![q[2]] = Blob(q[3], q[4], T[q[1]].look, T[q[1]].st,
                                    T[q[1]].res)] :
        q \in {p \in Present(T) \X {f \in Names : T[f] = NONE} \X Names \X {0, 1} :
                /\ p[3] \in {T[p[1]].id, p[2]}
                /\ p[4] \in {T[p[1]].hyp, 1 - T[p[1]].hyp}} }

Mutations(T) == Additions(T) \cup Edits(T)

Init ==
    /\ commits = <<>> /\ renames = <<>> /\ pub = 0
    /\ frozen = [x \in Valid |-> NoImm]
    /\ seenRes = [x \in Valid |-> {}]

\* A new commit on the branch.
Commit ==
    /\ Len(commits) < MaxCommits
    /\ \E t \in Mutations(Tip) :
        /\ commits' = Append(commits, t)
        /\ renames' = Append(renames, Detect(Tip, t))
    /\ UNCHANGED <<pub, frozen, seenRes>>

\* The commits of the branch become one commit.
SquashedCommits ==
    SubSeq(commits, 1, pub) \o <<Tip>>
SquashedRenames ==
    SubSeq(renames, 1, pub) \o <<Detect(At(commits, pub), Tip)>>

\* The branch merges to main when the checks pass on it. A squash merge makes the commits one.
Publish(Cs, Rs) ==
    /\ Passes(commits, renames)
    /\ commits' = Cs /\ renames' = Rs /\ pub' = Len(Cs)
    /\ LET new == pub + 1 .. Len(Cs)
           holds(i, x) == At(Cs, i)[x].res # 0
           first(x) == CHOOSE i \in new : holds(i, x) /\ \A j \in new : holds(j, x) => i <= j
       IN /\ frozen' = [x \in Valid |->
                IF frozen[x] # NoImm \/ ~\E i \in new : holds(i, x) THEN frozen[x]
                ELSE Imm(At(Cs, first(x))[x])]
          /\ seenRes' = [x \in Valid |->
                seenRes[x] \cup {At(Cs, i)[x].res : i \in {j \in new : holds(j, x)}}]

Merge == Len(commits) > pub /\ Publish(commits, renames)
SquashMerge == Len(commits) > pub /\ Publish(SquashedCommits, SquashedRenames)

Next == Commit \/ Merge \/ SquashMerge

Spec == Init /\ [][Next]_vars

\* ---------------------------------------------------------------------------------------
\* The properties. Each holds when the branch is merged and the checks pass.

OnMain == pub = Len(commits) /\ pub > 0

TypeOK ==
    /\ pub \in 0 .. Len(commits)
    /\ Len(commits) = Len(renames)

\* A file that git reported as renamed in some commit.
Moved(x) == \E i \in 1 .. Len(renames) : \E p \in Names : <<x, p>> \in renames[i]

\* A record whose result is on main keeps the immutable fields that it had then.
FrozenOnceRun ==
    OnMain /\ Passes(commits, renames) =>
        \A x \in Valid : frozen[x] # NoImm =>
            IF Tip[x] # NONE THEN Imm(Tip[x]) = frozen[x] ELSE Moved(x)

\* A confirmation runs once: a result on main stays, and no second result replaces it.
RunsOnce ==
    OnMain /\ Passes(commits, renames) =>
        \A x \in Valid : seenRes[x] # {} =>
            IF Tip[x] # NONE THEN seenRes[x] = {Tip[x].res} ELSE Moved(x)

\* Two records that ran never share a look number, so the tails of the looks sum below
\* BASE_TAIL.
DistinctLooks ==
    OnMain /\ Passes(commits, renames) =>
        \A x, y \in PresentIn(commits) :
            (x # y /\ Tip[x].res # 0 /\ Tip[y].res # 0) => Tip[x].look # Tip[y].look

\* No accepted history has more than MaxLooks records that spend a look.
LookCap ==
    OnMain /\ Passes(commits, renames) =>
        Cardinality({x \in PresentIn(commits) : Tip[x].st # "wd"}) <= MaxLooks

\* A record that ran is never withdrawn, so a withdrawn record spent no look.
WithdrawnNeverRan ==
    OnMain /\ Passes(commits, renames) =>
        \A x \in PresentIn(commits) :
            Tip[x].st = "wd" => seenRes[x] = {}

\* A squash merge leaves main green when the branch was green.
MainStaysGreen == OnMain => Passes(commits, renames)

\* A record on main with a result was registered, with no result, before it ran. The checks
\* do not enforce this.
RegisteredBeforeRun ==
    OnMain /\ Passes(commits, renames) =>
        \A x \in PresentIn(commits) :
            Tip[x].res # 0 =>
                LET b == BlobAt(commits, renames, x, RegOf(commits, renames, x))
                IN b.res = 0 /\ b.st = "reg"

=============================================================================
