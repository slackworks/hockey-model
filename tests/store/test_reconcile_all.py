"""Tests for ``store reconcile``: the stints and the status of every stored game (#35)."""

import argparse
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from seeding import add_game

from hockey_model.sources.nhl_events import parse_pbp
from hockey_model.sources.nhl_shifts import parse_shifts
from hockey_model.store.archive import OK, SHAPE_ERROR, Fetch, record_observation
from hockey_model.store.db import StoreError, connect
from hockey_model.store.event_writes import write_pbp, write_shifts
from hockey_model.store.reconcile_game import reconcile_games
from hockey_model.store.source_reads import NHL_PBP, NHL_SHIFTS
from hockey_model.store_cli import run_store_command

FIXTURES = Path(__file__).parent.parent / "fixtures" / "nhl"
GAME = 2025020709
START = datetime(2026, 1, 10, 1, 0, tzinfo=UTC)
NOW = START + timedelta(days=15)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "store.db")
    add_game(connection, GAME, START.isoformat(), home="EDM", away="LAK", score=(3, 4))
    yield connection
    connection.close()


def _load(name: str) -> object:
    return json.loads((FIXTURES / f"{name}_2025020709.json").read_text())


def _obs(
    conn: sqlite3.Connection,
    source: str,
    game_id: int,
    status: str = OK,
    when: datetime = NOW,
) -> int:
    fetch = Fetch(source, "u", when, status, f"{source}:1", str(game_id), b"{}")
    return record_observation(conn, fetch)


def _store(conn: sqlite3.Connection, game_id: int = GAME) -> None:
    write_pbp(conn, _obs(conn, NHL_PBP, game_id), parse_pbp(_load("pbp"), GAME))
    write_shifts(conn, _obs(conn, NHL_SHIFTS, game_id), parse_shifts(_load("shifts"), GAME))


def test_every_game_with_event_rows_gets_stints_and_a_status(conn: sqlite3.Connection) -> None:
    _store(conn)
    report = reconcile_games(conn, NOW)
    assert (report.games, report.skipped, report.failed) == (1, (), ())
    assert report.reconciled + report.incomplete == 1
    assert conn.execute("SELECT COUNT(*) FROM event_status").fetchone() == (1,)
    assert conn.execute("SELECT COUNT(*) FROM stints").fetchone()[0] > 100


def test_a_game_with_a_play_by_play_and_no_shifts_is_skipped_and_named(
    conn: sqlite3.Connection,
) -> None:
    write_pbp(conn, _obs(conn, NHL_PBP, GAME), parse_pbp(_load("pbp"), GAME))
    report = reconcile_games(conn, NOW)
    assert (report.games, report.skipped) == (1, (f"game {GAME}: no shift chart observation",))
    assert conn.execute("SELECT COUNT(*) FROM event_status").fetchone() == (0,)


def test_one_game_can_be_named(conn: sqlite3.Connection) -> None:
    _store(conn)
    assert reconcile_games(conn, NOW, [GAME]).games == 1
    skipped = reconcile_games(conn, NOW, [GAME + 1]).skipped
    assert skipped == (f"game {GAME + 1}: no play-by-play observation",)


def test_a_game_that_fails_is_named_and_the_others_still_run(conn: sqlite3.Connection) -> None:
    _store(conn)
    other = GAME + 100
    conn.execute("UPDATE observations SET key = ?", (str(other),))
    _store(conn)
    report = reconcile_games(conn, NOW)
    assert report.games == 2
    assert len(report.failed) == 1
    assert f"game {other}: no row in games" in report.failed[0]
    assert report.reconciled + report.incomplete == 1


def _args(db: Path, game: int | None = None) -> argparse.Namespace:
    return argparse.Namespace(command="store", store_command="reconcile", db=str(db), game=game)


def test_the_command_prints_the_report_and_exits_zero(tmp_path: Path) -> None:
    path = tmp_path / "cli.db"
    store = connect(path)
    add_game(store, GAME, START.isoformat(), home="EDM", away="LAK", score=(3, 4))
    _store(store)
    store.commit()
    store.close()
    payload, code = run_store_command(_args(path))
    assert code == 0
    assert isinstance(payload, dict)
    assert payload["games"] == 1
    assert list(payload["failed"]) == []


def test_the_command_exits_one_when_a_game_fails(tmp_path: Path) -> None:
    path = tmp_path / "cli.db"
    store = connect(path)
    _store(store)
    store.commit()
    store.close()
    payload, code = run_store_command(_args(path))
    assert code == 1
    assert isinstance(payload, dict)
    assert len(payload["failed"]) == 1


def test_the_command_names_a_missing_store(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="no store file"):
        run_store_command(_args(tmp_path / "none.db"))


def test_a_newest_shift_chart_that_has_no_rows_is_named_as_the_reason(
    conn: sqlite3.Connection,
) -> None:
    _store(conn)
    _obs(conn, NHL_SHIFTS, GAME, status=SHAPE_ERROR, when=NOW + timedelta(hours=1))
    report = reconcile_games(conn, NOW)
    assert report.skipped == (f"game {GAME}: the newest shift chart has no rows",)


def test_a_newest_play_by_play_that_has_no_rows_is_named_as_the_reason(
    conn: sqlite3.Connection,
) -> None:
    _store(conn)
    _obs(conn, NHL_PBP, GAME, status=SHAPE_ERROR, when=NOW + timedelta(hours=1))
    report = reconcile_games(conn, NOW)
    assert report.skipped == (f"game {GAME}: the newest play-by-play has no rows",)


def test_an_event_observation_whose_key_is_not_a_game_id_is_a_failure(
    conn: sqlite3.Connection,
) -> None:
    record_observation(conn, Fetch(NHL_PBP, "u", NOW, OK, "nhl_pbp:1", "abc", b"{}"))
    report = reconcile_games(conn, NOW)
    assert report.games == 0
    assert len(report.failed) == 1
    assert "key 'abc' is not a game id" in report.failed[0]


def test_a_named_game_that_is_skipped_exits_one(tmp_path: Path) -> None:
    path = tmp_path / "cli.db"
    store = connect(path)
    store.close()
    payload, code = run_store_command(_args(path, GAME))
    assert code == 1
    assert isinstance(payload, dict)
    assert len(payload["skipped"]) == 1
