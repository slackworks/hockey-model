"""Tests for the revisit policy: which final boxscores a refresh reads again (#82)."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from refresh_support import FakeClient
from seeding import add_game

from hockey_model.sources.nhl import boxscore_url
from hockey_model.store import refresh_revisit
from hockey_model.store.archive import OK, Fetch, record_observation
from hockey_model.store.boxscore_revisit import boxscores_due
from hockey_model.store.db import StoreError, connect
from hockey_model.store.refresh import refresh
from hockey_model.store.source_reads import BOXSCORES

START = datetime(2026, 1, 10, 18, 0, tzinfo=UTC)
AFTER_DAYS = (2, 14)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "store.db")
    yield connection
    connection.close()


def _read(conn: sqlite3.Connection, game_id: int, when: datetime, status: str = OK) -> None:
    record_observation(
        conn, Fetch(BOXSCORES, "u", when, status, "nhl_boxscore:1", key=str(game_id))
    )


def _game(conn: sqlite3.Connection, game_id: int = 1, state: str = "OFF") -> None:
    add_game(conn, game_id, START.isoformat(), state=state)


@pytest.mark.parametrize(
    ("read_at", "now", "due"),
    [
        (START + timedelta(hours=3), START + timedelta(days=1), False),
        (START + timedelta(hours=3), START + timedelta(days=2) - timedelta(seconds=1), False),
        (START + timedelta(hours=3), START + timedelta(days=2), True),
        (START + timedelta(days=2, hours=1), START + timedelta(days=13), False),
        (START + timedelta(days=2, hours=1), START + timedelta(days=14), True),
        (START + timedelta(days=14, hours=1), START + timedelta(days=400), False),
        (START + timedelta(days=90), START + timedelta(days=400), False),
    ],
)
def test_a_game_is_due_after_each_revisit_day_that_no_read_has_met(
    conn: sqlite3.Connection, read_at: datetime, now: datetime, due: bool
) -> None:
    _game(conn)
    _read(conn, 1, read_at)
    assert (boxscores_due(conn, now, AFTER_DAYS) == [1]) is due


def test_a_game_with_no_good_read_is_not_a_revisit(conn: sqlite3.Connection) -> None:
    _game(conn)
    _read(conn, 1, START + timedelta(hours=3), status="http_error")
    assert boxscores_due(conn, START + timedelta(days=30), AFTER_DAYS) == []


def test_a_failed_revisit_leaves_the_game_due(conn: sqlite3.Connection) -> None:
    _game(conn)
    _read(conn, 1, START + timedelta(hours=3))
    _read(conn, 1, START + timedelta(days=3), status="http_error")
    assert boxscores_due(conn, START + timedelta(days=4), AFTER_DAYS) == [1]


def test_a_game_that_is_not_final_is_not_due(conn: sqlite3.Connection) -> None:
    _game(conn, state="LIVE")
    _read(conn, 1, START + timedelta(hours=3))
    assert boxscores_due(conn, START + timedelta(days=30), AFTER_DAYS) == []


def test_the_oldest_due_time_comes_first(conn: sqlite3.Connection) -> None:
    add_game(conn, 1, (START + timedelta(days=5)).isoformat())
    add_game(conn, 2, START.isoformat())
    for game_id, start in ((1, START + timedelta(days=5)), (2, START)):
        _read(conn, game_id, start + timedelta(hours=3))
    assert boxscores_due(conn, START + timedelta(days=30), AFTER_DAYS) == [2, 1]


def test_a_refresh_reads_at_most_the_bound_and_names_the_rest(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(refresh_revisit, "MAX_REVISITS", 1)
    now = datetime(2026, 1, 14, 9, 0, tzinfo=UTC)
    refresh(conn, FakeClient(), now=datetime(2026, 1, 10, 21, 0, tzinfo=UTC))
    games = [row[0] for row in conn.execute("SELECT game_id FROM latest_final_games")]
    assert len(games) > 1
    client = FakeClient()
    report = refresh(conn, client, now=now)
    boxscores = [u for u in client.urls if "/boxscore" in u]
    assert len(boxscores) == 1
    assert any("revisit(s) wait for the next refresh" in note for note in report.notes)


def test_a_known_gap_does_not_use_a_slot_of_the_bound(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(refresh_revisit, "MAX_REVISITS", 1)
    refresh(conn, FakeClient(), now=datetime(2026, 1, 10, 21, 0, tzinfo=UTC))
    now = datetime(2026, 1, 14, 9, 0, tzinfo=UTC)
    first, second, *_ = boxscores_due(conn, now, AFTER_DAYS)
    for stamp in ("2026-01-11T00:00:00.000000+00:00", "2026-01-12T00:00:00.000000+00:00"):
        conn.execute(
            "INSERT INTO source_reads (source, url, fetched_at, ok, error, permanent)"
            " VALUES (?, ?, ?, 0, 'HTTP 503', 0)",
            (BOXSCORES, boxscore_url(first), stamp),
        )
    client = FakeClient()
    refresh(conn, client, now=now)
    assert [u for u in client.urls if "/boxscore" in u] == [boxscore_url(second)]


def test_a_stored_time_that_is_not_a_time_names_the_game(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO games (game_id, state, start_utc, fetched_at) VALUES (7, 'OFF', 'soon', 'x')"
    )
    _read(conn, 7, START)
    with pytest.raises(StoreError, match="game 7"):
        boxscores_due(conn, START + timedelta(days=30), AFTER_DAYS)
