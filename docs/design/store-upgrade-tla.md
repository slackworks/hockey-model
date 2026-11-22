# TLA+ model of the store upgrade

Issue: #263.
Files: `specs/store_upgrade.tla`, `specs/store_upgrade.cfg`, the `upgrade` shard of `specs/shards.json`.
Code: `src/hockey_model/store/upgrade.py`, with `store/db.py`, `store/migrations.py`, and `store/shape.py`.

## Problem

`upgrade_store` is the only producer step that changes a store file.
Its safety rests on the order of its steps, and no unit test covers that order well.
The rules are these.

1. The write lock is taken before the version is read and before the copy, so no write lands between the copy and the migration.
2. The changes of all versions, the view rebuild, the shape probe, the ledger row, and the stamp run in one transaction.
3. A run that stops at any step leaves the store at the old version, with a complete copy or no copy.
4. A repeated run does not apply a change twice.
5. A stamp that is ahead of the shape is discounted to the shape, so the missing changes run again.
6. No row is lost.

A change of `upgrade.py` can break one of these rules without a test that fails.

## Options

1. More unit tests of the failure cases. They show one order at a time, and they cannot show a crash between any two steps with a second process.
2. A property test over a fake store. It needs a model of the crash points, which is the model that this note describes.
3. A TLA+ model checked by TLC. It tries every order of the steps of two processes and a writer, with a crash at every step.

## Choice

Option 3, as for the daily prepare backfill (`docs/design/daily-prepare-tla.md`).
The model checks the order of the steps.
It does not check what the change of a version does.
The contents of a migration, and the rule that an unknown new field stays unknown, stay with the unit tests of #81.

## Mapping from the code to the model

| Python step | Action |
| --- | --- |
| `_lock`: `BEGIN IMMEDIATE` | `TakeLock`, or `LockBusy` when another process holds the lock |
| `_read_version`, `_start_version`, `earned_version` | `ReadVersion`: the start version is the stamp, or the version that the shape earns when the stamp is ahead |
| `stored == SCHEMA_VERSION`: `COMMIT` and return | `ReadVersion` with the lock released and the run done |
| `_backup`: the copy file exists | `CopyRefused`: only a complete copy has the final name |
| `_backup`: the copy under the temporary name | `CopyBegin`, which replaces a temporary file that a killed run left |
| `_backup`: the rename | `CopyEnd`: the copy is complete and holds the rows of the store |
| `_backup`: the copy raises an error | `CopyFails`: the partial file is removed, the lock is released, and the run ends |
| `_drop_views` | `DropViews`, before the first change. A change cannot rebuild a table that a view names |
| The loop over `MIGRATIONS[version]` | `ApplyUnit` for each requirement of a version, and `NextVersion` |
| The view script | `Views`, after the last change |
| `_refuse_a_short_shape` | `Probe` |
| The `INSERT` into `schema_migrations` | `WriteLedger` |
| `UPDATE schema_version` | `WriteStamp` |
| `COMMIT` | `Commit`: shape, rows, ledger row, and stamp become visible together |
| `_rollback` after a `StoreError` in the changes | `Fail`: the transaction ends, the lock is released, the copy stays |
| A killed process | `Crash`: SQLite drops the open transaction and the lock |
| The operator moves the copy aside | `OperatorMovesCopy` |
| A refresh adds a row | `Writer`, which needs the lock |

A version adds requirements as `shape.py` lists them.
The config names each requirement as `10 * version + index`, so the model holds the same version chain as the code, with the two versions that add two requirements.
A change either gives the table the shape of its requirement, or it only creates what is absent and leaves a table with the wrong shape as it is (`CREATE TABLE IF NOT EXISTS`).
The model lets each change do either, so the shape can stay behind and the probe can refuse the run.

## State

The committed store has the stamp, the shape (the number of requirements that it holds), the rows, the ledger, and the count of committed changes of each version.
The copy file is absent, partial (a temporary name), or complete, and it records the rows that it holds.
Each of two processes has a program counter and a run count, and the open transaction has its own shape, rows, stamp, and ledger.
`StaleStamp` lets a store start with a stamp ahead of its shape, as an upgrade before #272 could leave it.

## Properties

