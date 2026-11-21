"""Upgrade a store file to the current schema version, keeping every row.

Only this producer step migrates a store. The open path in ``store/db.py`` validates the
version and never changes the file. The upgrade takes the write lock, copies the store
with SQLite's backup API, applies the ordered changes in one transaction, and records each
run in ``schema_migrations``. The lock is taken before the copy, so no write can land
between the backup and the migration.

A version 1 store has no attempt counts. The upgrade rebuilds ``mp_team_games`` so that its
rows keep their values, the counts are NULL, and ``attempts_known`` is 0. No count is
filled in, because the value at the old fetch time is unknown.

The version stamp is written in the same transaction as every change, so a stamp cannot name
a shape the store does not hold. A stamp that is already ahead of the shape is caught by a
column probe, and the upgrade reruns every change from version 1.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from stat import S_ISREG

from hockey_model.store.db import SCHEMA_VERSION, StoreError
from hockey_model.store.migrations import MIGRATIONS, NULLABLE_BEFORE_V5
from hockey_model.store.shape import Requirement, earned_version
from hockey_model.store.sql_text import statements as script_statements
from hockey_model.store.timestamps import iso_utc

_LEDGER = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    " from_version INTEGER NOT NULL, to_version INTEGER NOT NULL,"
    " applied_at TEXT NOT NULL, backup_path TEXT NOT NULL)"
)
_VIEWS_PATH = Path(__file__).with_name("views.sql")
# The MoneyPuck team file is read on the weekly schedule, so after an upgrade the
# attempt counts arrive only at the next weekly read unless the reread flag forces it.
_NEXT_STEP = (
    "run `hockey-model refresh --reread-moneypuck` before any attempt-form backtest; "
    "until it runs, the attempt-form features are empty and every game counts as missing"
)
_SKIP_NOTE = "no store file there yet; the first refresh creates it at the current version"
# Only the 1->2 step writes an empty count. The decision to ask for a reread is taken from
# the data below, because a version 2 store that a 1->2 step made can reach a later version
# with its counts still NULL.
_EMPTY_ATTEMPTS_FROM = 1
# The view, not the table: a reread appends a counted row and leaves the v1 row in place, so
# a table count would ask for the reread again forever.
_EMPTY_ATTEMPTS_SQL = "SELECT COUNT(*) FROM latest_mp_team_games WHERE attempts_known = 0"


@dataclass(frozen=True)
class UpgradeReport:
    """The outcome of one upgrade run.

    ``backup_path`` is the backup this run wrote. When nothing changed, it is the backup of
    the last upgrade that the ledger records, or None when there is none. The workflow drops
    that file after the run, so a no-op report can name a backup that is gone.
    ``from_version`` is None when there is no store file and the caller allowed a skip.
    ``next_step`` is a command to run, or None. ``note`` is a statement about the run.
    """

    path: str
    from_version: int | None
    to_version: int
    changed: bool
    backup_path: str | None
    next_step: str | None = None
    note: str | None = None


def _check_path(path: Path, if_missing: str) -> bool:
    """Return True when the caller allows a skip of an absent store. Refuse any other path.

    ``sqlite3.connect`` would create an empty file. This function is the one place that
    decides whether a store exists, so a caller does not resolve the path a second way.
    """
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        if if_missing == "skip":
            return True
        raise StoreError(
            f"upgrade store {str(path)!r}: no store file there; check the path given to --db "
            "or HOCKEY_MODEL_DB"
        ) from None
    except OSError as exc:
        raise StoreError(
            f"upgrade store {str(path)!r}: cannot read the path: {exc}; check the "
            "permissions of the directory that holds it"
        ) from exc
    if not S_ISREG(mode):
        raise StoreError(
            f"upgrade store {str(path)!r}: the path is not a regular file, such as a "
            "directory; name the store file, such as <dir>/store.db"
        )
    return False


def _lock(conn: sqlite3.Connection, path: Path) -> None:
    """Take the write lock. A busy store is named as busy, so a retry is the fix."""
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        raise StoreError(
            f"upgrade store {str(path)!r}: cannot take the write lock: {exc}; the file is "
            "unchanged. Stop the other process that uses the store, then run the upgrade again."
        ) from exc


def _read_version(conn: sqlite3.Connection, path: Path) -> int:
    """Return the stored version. Name the SQLite error when the version cannot be read."""
    try:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
    except sqlite3.Error as exc:
        raise StoreError(
            f"upgrade store {str(path)!r}: cannot read schema_version: {exc}; the file is "
            "unchanged. Check that it is a hockey-model store and that no other process holds it"
        ) from exc
    if row is None:
        raise StoreError(
            f"upgrade store {str(path)!r}: schema_version has no row, so the store is "
            "half-initialized and cannot be upgraded; restore it from a backup or use a new path"
        )
    return int(row[0])


# The two statements that read the ledger, and the one that reports each failure. A missing
# table is the expected first-run case. Any other error means the file itself is in doubt, so
# it must not read as "this store has no backup".
_LEDGER_EXISTS = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
_LAST_BACKUP = "SELECT backup_path FROM schema_migrations ORDER BY rowid DESC LIMIT 1"


def _last_backup(conn: sqlite3.Connection, path: Path) -> str | None:
    """Return the backup named by the newest ledger row, or None when there is no ledger."""
    if conn.execute(_LEDGER_EXISTS).fetchone() is None:
        return None
    try:
        row = conn.execute(_LAST_BACKUP).fetchone()
    except sqlite3.Error as exc:
        raise StoreError(
            f"upgrade store {str(path)!r}: cannot read the upgrade ledger: {exc}; the file "
            "may be damaged. Start from a copy and run the upgrade again."
        ) from exc
    return None if row is None else str(row[0])


def _backup(path: Path, version: int) -> Path:
    """Copy the store with SQLite's backup API. Remove a partial copy when the copy fails."""
    target = path.with_name(f"{path.name}.v{version}.bak")
    if target.exists():
        raise StoreError(
            f"upgrade store {str(path)!r}: backup {str(target)!r} already exists; move it aside "
            "so the earlier backup is kept, then run the upgrade again"
        )
    source = sqlite3.connect(path)
    dest = sqlite3.connect(target)
    try:
        source.backup(dest)
    except sqlite3.Error as exc:
        dest.close()
        target.unlink(missing_ok=True)
        raise StoreError(
            f"upgrade store {str(path)!r}: backup failed: {exc}; the store is unchanged"
        ) from exc
    finally:
        source.close()
        dest.close()
    return target


