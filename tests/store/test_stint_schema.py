"""Tests for schema version 12: the stints and the event status tables (#35)."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from seeding import add_game, drop_event_tables, drop_stint_tables

from hockey_model.store.archive import OK, SHAPE_ERROR, Fetch, record_observation
from hockey_model.store.db import SCHEMA_VERSION, connect
from hockey_model.store.source_reads import NHL_PBP, NHL_SHIFTS
from hockey_model.store.upgrade import upgrade_store

GAME = 2025020709
T1 = datetime(2026, 1, 11, 6, 0, tzinfo=UTC)
T2 = datetime(2026, 1, 13, 6, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "store.db")
    yield connection
    connection.close()


def _obs(conn: sqlite3.Connection, source: str, when: datetime, status: str = OK) -> int:
    fetch = Fetch(source, "https://example.test/u", when, status, f"{source}:1", str(GAME), b"{}")
    return record_observation(conn, fetch)


def _stint(conn: sqlite3.Connection, pbp: int, shift: int, number: int = 1) -> None:
    conn.execute(
        "INSERT INTO stints (game_id, pbp_obs, shift_obs, stint, period, start_second,"
        " end_second, home_skaters, away_skaters, home_goalies, away_goalies)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (GAME, pbp, shift, number, 1, 0, 60, "[1,2]", "[3]", "[90]", "[91]"),
    )


def _status(conn: sqlite3.Connection, pbp: int, shift: int, status: str = "provisional") -> None:
    conn.execute(
        "INSERT INTO event_status (game_id, pbp_obs, shift_obs, status, checked_at, notes)"
        " VALUES (?,?,?,?,?,?)",
        (GAME, pbp, shift, status, "2026-01-12T00:00:00.000000+00:00", "[]"),
    )


def test_the_schema_is_version_twelve() -> None:
    assert SCHEMA_VERSION == 12


def test_the_upgrade_from_version_eleven_adds_empty_tables_and_keeps_every_row(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v11.db"
    store = connect(path)
    add_game(store, 7, "2026-01-05T00:00:00+00:00")
    drop_stint_tables(store)
    store.execute("UPDATE schema_version SET version = 11")
    store.commit()
    store.close()
    report = upgrade_store(path)
    assert (report.from_version, report.to_version) == (11, SCHEMA_VERSION)
    upgraded = connect(path)
    try:
        for table in ("stints", "event_status"):
            assert upgraded.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
        assert upgraded.execute("SELECT COUNT(*) FROM games").fetchone() == (1,)
    finally:
        upgraded.close()


def test_the_upgrade_from_version_ten_passes_through_both_steps(tmp_path: Path) -> None:
    path = tmp_path / "v10.db"
    store = connect(path)
    drop_event_tables(store)
    store.execute("UPDATE schema_version SET version = 10")
    store.commit()
    store.close()
    assert upgrade_store(path).to_version == SCHEMA_VERSION
    upgraded = connect(path)
    try:
        assert upgraded.execute("SELECT COUNT(*) FROM stints").fetchone() == (0,)
        assert upgraded.execute("SELECT COUNT(*) FROM pbp_events").fetchone() == (0,)
    finally:
        upgraded.close()


def test_a_stint_names_the_two_observations_that_gave_it(conn: sqlite3.Connection) -> None:
    pbp, shift = _obs(conn, NHL_PBP, T1), _obs(conn, NHL_SHIFTS, T1)
    _stint(conn, pbp, shift)
    with pytest.raises(sqlite3.IntegrityError):
        _stint(conn, pbp, shift)
    with pytest.raises(sqlite3.IntegrityError):
        _stint(conn, 999, shift, 2)


def test_a_status_other_than_the_three_is_refused(conn: sqlite3.Connection) -> None:
    pbp, shift = _obs(conn, NHL_PBP, T1), _obs(conn, NHL_SHIFTS, T1)
    _status(conn, pbp, shift, "reconciled")
    with pytest.raises(sqlite3.IntegrityError):
        _status(conn, pbp, shift + 100, "done")


def test_the_views_give_the_rows_of_the_newest_pair_of_observations(
    conn: sqlite3.Connection,
) -> None:
    old = _obs(conn, NHL_PBP, T1), _obs(conn, NHL_SHIFTS, T1)
    new = _obs(conn, NHL_PBP, T2), _obs(conn, NHL_SHIFTS, T2)
    for pair in (old, new):
        _stint(conn, *pair)
        _status(conn, *pair)
    stints = conn.execute("SELECT pbp_obs, shift_obs FROM latest_stints").fetchall()
    states = conn.execute("SELECT pbp_obs, shift_obs FROM latest_event_status").fetchall()
    assert stints == [new]
    assert states == [new]


def test_a_refused_newer_fetch_leaves_no_stints_in_the_view(conn: sqlite3.Connection) -> None:
    old = _obs(conn, NHL_PBP, T1), _obs(conn, NHL_SHIFTS, T1)
    _stint(conn, *old)
    _obs(conn, NHL_SHIFTS, T2, SHAPE_ERROR)
    assert conn.execute("SELECT COUNT(*) FROM latest_stints").fetchone() == (0,)
