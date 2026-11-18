"""Tests for the cause that the CLI names for a SQLite error the store layer did not catch."""

import sqlite3
from pathlib import Path

import pytest

from hockey_model import cli
from hockey_model.store.db import sqlite_failure_fix


def _sqlite_error(name: str, text: str) -> sqlite3.OperationalError:
    """Build a SQLite error that carries the result code name, as a real one does."""
    error = sqlite3.OperationalError(text)
    error.sqlite_errorname = name
    return error


@pytest.mark.parametrize(
    ("name", "text", "expected"),
    [
        ("SQLITE_ERROR", "no such column: attempts_known", "hockey-model store upgrade"),
        ("SQLITE_ERROR", "no such table: latest_mp_team_games", "hockey-model store upgrade"),
        ("SQLITE_BUSY", "database is locked", "another process holds the store"),
        ("SQLITE_LOCKED", "database table is locked", "another process holds the store"),
        ("SQLITE_CORRUPT", "database disk image is malformed", "not a readable SQLite store"),
        ("SQLITE_FULL", "database or disk is full", "is full"),
        ("SQLITE_READONLY", "attempt to write a readonly database", "is read-only"),
    ],
)
def test_each_sqlite_cause_names_its_own_fix(name: str, text: str, expected: str) -> None:
    """A named cause gets its own sentence, and only the read-only cause names permissions."""
    message = sqlite_failure_fix(_sqlite_error(name, text))
    assert expected in message
    assert ("permissions" in message) == (name == "SQLITE_READONLY")


def test_the_fallback_names_the_error_and_the_store_without_a_guess() -> None:
    """An unknown cause must not read as a guess about the path or the permissions."""
    message = sqlite_failure_fix(_sqlite_error("SQLITE_IOERR", "disk I/O error"), None)
    assert "disk I/O error" in message
    assert "no known cause fits this error" in message


def test_a_given_path_is_named_in_the_message() -> None:
    """The operator needs the store path in the line, so it is a parameter, not a guess."""
    message = sqlite_failure_fix(_sqlite_error("SQLITE_BUSY", "locked"), Path("/tmp/s.db"))
    assert "/tmp/s.db" in message


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_sqlite_error("SQLITE_ERROR", "no such column: attempts_known"), "store upgrade"),
        (_sqlite_error("SQLITE_BUSY", "database is locked"), "another process holds the store"),
        (PermissionError("cannot open the store"), "check the file path and permissions"),
    ],
)
def test_the_cli_prints_the_cause_and_exits_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: BaseException,
    expected: str,
) -> None:
    """The CLI keeps exit 2 for each cause, and prints the sentence that cause earns."""

    def boom(_args: object) -> tuple[object, int]:
        raise error

    monkeypatch.setattr(cli, "run_store_command", boom)
    assert cli.main(["refresh"]) == 2
    assert expected in capsys.readouterr().err
