"""Open the SQLite store and guard its schema version."""

import errno
import sqlite3
import urllib.parse
from collections.abc import Mapping
from pathlib import Path

from hockey_model.store.shape import earned_version
from hockey_model.types import HockeyModelError

DB_ENV = "HOCKEY_MODEL_DB"
SCHEMA_VERSION = 8
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
    except OSError as exc:
        raise StoreError(
            f"open store {str(path)!r}: cannot create the directory {str(path.parent)!r}: "
            f"{exc}; check that the path exists and is writable"
        ) from exc
    try:
        conn = sqlite3.connect(path)
    except sqlite3.Error as exc:
        raise StoreError(_open_failure_fix(path, exc)) from exc
    try:
        return _prepare(conn, path)
    except sqlite3.Error as exc:
        conn.close()
        raise StoreError(_open_failure_fix(path, exc)) from exc
    except StoreError, OSError:
        conn.close()
        raise


def read_only_uri(path: Path) -> str:
    """Return the SQLite URI that opens ``path`` for reading only and never creates the file."""
    return f"file:{urllib.parse.quote(str(path))}?mode=ro"


def _open_failure_fix(path: Path, exc: sqlite3.Error) -> str:
    """Name the open failure and the fix. A lock and a damaged file need different fixes."""
    name = getattr(exc, "sqlite_errorname", "")
    if name == "SQLITE_NOTADB":
        fix = "the file is not a SQLite store, so use a new path"
    elif name in {"SQLITE_BUSY", "SQLITE_LOCKED"}:
        fix = "another process holds the store; stop it, then open the store again"
    elif name in {"SQLITE_CORRUPT", "SQLITE_FULL", "SQLITE_IOERR"}:
        # A damaged file and a full disk both read as a failed read or write, and neither
        # becomes correct by changing a permission.
        fix = "the store or its disk has a fault, so restore the store from a backup"
    else:
        fix = "check the disk and the file permissions, then open the store again"
    return f"open store {str(path)!r}: {exc}; {fix}"


def sqlite_failure_fix(exc: sqlite3.Error) -> str:
    """Name the cause of a SQLite error that escaped the store layer, and the fix.

    The store layer raises ``StoreError`` for every cause it can name, and that message holds
    the path. This function covers the error that gets past it, so it names no path. The
    message never guesses: when no code fits, it states the error alone, with no claim about
    the path or the permissions.
    """
    name = getattr(exc, "sqlite_errorname", "")
    if name == "SQLITE_CORRUPT" or name == "SQLITE_NOTADB":
        return f"the file is not a readable SQLite store ({exc}); use a new path"
    if _is_schema_error(name, exc):
        return (
            f"the store does not match the code's schema ({exc}); upgrade it with "
            "`hockey-model store upgrade`. The upgrade keeps every row and writes a backup first."
        )
    if name in {"SQLITE_BUSY", "SQLITE_LOCKED"}:
        return f"another process holds the store ({exc}); stop it, then run the command again"
    if name == "SQLITE_FULL":
        return f"the disk that holds the store is full ({exc}); free space, then run it again"
    if name == "SQLITE_READONLY":
        return (
            f"the store is read-only ({exc}); change the permissions of the file and its "
            "directory, then run the command again"
        )
    return f"{exc}; no known cause fits this error. Report it with this line."


def os_error_fix(exc: OSError) -> str:
    """Name the cause of an ``OSError`` that escaped the store layer, and the fix.

    A permission sentence fits only an access failure. Every other errno names its own cause,
    so a full disk and a missing directory do not send the reader after a permission bit.
    """
    if exc.errno == errno.ENOSPC:
        return f"the disk that holds the file is full ({exc}); free space, then run it again"
    if exc.errno in {errno.EACCES, errno.EPERM}:
        return (
            f"the file or its directory refuses access ({exc}); change the permissions of "
            "the file and its directory, then run the command again"
        )
    if exc.errno == errno.ENOENT:
        return f"no file at that path ({exc}); check the path, then run the command again"
    return f"{type(exc).__name__}: {exc}; no known cause fits this error. Report it with this line."


def _is_schema_error(name: str, exc: sqlite3.Error) -> bool:
    """Return True when the error says a column or a table that the code names is absent.

    SQLite raises a plain ``SQLITE_ERROR`` for these, so the message text is the only signal
    that separates them from any other statement failure.
    """
    if name != "SQLITE_ERROR":
        return False
    text = str(exc)
    return "no such column" in text or "no such table" in text


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
    _refuse_a_short_shape(conn, path)
    # The views are code, not data, so every connection makes them again.
    conn.executescript(_VIEWS_PATH.read_text(encoding="utf-8"))
    return conn


def _refuse_a_short_shape(conn: sqlite3.Connection, path: Path) -> None:
    """Refuse a store whose stamp names a shape that its tables do not hold.

    A stamp is written by the code and the shape by the tables, so a failed upgrade can leave
    the two apart. The stamp alone would open such a store, and the first read that names the
    missing column would fail with a message that names no fix.
    """
    earned, missing = earned_version(conn)
    if missing is not None and earned < SCHEMA_VERSION:
        raise StoreError(
            f"open store {str(path)!r}: the stamp says version {SCHEMA_VERSION}, but "
            f"{missing.table} lacks {missing.column}, so the tables hold version {earned}. "
            f"Upgrade it with `hockey-model store upgrade --db {str(path)!r}`, which runs "
            "every step again and writes a backup first."
        )


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
