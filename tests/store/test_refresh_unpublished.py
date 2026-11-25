"""The goalie file of a new season does not exist before the season starts (#70)."""

import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from refresh_support import NOW, FakeClient
from seeding import add_game

from hockey_model.sources.errors import SourceError
from hockey_model.sources.moneypuck import goalie_games_url
from hockey_model.store import refresh_other
from hockey_model.store.archive import observations_for
from hockey_model.store.db import connect
from hockey_model.store.refresh_context import RefreshRun
from hockey_model.store.source_reads import abandoned_urls

CURRENT = 2025
URL = goalie_games_url(CURRENT)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "store.db")
    yield connection
    connection.close()


def missing(url: str, status: int) -> SourceError:
    return SourceError("moneypuck", url, f"HTTP {status}", status=status)


def moneypuck_run(conn: sqlite3.Connection, url: str, failure: SourceError, days: int = 0):
    run = RefreshRun(conn, FakeClient({url: failure}), NOW + timedelta(days=days))
    refresh_other.moneypuck(run)
    return run


def failed_reads(conn: sqlite3.Connection, url: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM source_reads WHERE url = ? AND ok = 0", (url,)
    ).fetchone()
    return row[0]


def test_a_404_before_the_season_has_a_final_game_is_a_note_and_not_an_error(
    conn: sqlite3.Connection,
) -> None:
    run = moneypuck_run(conn, URL, missing(URL, 404))
    assert run.errors == []
    assert [n for n in run.notes if "not published yet" in n] == [
        f"moneypuck: the goalie file of season {CURRENT} is not published yet, and the store "
        "holds no final game of that season; the next refresh reads it again"
    ]


def test_an_unpublished_file_never_becomes_a_known_gap(conn: sqlite3.Connection) -> None:
    for days in range(4):
        moneypuck_run(conn, URL, missing(URL, 404), days)
    assert failed_reads(conn, URL) == 0
    assert URL not in abandoned_urls(conn, "moneypuck")


def test_an_expected_absence_writes_its_observation_and_no_failed_read(
    conn: sqlite3.Connection,
) -> None:
    """The request ran, so the archive says what it returned. The absence is not a gap."""
    moneypuck_run(conn, URL, missing(URL, 404))
    (found,) = [o for o in observations_for(conn, "moneypuck", None) if o.url == URL]
    assert found.status == "http_error"
    assert found.error is not None and "HTTP 404" in found.error
    assert failed_reads(conn, URL) == 0


def test_the_file_is_read_when_it_appears_after_404_runs(conn: sqlite3.Connection) -> None:
    moneypuck_run(conn, URL, missing(URL, 404))
    run = RefreshRun(conn, FakeClient(), NOW + timedelta(days=1))
    refresh_other.moneypuck(run)
    assert run.errors == []
    assert not [n for n in run.notes if "not published yet" in n]
    assert conn.execute(
        "SELECT COUNT(*) FROM source_reads WHERE url = ? AND ok = 1", (URL,)
    ).fetchone() == (1,)


def test_a_404_after_a_final_game_of_the_season_is_a_failure(conn: sqlite3.Connection) -> None:
    add_game(conn, 1, "2025-10-10T00:00", season=20252026)
    run = moneypuck_run(conn, URL, missing(URL, 404))
    assert [str(e) for e in run.errors] == [f"fetch moneypuck {URL}: HTTP 404"]
    assert failed_reads(conn, URL) == 1


def test_a_playoff_game_counts_as_a_final_game_of_the_season(conn: sqlite3.Connection) -> None:
    add_game(conn, 1, "2026-04-20T00:00", season=20252026, game_type=3)
    run = moneypuck_run(conn, URL, missing(URL, 404))
    assert len(run.errors) == 1


@pytest.mark.parametrize("status", [403, 500, 503])
def test_another_status_is_a_failure_even_with_no_game(
    conn: sqlite3.Connection, status: int
) -> None:
    run = moneypuck_run(conn, URL, missing(URL, status))
    assert len(run.errors) == 1
    assert failed_reads(conn, URL) == 1


def test_a_404_for_an_earlier_season_is_a_failure(conn: sqlite3.Connection) -> None:
    old = goalie_games_url(2020)
    run = moneypuck_run(conn, old, missing(old, 404))
    assert len(run.errors) == 1
    assert failed_reads(conn, old) == 1


def test_a_failure_with_no_status_is_a_failure(conn: sqlite3.Connection) -> None:
    failure = SourceError("moneypuck", URL, "network error timed out after 4 attempts")
    run = moneypuck_run(conn, URL, failure)
    assert len(run.errors) == 1
