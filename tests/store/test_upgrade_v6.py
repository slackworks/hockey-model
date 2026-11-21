"""The 5->6 step adds source_reads.permanent and keeps every row (#279)."""

import sqlite3
from pathlib import Path

import pytest
from test_upgrade import make_v1_store, rows, version_of

from hockey_model.store.db import SCHEMA_VERSION, connect
from hockey_model.store.shape import Requirement, earned_version
from hockey_model.store.upgrade import upgrade_store

READS = [
    ("nhl_scores", "https://example.test/a", "2026-01-01T00:00:00Z", 0, "HTTP 403"),
    ("nhl_scores", "https://example.test/a", "2026-01-02T00:00:00Z", 1, None),
    ("espn_injuries", "https://example.test/b", "2026-01-03T00:00:00Z", 0, "HTTP 503"),
]
_COLUMNS = "source, url, fetched_at, ok, error"


@pytest.fixture
def v5(tmp_path: Path) -> Path:
    """Return a store that holds version 5: the current shape without the new column."""
    path = tmp_path / "v5.db"
    connect(path).close()
    conn = sqlite3.connect(path)
    conn.executemany(
        f"INSERT INTO source_reads ({_COLUMNS}) VALUES (?, ?, ?, ?, ?)",
        READS,
    )
    conn.execute("ALTER TABLE source_reads DROP COLUMN permanent")
    conn.execute("UPDATE schema_version SET version = 5")
    conn.commit()
    conn.close()
    return path


def _columns(path: Path) -> list[str]:
    return [row[0] for row in rows(path, "SELECT name FROM pragma_table_info('source_reads')")]


def test_a_store_without_the_column_earns_version_5(v5: Path) -> None:
    conn = sqlite3.connect(v5)
    try:
        earned, missing = earned_version(conn)
    finally:
        conn.close()
    assert (earned, missing) == (5, Requirement(6, "source_reads", "permanent", False))


def test_the_step_adds_the_column_and_keeps_every_row(v5: Path) -> None:
    report = upgrade_store(v5)
    assert (report.from_version, report.to_version, report.changed) == (5, SCHEMA_VERSION, True)
    assert version_of(v5) == SCHEMA_VERSION == 6
    assert _columns(v5)[-1] == "permanent"
    assert rows(v5, f"SELECT {_COLUMNS} FROM source_reads ORDER BY rowid") == READS
    # An old row has no stored cause, so the reader counts it as transient.
    assert rows(v5, "SELECT permanent FROM source_reads") == [(None,)] * len(READS)


def test_a_second_upgrade_changes_nothing(v5: Path) -> None:
    upgrade_store(v5)
    before = rows(v5, "SELECT * FROM source_reads ORDER BY rowid")
    report = upgrade_store(v5)
    assert report.changed is False
    assert rows(v5, "SELECT * FROM source_reads ORDER BY rowid") == before


def test_a_version_1_store_reaches_the_new_column(tmp_path: Path) -> None:
    path = make_v1_store(tmp_path / "v1.db")
    upgrade_store(path)
    assert "permanent" in _columns(path)
    assert version_of(path) == SCHEMA_VERSION