| Property | Meaning |
| --- | --- |
| `StampNotAhead` | After an upgrade commits, the stamp does not exceed the version that the shape earns |
| `NoHalfApplied` | After an upgrade commits, the shape is the shape of a whole version |
| `NoVersionTwice` | The change of a version is committed once, and the committed changes are the shape |
| `NoRowLost` | A row that was committed stays |
| `CopyMatchesStore` | While a process holds the lock after the copy, the complete copy holds the rows of the store, so no write lands between the copy and the commit |
| `RefusalOnlyForCompleteCopy` | A run refuses to start only for a complete copy, so a copy that a crash left half written never blocks an upgrade |
| `LedgerNamesCompleteCopy` | The ledger names a copy that was complete |
| `LedgerMatchesStamp` | A ledger row exists only for a store that holds the current stamp |
| `DoneStoreIsWhole` | A run that ends leaves a stamp that does not name a shape that the store lacks |
| `OneHolder` | At most one process holds the lock |

## Result

TLC finds no violation.
The check takes seconds on one machine, with 2,700,705 distinct states and a depth of 38 for the accepted config.
Each break test below stops with the violation that the table names.

### What the model found

The first version of the model followed the code, and it showed that a process that dies in the copy leaves a partial file under the final name of the copy.
The next run refused with "backup already exists; move it aside so the earlier backup is kept", and it called a half-written file an earlier backup.
No data was lost, because the refusal runs before any change, but the operator was told to keep a file that is not a backup.
`_backup` now copies to `<store>.v<version>.bak.tmp` and renames the file when the copy is complete, so a file with the final name is complete.
A temporary file that a killed run left is replaced by the next run, and the daily workflows delete it with the backups.
The break test "the copy is written under its final name" shows the earlier rule.

No other ordering rule of the upgrade was found unenforced.

### Not modeled

- The contents of a change, and the rule that an unknown new field stays unknown instead of backfilled (#81).
- The read path of a consumer, which `connect()` answers by refusing a stamp that does not match the shape.
- A disk that fills, or a rollback that fails. `Fail` and `Crash` stand for both, and the code names the second case in its message.
- The refusals that end a run before the copy: a stamp above `SCHEMA_VERSION`, a version with no entry in `MIGRATIONS`, and the NULL check (4 to 5). They change nothing, so the order of the refusals and the copy is checked by the unit tests of `upgrade.py`.
- A directory sync after the rename of the copy.
- A change that runs twice. `ApplyUnit` treats a re-run as safe, but the changes 5 to 6, 6 to 7, and 7 to 8 are not (`ADD COLUMN`, `DROP COLUMN`, and a select of dropped columns). A second run fails and rolls back, and no current path makes such a store.

## Break tests

Each break edits a scratch copy of the spec, and TLC must stop with a violation.
The edit is the code change that the break names.

| Break | Edit | TLC stops with |
| --- | --- | --- |
| The upgrade can commit (reachability) | Add the invariant `~committedByUpgrade` | `NeverUpgraded` |
| The lock is released after the version is read, so the copy and the changes run without it | `ReadVersion` releases the lock, and the later actions drop `lock = p` | `OneHolder` |
| The stamp is committed before the probe | `Views` sets the stamp in the committed store | `StampNotAhead` |
| The ledger row is committed outside the transaction | `WriteLedger` sets the committed ledger | `LedgerMatchesStamp` |
| The run starts at version 1 and ignores the stored version | `ReadVersion` starts at version 1 | `NoVersionTwice` |
| The writer ignores the lock | `Writer` drops `lock = "none"` | `CopyMatchesStore` |
| A change drops a row | `ApplyUnit` empties the rows of the transaction | `NoRowLost` |
| The probe is skipped | `Probe` has no guard on the shape | `StampNotAhead` |
| No step drops the views before the changes | `DropViews` is removed, so no change can run | `NeverUpgraded` holds: the upgrade can never commit |
| Each requirement is committed on its own | `ApplyUnit` commits the shape | `NoVersionTwice` |
| The migration starts before the copy is complete | `CopyBegin` goes to the changes and no copy ends | `CopyMatchesStore` |
| Two upgrades start at once | `TakeLock` and the later actions drop the lock | `OneHolder` |
| The copy is written under its final name | `CopyRefused` fires for a partial copy | `RefusalOnlyForCompleteCopy` |
| The stamp is trusted without the probe | `StartOf` returns the stamp | `DoneStoreIsWhole` |

A copy that is taken after the lock is released is the second row.

## Run

`bash scripts/tlc.sh --shard upgrade` runs the model, and `bash scripts/tlc.sh` runs every shard.
The `upgrade` shard starts when `store/upgrade.py`, `store/db.py`, `store/migrations.py`, `store/shape.py`, `store/schema.sql`, or a file of the spec changes.
`tests/test_tla_store_upgrade.py` checks that the config holds `SCHEMA_VERSION` and the requirements of `shape.py`, and that `MIGRATIONS` is the chain that the model assumes.
