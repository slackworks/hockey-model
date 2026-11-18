"""Open the SQLite store and guard its schema version."""

import sqlite3
from collections.abc import Mapping
from pathlib import Path

from hockey_model.types import HockeyModelError

DB_ENV = "HOCKEY_MODEL_DB"
SCHEMA_VERSION = 5
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_VIEWS_PATH = Path(__file__).with_name("views.sql")


class StoreError(HockeyModelError):
    """Raised when the database path is missing or the schema does not match."""


def db_path_from_env(environ: Mapping[str, str]) -> Path:
    """Return the database path named by ``HOCKEY_MODEL_DB``."""
    value = environ.get(DB_ENV, "").strip()
    if not value:
        raise StoreError(
            f"read {DB_ENV}: the variable is not set or is empty; "
            f"set {DB_ENV} to the path of the SQLite file"
        )
    return Path(value).expanduser()


def connect(path: Path) -> sqlite3.Connection:
    """Open the store at ``path``, creating the file and schema on first use."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
    except (OSError, sqlite3.Error) as exc:
        raise StoreError(
            f"open store {str(path)!r}: {exc}; check that the directory exists and is writable"
        ) from exc
    try:
        return _prepare(conn, path)
    except sqlite3.Error as exc:
        conn.close()
        raise StoreError(_open_failure_fix(path, exc)) from exc
    except StoreError, OSError:
        conn.close()
        raise


def _open_failure_fix(path: Path, exc: sqlite3.Error) -> str:
    """Name the open failure and the fix. A lock and a damaged file need different fixes."""
    text = str(exc)
    name = getattr(exc, "sqlite_errorname", "")
    if name == "SQLITE_NOTADB":
        fix = "the file is not a SQLite store, so use a new path"
    elif name in {"SQLITE_BUSY", "SQLITE_LOCKED"}:
        fix = "another process holds the store; stop it, then open the store again"
    else:
        fix = "check the disk and the file permissions, then open the store again"
    return f"open store {str(path)!r}: {text}; {fix}"


def _prepare(conn: sqlite3.Connection, path: Path) -> sqlite3.Connection:
    conn.execute("PRAGMA foreign_keys = ON")
    mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if mode != "wal":
        raise StoreError(
            f"open store {str(path)!r}: journal mode is {mode!r}, not 'wal'; "
            "use a filesystem that supports WAL"
        )
    if not _has_table(conn, "schema_version"):
        _create_schema(conn)
    stored = _stored_version(conn, path)
    if stored != SCHEMA_VERSION:
        raise StoreError(
            f"open store {str(path)!r}: schema version {stored} does not match "
            f"expected {SCHEMA_VERSION}. {_mismatch_fix(path, stored)}"
        )
    # The views are code, not data, so every connection makes them again.
    conn.executescript(_VIEWS_PATH.read_text(encoding="utf-8"))
    return conn


def _mismatch_fix(path: Path, stored: int) -> str:
    """Return the fix for a store whose version is not the one this code reads."""
    if stored < SCHEMA_VERSION:
        return (
            f"Upgrade it with `hockey-model store upgrade --db {str(path)!r}`. The upgrade "
            "keeps every row and writes a backup first."
        )
    return "This store is newer than this hockey-model. Use a hockey-model that reads it."


def _stored_version(conn: sqlite3.Connection, path: Path) -> int:
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        raise StoreError(
            f"open store {str(path)!r}: schema_version has no row, so the store is "
            "half-initialized; delete the file or use a new path"
        )
    return int(row[0])


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _create_schema(conn: sqlite3.Connection) -> None:
    # One script, so the tables and the version row commit together or not at all.
    # A failure leaves the transaction open, and closing the connection rolls it back.
    script = (
        "BEGIN;\n"
        + _SCHEMA_PATH.read_text(encoding="utf-8")
        + f"\nINSERT INTO schema_version (version) VALUES ({SCHEMA_VERSION});\nCOMMIT;\n"
    )
    conn.executescript(script)
