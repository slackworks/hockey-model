------------------------------ MODULE store_upgrade ------------------------------
\* A model of one store upgrade (src/hockey_model/store/upgrade.py): the write lock, the copy,
\* the changes of each version in one transaction, the shape probe, the ledger row, the stamp,
\* and the commit. Two upgrade processes and a writer run at once, and a process can crash at
\* any step. The mapping from the Python steps to the actions is in docs/design/store-upgrade-tla.md.
\*
\* The model checks the order of the steps. It does not check what the change of a version does.

EXTENDS Naturals, FiniteSets

CONSTANTS
    SchemaVersion,  \* SCHEMA_VERSION in store/db.py
    Reqs,           \* the requirements of store/shape.py, as 10 * version + index in the version
    Procs,          \* the upgrade processes
    MaxRuns,        \* how many runs one process starts
    MaxRows,        \* how many rows the writer adds
    StaleStamp      \* TRUE lets a store start with a stamp ahead of its shape

ASSUME SchemaVersion \in Nat /\ SchemaVersion >= 2
ASSUME Reqs \subseteq Nat /\ \A r \in Reqs : r \div 10 \in 2 .. SchemaVersion

\* The number of requirements that version v added.
Needs(v) == Cardinality({r \in Reqs : r \div 10 = v})
ASSUME \A v \in 2 .. SchemaVersion : Needs(v) >= 1

Versions == 1 .. SchemaVersion

RECURSIVE Cum(_)
\* The requirements that a store holds when it holds the shape of version v.
Cum(v) == IF v = 0 THEN 0 ELSE Cum(v - 1) + Needs(v)
Total == Cum(SchemaVersion)

\* The newest version whose requirements the shape holds, as earned_version() reads it.
Earned(s) == CHOOSE v \in Versions : Cum(v) <= s /\ (v = SchemaVersion \/ Cum(v + 1) > s)

\* What _start_version believes: a stamp ahead of the shape is discounted to the shape.
StartOf(st, sh) == IF st > Earned(sh) THEN Earned(sh) ELSE st

VARIABLES
    stamp, shape, rows, ever, ledger, changes,  \* the committed store
    backup, backupRows, ledgerBackup,           \* the copy of the store
    committedByUpgrade,                         \* an upgrade has committed
    lock, pc, runs, start,                      \* the processes
    txShape, txNext, txUnit, txRows, txStamp, txLedger

vars == <<stamp, shape, rows, ever, ledger, changes, backup, backupRows, ledgerBackup,
          committedByUpgrade, lock, pc, runs, start, txShape, txNext, txUnit, txRows,
          txStamp, txLedger>>

Holding == {"locked", "copying", "copied", "applying", "probing", "ledgering", "stamping"}

Init ==
    /\ stamp \in Versions
    /\ shape \in IF StaleStamp THEN 0 .. Cum(stamp) ELSE {Cum(stamp)}
    /\ rows = {}
    /\ ever = {}
    /\ ledger = 0
    \* The shape records the changes that were committed before this model starts.
    /\ changes = [v \in Versions |-> IF v >= 2 /\ v <= Earned(shape) THEN 1 ELSE 0]
    /\ backup = "absent"
    /\ backupRows = {}
    /\ ledgerBackup = "none"
    /\ committedByUpgrade = FALSE
    /\ lock = "none"
    /\ pc = [p \in Procs |-> "idle"]
    /\ runs = [p \in Procs |-> 0]
    /\ start = [p \in Procs |-> 0]
    /\ txShape = 0 /\ txNext = 0 /\ txUnit = 0 /\ txRows = {} /\ txStamp = 0 /\ txLedger = 0

Other == UNCHANGED <<stamp, shape, rows, ever, ledger, changes, backup, backupRows,
                     ledgerBackup, committedByUpgrade>>
Tx == UNCHANGED <<txShape, txNext, txUnit, txRows, txStamp, txLedger>>
Procs_ == UNCHANGED <<runs, start>>

\* BEGIN IMMEDIATE. The lock is held until the commit, the rollback, or the end of the process.
TakeLock(p) ==
    /\ pc[p] = "idle" /\ runs[p] < MaxRuns /\ lock = "none"
    /\ lock' = p /\ pc' = [pc EXCEPT ![p] = "locked"] /\ runs' = [runs EXCEPT ![p] = @ + 1]
    /\ Other /\ Tx /\ UNCHANGED start

\* The lock is busy, so the run stops with an error and changes nothing.
LockBusy(p) ==
    /\ pc[p] = "idle" /\ runs[p] < MaxRuns /\ lock # "none"
    /\ runs' = [runs EXCEPT ![p] = @ + 1]
    /\ Other /\ Tx /\ UNCHANGED <<lock, pc, start>>

