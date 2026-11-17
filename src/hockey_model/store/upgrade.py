"""Upgrade a store file to the current schema version, keeping every row.

Only this producer step migrates a store. The open path in ``store/db.py`` validates the
version and never changes the file. The upgrade takes the write lock, copies the store
with SQLite's backup API, applies the ordered changes in one transaction, and records each
run in ``schema_migrations``. The lock is taken before the copy, so no write can land
between the backup and the migration.

A version 1 store has no attempt counts. The upgrade rebuilds ``mp_team_games`` so that its
rows keep their values, the counts are NULL, and ``attempts_known`` is 0. No count is
filled in, because the value at the old fetch time is unknown.
"""

import argparse
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from hockey_model.store.db import SCHEMA_VERSION, StoreError
from hockey_model.store.timestamps import iso_utc

_MP_TEAM_GAMES_V2 = """
CREATE TABLE mp_team_games_v2 (
    game_id INTEGER,
    team TEXT,
    game_date TEXT,
    xg_for REAL,
    xg_against REAL,
    shot_attempts_for INTEGER,
    shot_attempts_against INTEGER,
    unblocked_for INTEGER,
    unblocked_against INTEGER,
    attempts_known INTEGER NOT NULL DEFAULT 1 CHECK (attempts_known IN (0, 1)),
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (game_id, team, fetched_at),
    CHECK (
        attempts_known = 0
        OR (shot_attempts_for IS NOT NULL AND shot_attempts_against IS NOT NULL
            AND unblocked_for IS NOT NULL AND unblocked_against IS NOT NULL)
    )
)
"""

# The change from each version to the next, in order. Each change is one statement. The
# views are dropped before the first change and rebuilt after the commit.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        _MP_TEAM_GAMES_V2,
        "INSERT INTO mp_team_games_v2 (game_id, team, game_date, xg_for, xg_against,"
        " fetched_at, attempts_known)"
        " SELECT game_id, team, game_date, xg_for, xg_against, fetched_at, 0"
        " FROM mp_team_games",
        "DROP TABLE mp_team_games",
        "ALTER TABLE mp_team_games_v2 RENAME TO mp_team_games",
        "CREATE INDEX IF NOT EXISTS mp_team_games_team ON mp_team_games (team)",
    ),
    2: (
        "CREATE TABLE IF NOT EXISTS fit_labels ("
        " label TEXT NOT NULL,"
        " fit_id INTEGER NOT NULL REFERENCES model_fits (fit_id),"
        " set_at TEXT NOT NULL)",
    ),
}
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


@dataclass(frozen=True)
class UpgradeReport:
    """The outcome of one upgrade run.

    ``backup_path`` is the backup this run wrote. When nothing changed, it is the backup of
    the last upgrade that the ledger records, or None when there is none.
    """

    path: str
    from_version: int
    to_version: int
    changed: bool
    backup_path: str | None
    next_step: str | None = None


def _check_path(path: Path) -> None:
    """Refuse a missing file. ``sqlite3.connect`` would create an empty one."""
    if not path.is_file():
        raise StoreError(
            f"upgrade store {str(path)!r}: no store file there; check the path given to --db "
            "or HOCKEY_MODEL_DB"
        )


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


def _last_backup(conn: sqlite3.Connection) -> str | None:
    """Return the backup named by the newest ledger row, or None when there is no ledger."""
    try:
        row = conn.execute(
            "SELECT backup_path FROM schema_migrations ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
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


def _apply(conn: sqlite3.Connection, path: Path, stored: int, backup: Path) -> None:
    """Run the changes and the ledger row, then commit. Roll back on any error."""
    try:
        _drop_views(conn)
        for version in range(stored, SCHEMA_VERSION):
            for statement in MIGRATIONS[version]:
                conn.execute(statement)
        conn.execute(_LEDGER)
        conn.execute(
            "INSERT INTO schema_migrations (from_version, to_version, applied_at, backup_path)"
            " VALUES (?, ?, ?, ?)",
            (stored, SCHEMA_VERSION, iso_utc(datetime.now(UTC)), str(backup)),
        )
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise StoreError(
            f"upgrade store {str(path)!r}: {exc}; the store is unchanged. The backup is "
            f"{str(backup)!r}. Fix the cause, move the backup aside, then run the "
            "upgrade again."
        ) from exc


def _rebuild_views(conn: sqlite3.Connection, path: Path, backup: Path) -> None:
    """Rebuild the views after the commit. A failure here leaves the upgrade in place."""
    try:
        conn.executescript(_VIEWS_PATH.read_text(encoding="utf-8"))
    except (sqlite3.Error, OSError) as exc:
        raise StoreError(
            f"upgrade store {str(path)!r}: the schema is upgraded, but the views failed to "
            f"rebuild: {exc}. The backup is {str(backup)!r}. Open the store once with the "
            "current hockey-model to rebuild the views."
        ) from exc


def upgrade_store(path: Path) -> UpgradeReport:
    """Upgrade the store at ``path`` to ``SCHEMA_VERSION``. Return what it did."""
    _check_path(path)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        # The write lock is held before the version is read and before the copy is made.
        _lock(conn, path)
        stored = _read_version(conn, path)
        if stored == SCHEMA_VERSION:
            conn.execute("COMMIT")
            return UpgradeReport(str(path), stored, SCHEMA_VERSION, False, _last_backup(conn))
        if stored > SCHEMA_VERSION or stored not in MIGRATIONS:
            conn.execute("ROLLBACK")
            raise StoreError(
                f"upgrade store {str(path)!r}: no upgrade path from version {stored} to "
                f"{SCHEMA_VERSION}; use a hockey-model that reads version {stored}, or a new path"
            )
        backup = _backup(path, stored)
        _apply(conn, path, stored, backup)
        _rebuild_views(conn, path, backup)
    finally:
        conn.close()
    return UpgradeReport(str(path), stored, SCHEMA_VERSION, True, str(backup), _NEXT_STEP)


def add_store_group(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    """Add the ``store`` command group with its ``upgrade`` subcommand."""
    group = add_parser("store", help="manage the store file")
    sub = group.add_subparsers(dest="store_command")
    upgrade = sub.add_parser(
        "upgrade", help="move the store to the current schema, keeping every row"
    )
    upgrade.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="store file to upgrade; default is HOCKEY_MODEL_DB",
    )


def run_store_group(args: argparse.Namespace, db_path: Path) -> tuple[object, int]:
    """Run a ``store`` subcommand and return its JSON payload and exit code."""
    if args.store_command != "upgrade":
        raise StoreError("store: name a subcommand, such as `hockey-model store upgrade`")
    return asdict(upgrade_store(db_path)), 0
