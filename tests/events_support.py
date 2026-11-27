"""Shared helpers for the tests of the event refresh (#35)."""

import sqlite3
from datetime import timedelta

from refresh_support import NOW, FakeClient, fixture
from seeding import add_game

from hockey_model.sources.nhl import boxscore_url
from hockey_model.sources.nhl_events import pbp_url
from hockey_model.sources.nhl_shifts import shifts_url
from hockey_model.store.refresh_context import RefreshRun
from hockey_model.store.refresh_events import read_events
from hockey_model.store.refresh_nhl import read_boxscores
from hockey_model.store.source_reads import NHL_PBP, NHL_SHIFTS

GAME = 2025020709
BOX = boxscore_url(GAME)
START = "2026-01-10T01:00:00.000000+00:00"


def status_rows(conn: sqlite3.Connection, game_id: int = GAME) -> list[tuple]:
    """Return the status and check time of a game."""
    return conn.execute(
        "SELECT status, checked_at FROM event_status WHERE game_id = ?", (game_id,)
    ).fetchall()


def reconciled_game(conn: sqlite3.Connection) -> None:
    """Store the boxscore and the events of one game, read long after its last revisit day."""
    add_game(conn, GAME, START, home="EDM", away="LAK", score=(3, 4))
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