\* _read_version and _start_version. A store at the current version commits and ends the run.
ReadVersion(p) ==
    /\ pc[p] = "locked" /\ lock = p
    /\ LET s == StartOf(stamp, shape)
       IN /\ start' = [start EXCEPT ![p] = s]
          /\ IF s = SchemaVersion
                THEN /\ lock' = "none" /\ pc' = [pc EXCEPT ![p] = "done"]
                ELSE /\ UNCHANGED lock /\ pc' = [pc EXCEPT ![p] = "copying"]
    /\ Other /\ Tx /\ UNCHANGED runs

\* _backup refuses to run when the copy file exists. A complete copy has its final name. A
\* copy in progress has a temporary name, so "partial" never has the final name.
\* The run ends and the lock is released.
CopyRefused(p) ==
    /\ pc[p] = "copying" /\ lock = p /\ backup = "complete"
    /\ lock' = "none" /\ pc' = [pc EXCEPT ![p] = "idle"]
    /\ Other /\ Tx /\ Procs_

\* A temporary file that a killed run left is replaced.
CopyBegin(p) ==
    /\ pc[p] = "copying" /\ lock = p /\ backup # "complete"
    /\ backup' = "partial"
    /\ pc' = [pc EXCEPT ![p] = "copied"]
    /\ UNCHANGED <<stamp, shape, rows, ever, ledger, changes, backupRows, ledgerBackup,
                   committedByUpgrade, lock>> /\ Tx /\ Procs_

\* The copy is complete and holds the rows that the store holds now.
CopyEnd(p) ==
    /\ pc[p] = "copied" /\ lock = p /\ backup = "partial"
    \* The rename gives the complete copy its final name.
    /\ backup' = "complete" /\ backupRows' = rows
    /\ pc' = [pc EXCEPT ![p] = "applying"]
    /\ txShape' = shape /\ txNext' = start[p] + 1 /\ txUnit' = 0 /\ txRows' = rows
    /\ txStamp' = stamp /\ txLedger' = ledger
    /\ UNCHANGED <<stamp, shape, rows, ever, ledger, changes, ledgerBackup,
                   committedByUpgrade, lock>> /\ Procs_

\* One requirement of the version that is being applied. A version that restarts from a partial
\* shape rebuilds its tables, so the first requirement sets the shape and does not add to it.
ApplyUnit(p) ==
    /\ pc[p] = "applying" /\ lock = p /\ txNext <= SchemaVersion
    /\ txUnit < Needs(txNext)
    /\ txShape' = Cum(txNext - 1) + txUnit + 1 /\ txUnit' = txUnit + 1
    /\ UNCHANGED <<txNext, txRows, txStamp, txLedger>>
    /\ Other /\ UNCHANGED <<lock, pc>> /\ Procs_

NextVersion(p) ==
    /\ pc[p] = "applying" /\ lock = p /\ txNext <= SchemaVersion
    /\ txUnit = Needs(txNext)
    /\ txNext' = txNext + 1 /\ txUnit' = 0
    /\ UNCHANGED <<txShape, txRows, txStamp, txLedger>>
    /\ Other /\ UNCHANGED <<lock, pc>> /\ Procs_

\* The views are rebuilt, then _refuse_a_short_shape reads the probe.
Views(p) ==
    /\ pc[p] = "applying" /\ lock = p /\ txNext > SchemaVersion
    /\ pc' = [pc EXCEPT ![p] = "probing"]
    /\ Other /\ Tx /\ UNCHANGED <<lock, runs, start>>

Probe(p) ==
    /\ pc[p] = "probing" /\ lock = p /\ Earned(txShape) >= SchemaVersion
    /\ pc' = [pc EXCEPT ![p] = "ledgering"]
    /\ Other /\ Tx /\ UNCHANGED <<lock, runs, start>>

WriteLedger(p) ==
    /\ pc[p] = "ledgering" /\ lock = p
    /\ txLedger' = ledger + 1 /\ pc' = [pc EXCEPT ![p] = "stamping"]
    /\ UNCHANGED <<txShape, txNext, txUnit, txRows, txStamp>>
    /\ Other /\ UNCHANGED <<lock, runs, start>>

WriteStamp(p) ==
    /\ pc[p] = "stamping" /\ lock = p /\ txStamp # SchemaVersion
    /\ txStamp' = SchemaVersion
    /\ UNCHANGED <<txShape, txNext, txUnit, txRows, txLedger>>
    /\ Other /\ UNCHANGED <<lock, pc, runs, start>>

