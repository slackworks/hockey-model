"""Tests for the derive hook of ``RefreshRun.attempt``: rows that a good unit derives (#82)."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from refresh_support import FakeClient

from hockey_model.store.db import StoreError, connect
from hockey_model.store.recording import Reading
from hockey_model.store.refresh_context import DeriveError, RefreshRun
from hockey_model.store.source_reads import BOXSCORES

NOW = datetime(2026, 1, 10, 10, 0, tzinfo=UTC)
URL = "https://api-web.nhle.com/v1/gamecenter/2025020697/boxscore"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "store.db")
    yield connection
    connection.close()


def _unit(run: RefreshRun, requests: int):
    def unit(stamp: datetime) -> int:
        for _ in range(requests):
            run.client.get("nhl", URL)
        return 1

    return unit


def _counts(conn: sqlite3.Connection) -> tuple[int, int]:
    reads = conn.execute("SELECT COUNT(rowid) FROM source_reads").fetchone()[0]
    observations = conn.execute("SELECT COUNT(rowid) FROM observations").fetchone()[0]
    return reads, observations


def test_the_hook_gets_the_observation_that_holds_the_bytes(conn: sqlite3.Connection) -> None:
    run = RefreshRun(conn, FakeClient(), NOW)
    seen: list[int] = []
    reading = Reading("nhl_boxscore:1", derive=lambda _conn, obs_id: seen.append(obs_id))
    assert run.attempt(BOXSCORES, URL, _unit(run, 1), reading)
    assert conn.execute("SELECT obs_id FROM observations").fetchall() == [(seen[0],)]


def test_a_store_error_in_the_hook_keeps_the_bytes_and_records_no_failed_read(
    conn: sqlite3.Connection,
) -> None:
    run = RefreshRun(conn, FakeClient(), NOW)

    def derive(_conn: sqlite3.Connection, obs_id: int) -> None:
        raise StoreError("the revision table refused the row")

    with pytest.raises(DeriveError, match="refused the row"):
        run.attempt(BOXSCORES, URL, _unit(run, 1), Reading("nhl_boxscore:1", derive=derive))
    # The unit rolled back and no failed read exists, so no backoff starts. The observation and
    # its bytes stay, so the next read can differ and this one can be parsed again.
    assert _counts(conn) == (0, 1)
    assert conn.execute("SELECT sha256 IS NOT NULL FROM observations").fetchone() == (1,)
    assert conn.execute("SELECT COUNT(sha256) FROM payloads").fetchone() == (1,)
    assert not run.errors


@pytest.mark.parametrize("requests", [0, 2])
def test_a_unit_with_a_hook_must_make_exactly_one_request(
    conn: sqlite3.Connection, requests: int
) -> None:
    run = RefreshRun(conn, FakeClient(), NOW)
    reading = Reading("nhl_boxscore:1", derive=lambda _conn, obs_id: None)
    with pytest.raises(DeriveError, match="exactly one"):
        run.attempt(BOXSCORES, URL, _unit(run, requests), reading)
    assert _counts(conn) == (0, requests)