def _drop_views(conn: sqlite3.Connection) -> None:
    """Drop every view. A view that names a rebuilt table would block the rebuild."""
    names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")]
    for name in names:
        conn.execute(f"DROP VIEW IF EXISTS {name}")


def _refuse_a_null_column(conn: sqlite3.Connection, path: Path) -> None:
    """Stop the upgrade when a row holds a NULL that the current shape refuses.

    The 4->5 step adds NOT NULL to two columns that an earlier store wrote without it. The
    rebuild cannot fill a NULL with a true value, so it names the table and the count instead.
    """
    for table, column in NULLABLE_BEFORE_V5:
        query = f"SELECT COUNT(*) FROM {table} WHERE {column} IS NULL"
        try:
            count = int(conn.execute(query).fetchone()[0])
        except sqlite3.Error as exc:
            raise StoreError(
                f"upgrade store {str(path)!r}: cannot read {table}.{column}: {exc}; the file "
                "does not hold the shape that version 1 wrote. Restore it from a backup, or "
                "use a new path"
            ) from exc
        if count:
            raise StoreError(
                f"upgrade store {str(path)!r}: {table} holds {count} rows with a NULL "
                f"{column}, and the current shape needs a value there; the rebuild cannot "
                "invent one. Set the value in those rows, then run the upgrade again."
            )


def _apply(conn: sqlite3.Connection, path: Path, stored: int, backup: Path) -> None:
    """Run every change, the views, the ledger row, and the stamp, then commit.

    The view rebuild and the shape check run inside the transaction. A view that names a
    column the new shape lacks raises here, and a column that a step failed to add is named
    here, both before the stamp is written. The store therefore cannot be left stamped at a
    version whose shape it does not hold.
    """
    try:
        _drop_views(conn)
        for version in range(stored, SCHEMA_VERSION):
            for statement in MIGRATIONS[version]:
                conn.execute(statement)
        for statement in script_statements(_VIEWS_PATH.read_text(encoding="utf-8")):
            conn.execute(statement)
        _refuse_a_short_shape(conn)
        conn.execute(_LEDGER)
        conn.execute(
            "INSERT INTO schema_migrations (from_version, to_version, applied_at, backup_path)"
            " VALUES (?, ?, ?, ?)",
            (stored, SCHEMA_VERSION, iso_utc(datetime.now(UTC)), str(backup)),
        )
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
        conn.execute("COMMIT")
    except (sqlite3.Error, OSError, StoreError) as exc:
        raise StoreError(
            f"upgrade store {str(path)!r}: {exc}; {_rollback(conn)} The backup is "
            f"{str(backup)!r}. Fix the cause, move the backup aside, then run the "
            "upgrade again."
        ) from exc


