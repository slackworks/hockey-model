"""The 6->7 step drops starter_reads.nhl_id and injury_reads.nhl_id, and keeps every value (#73)."""

import sqlite3
from pathlib import Path

import pytest
from test_upgrade import make_v1_store, rows, version_of

from hockey_model.store import upgrade
from hockey_model.store.db import SCHEMA_VERSION, StoreError, connect
from hockey_model.store.shape import Requirement, earned_version
from hockey_model.store.upgrade import upgrade_store

STARTERS = [
    (101, "TOR", "espn", "Joseph Woll", "likely", "2026-01-19T12:00:00Z"),
    (101, "TOR", "daily_faceoff", "Joseph Woll", "confirmed", "2026-01-19T14:00:00Z"),
    (102, "BOS", "espn", "Jeremy Swayman", "confirmed", "2026-01-19T15:00:00Z"),
]
INJURIES = [
    ("TOR", "Auston Matthews", "out", "2026-02-01", "2026-01-19T12:00:00Z"),
    ("BOS", "Brad Marchand", "day-to-day", None, "2026-01-19T12:00:00Z"),
]
_STARTER_COLUMNS = "game_id, team, source, goalie_name, status, fetched_at"
_INJURY_COLUMNS = "team, player_name, status, return_date, fetched_at"


def _v6(path: Path, starter_ids: list[int | None], injury_ids: list[int | None]) -> Path:
    """Build a store that holds version 6: the current shape with the two retired columns."""
    connect(path).close()
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE starter_reads ADD COLUMN nhl_id INTEGER")
    conn.execute("ALTER TABLE injury_reads ADD COLUMN nhl_id INTEGER")
    for row, nhl_id in zip(STARTERS, starter_ids, strict=True):
        conn.execute(
            f"INSERT INTO starter_reads ({_STARTER_COLUMNS}, nhl_id) VALUES (?,?,?,?,?,?,?)",
            (*row, nhl_id),
        )
    for row, nhl_id in zip(INJURIES, injury_ids, strict=True):
        conn.execute(
            f"INSERT INTO injury_reads ({_INJURY_COLUMNS}, nhl_id) VALUES (?,?,?,?,?,?)",
            (*row, nhl_id),
        )
    conn.execute("UPDATE schema_version SET version = 6")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def all_null(tmp_path: Path) -> Path:
    return _v6(tmp_path / "null.db", [None] * 3, [None] * 2)


@pytest.fixture
def populated(tmp_path: Path) -> Path:
    return _v6(tmp_path / "populated.db", [8479361, None, 8480280], [8479318, 8473468])


def _columns(path: Path, table: str) -> list[str]:
    return [row[0] for row in rows(path, f"SELECT name FROM pragma_table_info('{table}')")]


def test_a_store_with_the_columns_earns_version_6(all_null: Path) -> None:
    conn = sqlite3.connect(all_null)
    try:
        earned, missing = earned_version(conn)
    finally:
        conn.close()
    assert (earned, missing) == (6, Requirement(7, "starter_reads", "nhl_id", False, False))


def test_a_fresh_store_holds_neither_column(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    connect(path).close()
    assert "nhl_id" not in _columns(path, "starter_reads")
    assert "nhl_id" not in _columns(path, "injury_reads")
    conn = sqlite3.connect(path)
    try:
        assert earned_version(conn) == (SCHEMA_VERSION, None)
    finally:
        conn.close()


def test_the_step_drops_both_columns_and_keeps_every_other_value(all_null: Path) -> None:
    report = upgrade_store(all_null)
    assert (report.from_version, report.to_version, report.changed) == (6, SCHEMA_VERSION, True)
    assert version_of(all_null) == SCHEMA_VERSION == 7
    assert "nhl_id" not in _columns(all_null, "starter_reads")
    assert "nhl_id" not in _columns(all_null, "injury_reads")
    starters = rows(all_null, f"SELECT {_STARTER_COLUMNS} FROM starter_reads ORDER BY rowid")
    injuries = rows(all_null, f"SELECT {_INJURY_COLUMNS} FROM injury_reads ORDER BY rowid")
    assert starters == STARTERS
    assert injuries == INJURIES


def test_a_store_with_only_null_ids_gets_an_empty_archive(all_null: Path) -> None:
    upgrade_store(all_null)
    assert rows(all_null, "SELECT COUNT(*) FROM retired_nhl_ids") == [(0,)]


def test_a_populated_id_is_archived_with_its_observation_and_time(populated: Path) -> None:
    upgrade_store(populated)
    archive = rows(
        populated,
        "SELECT source_table, observation, fetched_at, nhl_id FROM retired_nhl_ids"
        " ORDER BY source_table, nhl_id",
    )
    assert archive == [
        (
            "injury_reads",
            '["BOS","Brad Marchand","day-to-day",null]',
            "2026-01-19T12:00:00Z",
            8473468,
        ),
        (
            "injury_reads",
            '["TOR","Auston Matthews","out","2026-02-01"]',
            "2026-01-19T12:00:00Z",
            8479318,
        ),
        (
            "starter_reads",
            '[101,"TOR","espn","Joseph Woll","likely"]',
            "2026-01-19T12:00:00Z",
            8479361,
        ),
        (
            "starter_reads",
            '[102,"BOS","espn","Jeremy Swayman","confirmed"]',
            "2026-01-19T15:00:00Z",
            8480280,
        ),
    ]
    assert rows(populated, "SELECT COUNT(*) FROM starter_reads") == [(3,)]
    assert rows(populated, "SELECT COUNT(*) FROM injury_reads") == [(2,)]


def test_a_second_upgrade_changes_nothing(populated: Path) -> None:
    upgrade_store(populated)
    before = rows(populated, "SELECT * FROM retired_nhl_ids ORDER BY rowid")
    report = upgrade_store(populated)
    assert report.changed is False
    assert rows(populated, "SELECT * FROM retired_nhl_ids ORDER BY rowid") == before


def test_a_failed_step_rolls_everything_back(
    populated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = (*upgrade.MIGRATIONS[6][:-1], "INSERT INTO no_such_table VALUES (1)")
    monkeypatch.setitem(upgrade.MIGRATIONS, 6, broken)
    with pytest.raises(StoreError, match="the store is unchanged"):
        upgrade_store(populated)
    assert version_of(populated) == 6
    assert "nhl_id" in _columns(populated, "starter_reads")
    assert "nhl_id" in _columns(populated, "injury_reads")
    tables = rows(populated, "SELECT name FROM sqlite_master WHERE type = 'table'")
    assert ("retired_nhl_ids",) not in tables
    stored = rows(populated, "SELECT nhl_id FROM starter_reads ORDER BY rowid")
    assert stored == [(8479361,), (None,), (8480280,)]


def test_a_stamp_ahead_of_the_columns_runs_the_step_again(populated: Path) -> None:
    conn = sqlite3.connect(populated)
    conn.execute("UPDATE schema_version SET version = 7")
    conn.commit()
    conn.close()
    report = upgrade_store(populated)
    assert report.changed is True
    assert report.note is not None
    assert "starter_reads without nhl_id" in report.note
    assert rows(populated, "SELECT COUNT(*) FROM retired_nhl_ids") == [(4,)]
    assert "nhl_id" not in _columns(populated, "starter_reads")


def test_a_version_1_store_reaches_the_new_shape(tmp_path: Path) -> None:
    path = make_v1_store(tmp_path / "v1.db")
    upgrade_store(path)
    assert version_of(path) == SCHEMA_VERSION
    assert "nhl_id" not in _columns(path, "starter_reads")
    assert "nhl_id" not in _columns(path, "injury_reads")
