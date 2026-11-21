"""Tests for the MoneyPuck writes."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hockey_model.sources.moneypuck import GoalieGameRow, TeamGameRow
from hockey_model.store.db import connect
from hockey_model.store.moneypuck_writes import (
    write_goalie_games,
    write_team_games,
)
from hockey_model.store.source_reads import last_ok_read, record_read

T0 = datetime(2026, 1, 11, 12, 0, tzinfo=UTC)
URL = "https://moneypuck.com/x.csv"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "store.db")
    yield connection
    connection.close()


def team_row(xg_for: float = 2.5, attempts_for: int = 50) -> TeamGameRow:
    return TeamGameRow(2025020697, "BOS", "2026-01-10", xg_for, 1.5, attempts_for, 40)


def test_write_team_games_skips_unchanged_rows(conn: sqlite3.Connection) -> None:
    assert write_team_games(conn, [team_row()], T0) == 1
    assert write_team_games(conn, [team_row()], T0 + timedelta(days=7)) == 0
    (count,) = conn.execute("SELECT COUNT(*) FROM mp_team_games").fetchone()
    assert count == 1


def test_write_team_games_keeps_both_reads_of_a_changed_row(conn: sqlite3.Connection) -> None:
    write_team_games(conn, [team_row(2.5)], T0)
    assert write_team_games(conn, [team_row(3.5)], T0 + timedelta(days=7)) == 1
    goals = conn.execute("SELECT xg_for FROM mp_team_games ORDER BY fetched_at").fetchall()
    assert goals == [(2.5,), (3.5,)]
    assert write_team_games(conn, [team_row(3.5)], T0 + timedelta(days=14)) == 0


def test_write_team_games_compares_with_newest_read(conn: sqlite3.Connection) -> None:
    write_team_games(conn, [team_row(2.5)], T0)
    write_team_games(conn, [team_row(3.5)], T0 + timedelta(days=7))
    assert write_team_games(conn, [team_row(2.5)], T0 + timedelta(days=14)) == 1


def test_write_goalie_games_skips_unchanged_rows(conn: sqlite3.Connection) -> None:
    row = GoalieGameRow(2025020709, 8478971, "EDM", 2.1, 3)
    assert write_goalie_games(conn, [row], T0) == 1
    assert write_goalie_games(conn, [row], T0 + timedelta(days=7)) == 0
    changed = GoalieGameRow(2025020709, 8478971, "EDM", 2.2, 3)
    assert write_goalie_games(conn, [changed], T0 + timedelta(days=14)) == 1


def test_last_ok_read_is_none_without_reads(conn: sqlite3.Connection) -> None:
    assert last_ok_read(conn, "moneypuck", URL) is None


def test_last_ok_read_ignores_failed_reads(conn: sqlite3.Connection) -> None:
    record_read(conn, "moneypuck", URL, T0)
    record_read(conn, "moneypuck", URL, T0 + timedelta(days=1), error="HTTP 503")
    other = "https://moneypuck.com/y.csv"
    record_read(conn, "moneypuck", other, T0 + timedelta(days=2))
    assert last_ok_read(conn, "moneypuck", URL) == T0


def test_write_team_games_writes_a_changed_attempt_count(conn: sqlite3.Connection) -> None:
    write_team_games(conn, [team_row(attempts_for=50)], T0)
    assert write_team_games(conn, [team_row(attempts_for=51)], T0 + timedelta(days=7)) == 1
    counts = conn.execute(
        "SELECT shot_attempts_for FROM latest_mp_team_games ORDER BY fetched_at"
    ).fetchall()
    assert counts == [(51,)]