\* COMMIT. The shape, the rows, the ledger row, and the stamp become visible together.
Commit(p) ==
    /\ pc[p] = "stamping" /\ lock = p /\ txStamp = SchemaVersion
    /\ stamp' = txStamp /\ shape' = txShape /\ rows' = txRows /\ ledger' = txLedger
    /\ changes' = [v \in Versions |->
                     IF v > start[p] /\ v >= 2 THEN changes[v] + 1 ELSE changes[v]]
    /\ ledgerBackup' = backup
    /\ committedByUpgrade' = TRUE
    /\ lock' = "none" /\ pc' = [pc EXCEPT ![p] = "done"]
    /\ UNCHANGED <<ever, backup, backupRows>> /\ Tx /\ Procs_

\* A change raises an error. The transaction rolls back and the run ends. The copy stays.
Fail(p) ==
    /\ pc[p] \in {"applying", "probing", "ledgering", "stamping"} /\ lock = p
    /\ lock' = "none" /\ pc' = [pc EXCEPT ![p] = "idle"]
    /\ Other /\ Tx /\ Procs_

\* The process dies. SQLite drops the open transaction and the lock. A copy that was not
\* finished stays as a partial file.
Crash(p) ==
    /\ pc[p] \in Holding /\ lock = p
    /\ lock' = "none" /\ pc' = [pc EXCEPT ![p] = "idle"]
    /\ Other /\ Tx /\ Procs_

\* The operator moves the copy aside, as the error of _backup asks.
OperatorMovesCopy ==
    /\ lock = "none" /\ backup # "absent"
    /\ backup' = "absent" /\ backupRows' = {}
    /\ UNCHANGED <<stamp, shape, rows, ever, ledger, changes, ledgerBackup, committedByUpgrade,
                   lock, pc, runs, start>> /\ Tx

\* A refresh adds a row. It needs the write lock.
Writer ==
    /\ lock = "none" /\ Cardinality(ever) < MaxRows
    /\ LET r == Cardinality(ever) + 1
       IN /\ rows' = rows \cup {r} /\ ever' = ever \cup {r}
    /\ UNCHANGED <<stamp, shape, ledger, changes, backup, backupRows, ledgerBackup,
                   committedByUpgrade, lock, pc, runs, start>> /\ Tx

Next ==
    \/ \E p \in Procs :
        \/ TakeLock(p) \/ LockBusy(p) \/ ReadVersion(p) \/ CopyRefused(p) \/ CopyBegin(p)
        \/ CopyEnd(p) \/ ApplyUnit(p) \/ NextVersion(p) \/ Views(p) \/ Probe(p)
        \/ WriteLedger(p) \/ WriteStamp(p) \/ Commit(p) \/ Fail(p) \/ Crash(p)
    \/ OperatorMovesCopy
    \/ Writer

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ stamp \in Versions /\ shape \in 0 .. Total /\ ledger \in Nat
    /\ backup \in {"absent", "partial", "complete"}
    /\ lock \in Procs \cup {"none"}

\* A stamp never names a shape that the store does not hold, once an upgrade has committed.
StampNotAhead == committedByUpgrade => stamp <= Earned(shape)

\* After an upgrade commits, the shape is the shape of a whole version.
NoHalfApplied == committedByUpgrade => \E v \in Versions : shape = Cum(v)

\* The change of a version is committed once, and the committed changes are the shape.
NoVersionTwice ==
    /\ \A v \in Versions : changes[v] <= 1
    /\ shape <= Total /\ txShape <= Total
    /\ committedByUpgrade => \A v \in 2 .. SchemaVersion : changes[v] = IF v <= Earned(shape) THEN 1 ELSE 0

\* Every row that was committed stays.
NoRowLost == ever \subseteq rows

\* A complete copy holds the rows of the store while a process holds the lock after the copy.
\* No write lands between the copy and the commit.
CopyMatchesStore ==
    \A p \in Procs : pc[p] \in {"applying", "probing", "ledgering", "stamping"}
        => backup = "complete" /\ backupRows = rows

\* A refusal names a complete copy. A copy that a crash left half written never blocks a run.
RefusalOnlyForCompleteCopy ==
    [][(\E p \in Procs : CopyRefused(p)) => backup = "complete"]_vars

\* The ledger names a copy that was complete, never a partial one.
LedgerNamesCompleteCopy == ledgerBackup \in {"none", "complete"}

\* A ledger row exists only for a store that holds the current stamp.
LedgerMatchesStamp == ledger > 0 => stamp = SchemaVersion

\* A run that ends leaves a store whose stamp does not name a shape that the store lacks.
DoneStoreIsWhole == \A p \in Procs : pc[p] = "done" => stamp <= Earned(shape)

\* At most one process holds the lock.
OneHolder == Cardinality({p \in Procs : pc[p] \in Holding}) <= 1
================================================================================
