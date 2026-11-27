"""Refresh reads the play-by-play and the shift chart of each final game (#35)."""

import json
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from refresh_support import NOW, FakeClient, fixture
from seeding import add_game

from hockey_model.sources.errors import SourceError
from hockey_model.sources.nhl import boxscore_url
from hockey_model.sources.nhl_events import pbp_url
from hockey_model.sources.nhl_shifts import shifts_url
from hockey_model.store.db import connect
from hockey_model.store.refresh import refresh
from hockey_model.store.refresh_context import RefreshRun
from hockey_model.store.refresh_events import read_events
from hockey_model.store.refresh_nhl import read_boxscores
from hockey_model.store.source_reads import NHL_PBP, NHL_SHIFTS

SCORED = 14
GAME = 2025020709
BOX = boxscore_url(GAME)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "store.db")
    yield connection
    connection.close()


def _games(conn: sqlite3.Connection, table: str) -> set[int]:
    return {row[0] for row in conn.execute(f"SELECT DISTINCT game_id FROM {table}")}


def _observations(conn: sqlite3.Connection, source: str, game_id: int = GAME) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM observations WHERE source = ? AND key = ?", (source, str(game_id))
    ).fetchone()[0]


def _status(conn: sqlite3.Connection, game_id: int = GAME) -> list[tuple]:
    return conn.execute(
        "SELECT status, checked_at FROM event_status WHERE game_id = ?", (game_id,)
    ).fetchall()


def test_a_refresh_stores_the_events_and_shifts_of_every_final_game(
    conn: sqlite3.Connection,
) -> None:
    report = refresh(conn, FakeClient(), now=NOW)
    assert report.errors == ()
    assert len(_games(conn, "pbp_events")) == SCORED
    assert _games(conn, "shifts") == _games(conn, "pbp_events")


def test_a_refresh_reconciles_each_game_it_read_events_for(conn: sqlite3.Connection) -> None:
    refresh(conn, FakeClient(), now=NOW)
    assert _games(conn, "event_status") == _games(conn, "pbp_events")
    assert {row[0] for row in conn.execute("SELECT status FROM event_status")} <= {
        "provisional",
        "incomplete",
    }


def test_a_game_with_events_is_not_read_again_in_the_same_window(conn: sqlite3.Connection) -> None:
    refresh(conn, FakeClient(), now=NOW)
    client = FakeClient()
    refresh(conn, client, now=NOW + timedelta(hours=1))
    assert pbp_url(GAME) not in client.urls
    assert shifts_url(GAME) not in client.urls


def test_a_game_is_read_again_at_the_revisit_days(conn: sqlite3.Connection) -> None:
    refresh(conn, FakeClient(), now=NOW)
    refresh(conn, FakeClient(), now=NOW + timedelta(days=3))
    assert _observations(conn, NHL_PBP) == 2
    assert _observations(conn, NHL_SHIFTS) == 2
    assert len(_status(conn)) == 1


def test_a_failed_play_by_play_stores_nothing_for_its_game(conn: sqlite3.Connection) -> None:
    failing: dict[str, bytes | Exception] = {
        pbp_url(GAME): SourceError("nhl_pbp", pbp_url(GAME), "HTTP 503")
    }
    report = refresh(conn, FakeClient(failing), now=NOW)
    assert len(report.errors) == 1
    assert GAME not in _games(conn, "pbp_events")
    assert GAME in _games(conn, "shifts")
    assert _status(conn) == []
    assert len(_games(conn, "pbp_events")) == SCORED - 1


def test_a_payload_for_another_game_is_a_failed_unit(conn: sqlite3.Connection) -> None:
    other = json.loads(fixture("nhl/pbp_2025020709.json"))
    other["id"] = GAME + 1
    report = refresh(conn, FakeClient({pbp_url(GAME): json.dumps(other).encode()}), now=NOW)
    assert len(report.errors) == 1
    assert GAME not in _games(conn, "pbp_events")


def _reconciled_game(conn: sqlite3.Connection) -> None:
    """Store the boxscore and the events of one game, read long after its last revisit day."""
    add_game(conn, GAME, "2026-01-10T01:00:00.000000+00:00", home="EDM", away="LAK", score=(3, 4))
    conn.execute("UPDATE games SET last_period = 'SO'")
    served: dict[str, bytes | Exception] = {
        BOX: fixture("nhl/boxscore_2025020709.json"),
        pbp_url(GAME): fixture("nhl/pbp_2025020709.json"),
        shifts_url(GAME): fixture("nhl/shifts_2025020709.json"),
    }
    run = RefreshRun(conn, FakeClient(served), NOW + timedelta(days=20))
    read_boxscores(run, [GAME])
    read_events(run, NHL_PBP, [GAME])
    read_events(run, NHL_SHIFTS, [GAME])
    assert run.errors == []


def test_a_new_boxscore_revision_changes_the_stored_status(conn: sqlite3.Connection) -> None:
    _reconciled_game(conn)
    assert [row[0] for row in _status(conn)] == ["reconciled"]
    body = json.loads(fixture("nhl/boxscore_2025020709.json"))
    for side in body["playerByGameStats"].values():
        for group in side.values():
            for entry in group:
                if entry.get("goals"):
                    entry["goals"] += 1
    later = RefreshRun(conn, FakeClient({BOX: json.dumps(body).encode()}), NOW + timedelta(days=21))
    read_boxscores(later, [GAME])
    assert later.errors == []
    assert [row[0] for row in _status(conn)] == ["incomplete"]


def test_a_new_boxscore_revision_that_cannot_reconcile_removes_the_status(
    conn: sqlite3.Connection,
) -> None:
    _reconciled_game(conn)
    conn.execute("UPDATE observations SET fetched_at = 'garbage' WHERE source = ?", (NHL_PBP,))
    later = RefreshRun(
        conn, FakeClient({BOX: fixture("nhl/boxscore_2025020709.json")}), NOW + timedelta(days=21)
    )
    read_boxscores(later, [GAME])
    assert len(later.errors) == 1
    assert "garbage" in str(later.errors[0])
    assert _status(conn) == []