def _refuse_a_short_shape(conn: sqlite3.Connection) -> None:
    """Stop the upgrade when a step did not add the column that its version needs.

    A step that only creates a table cannot repair a table that exists with the wrong shape,
    so a run of such steps can pass while the store keeps the defect that started it. The
    check reads the same probe that discounts the stamp, so the two cannot disagree.
    """
    earned, missing = earned_version(conn)
    if missing is not None and earned < SCHEMA_VERSION:
        raise StoreError(
            f"the steps ran, but {missing.table} still lacks {missing.column}, so the store "
            f"holds version {earned} and not {SCHEMA_VERSION}; restore the backup"
        )


def _rollback(conn: sqlite3.Connection) -> str:
    """Roll the changes back, and return a sentence that states the outcome.

    A rollback that fails is the one case where the store may be part migrated, so its error
    must reach the operator beside the cause and the backup, not replace them.
    """
    if not conn.in_transaction:
        return "the store is unchanged."
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error as exc:
        return f"the rollback also failed: {exc}; the store may be partly migrated."
    return "the store is unchanged."


def upgrade_store(path: Path, *, if_missing: str = "fail") -> UpgradeReport:
    """Upgrade the store at ``path`` to ``SCHEMA_VERSION``. Return what it did.

    ``if_missing`` accepts ``fail``, which names an absent store, or ``skip``, which returns
    an unchanged report so a first run can continue to the refresh that creates the store.
    """
    if _check_path(path, if_missing):
        return UpgradeReport(str(path), None, SCHEMA_VERSION, False, None, note=_SKIP_NOTE)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        # The write lock is held before the version is read and before the copy is made.
        _lock(conn, path)
        stored, note = _start_version(conn, path)
        if stored == SCHEMA_VERSION:
            conn.execute("COMMIT")
            return UpgradeReport(
                str(path), stored, SCHEMA_VERSION, False, _last_backup(conn, path), note=note
            )
        # The NULL check runs before the copy, so its "run the upgrade again" is not blocked
        # by the backup file that a failed run leaves behind.
        if stored < 5:
            _refuse_a_null_column(conn, path)
        backup = _backup(path, stored)
        _apply(conn, path, stored, backup)
        next_step = _NEXT_STEP if _needs_reread(conn, stored) else None
    finally:
        conn.close()
    return UpgradeReport(str(path), stored, SCHEMA_VERSION, True, str(backup), next_step, note)


def _start_version(conn: sqlite3.Connection, path: Path) -> tuple[int, str | None]:
    """Return the version to migrate from, and a note when the stamp was not trusted.

    A stamp at the current version is checked against the shape before it is believed. A
    stamp that names a shape the store does not hold is discounted to the version the shape
    earns, so every change between the two runs again. A version this code cannot reach is
    refused here, while the transaction is still open and nothing has changed.
    """
    stored = _read_version(conn, path)
    note: str | None = None
    if stored > SCHEMA_VERSION:
        raise StoreError(
            f"upgrade store {str(path)!r}: no upgrade path from version {stored} to "
            f"{SCHEMA_VERSION}; use a hockey-model that reads version {stored}, or a new path"
        )
    earned, missing = earned_version(conn)
    if missing is not None and earned < stored:
        stored, note = earned, _stamp_ahead_note(path, earned, missing)
    if stored == SCHEMA_VERSION:
        return stored, note
    if stored not in MIGRATIONS:
        raise StoreError(
            f"upgrade store {str(path)!r}: no upgrade path from version {stored} to "
            f"{SCHEMA_VERSION}; use a hockey-model that reads version {stored}, or a new path"
        )
    return stored, note


def _stamp_ahead_note(path: Path, earned: int, missing: Requirement) -> str:
    """Name the store whose stamp was ahead of its shape, the missing column, and the version.

    The caller has already found that the tables earn less than the stamp, so a requirement
    is always the reason.
    """
    return (
        f"the stamp of {str(path)!r} named a newer version than its tables hold; the tables "
        f"earn version {earned}, because version {missing.version} needs "
        f"{missing.table}.{missing.column}. Every change from version {earned} runs again, "
        "and the ledger records it."
    )


def _needs_reread(conn: sqlite3.Connection, stored: int) -> bool:
    """Return True when the upgrade left a MoneyPuck attempt count empty.

    The 1->2 step sets the counts of the rows it moved to NULL, and no later step fills them.
    The answer comes from the data, not from the starting version, because a version 2 store
    that the 1->2 step made can reach a later version with its counts still NULL. A reread
    adds a counted row beside the old one, so the check reads the newest read of each team
    game, as the model does.
    """
    if stored < _EMPTY_ATTEMPTS_FROM + 1:
        return True
    row = conn.execute(_EMPTY_ATTEMPTS_SQL).fetchone()
    return row is not None and int(row[0]) > 0
