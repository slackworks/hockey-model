"""Tests for the stint builder over stored rows (#35)."""

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from seeding import add_game

from hockey_model.sources.nhl_events import parse_pbp
from hockey_model.sources.nhl_shifts import ShiftRow, ShiftRows, parse_shifts
from hockey_model.store.archive import HTTP_ERROR, OK, Fetch, record_observation
from hockey_model.store.db import StoreError, connect
from hockey_model.store.event_writes import write_pbp, write_shifts
from hockey_model.store.source_reads import NHL_PBP, NHL_SHIFTS
from hockey_model.store.stint_build import build_game_stints, game_inputs

FIXTURES = Path(__file__).parent.parent / "fixtures" / "nhl"
GAME = 2025020709
T1 = datetime(2026, 1, 11, 6, 0, tzinfo=UTC)
T2 = datetime(2026, 1, 13, 6, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "store.db")
    add_game(connection, GAME, "2026-01-10T01:00:00+00:00", home="EDM", away="LAK")
    yield connection
    connection.close()


def _obs(conn: sqlite3.Connection, source: str, when: datetime = T1, status: str = OK) -> int:
    fetch = Fetch(source, "https://example.test/u", when, status, f"{source}:1", str(GAME))
    return record_observation(conn, fetch)


def _pbp():
    return parse_pbp(json.loads((FIXTURES / "pbp_2025020709.json").read_text()), GAME)


def _shifts():
    return parse_shifts(json.loads((FIXTURES / "shifts_2025020709.json").read_text()), GAME)


def _store(conn: sqlite3.Connection, when: datetime = T1) -> tuple[int, int]:
    pbp, shift = _obs(conn, NHL_PBP, when), _obs(conn, NHL_SHIFTS, when)
    write_pbp(conn, pbp, _pbp())
    write_shifts(conn, shift, _shifts())
    return pbp, shift


def _rows(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute("SELECT * FROM stints ORDER BY pbp_obs, shift_obs, stint").fetchall()


def test_a_saved_game_gives_stints_with_the_right_sides(conn: sqlite3.Connection) -> None:
    pbp, shift = _store(conn)
    built = build_game_stints(conn, GAME)
    assert built is not None
    assert (built.pbp_obs, built.shift_obs) == (pbp, shift)
    rows = _rows(conn)
    assert len(rows) == len(built.stints.stints) > 100
    assert {r[3] for r in rows} == set(range(1, len(rows) + 1))
    for row in rows:
        home, away = json.loads(row[7]), json.loads(row[8])
        assert len(home) <= 6
        assert len(away) <= 6
        assert row[5] < row[6]
        assert home == sorted(home)


def test_a_stint_has_one_goalie_a_side_in_most_of_the_game(conn: sqlite3.Connection) -> None:
    _store(conn)
    build_game_stints(conn, GAME)
    rows = _rows(conn)
    one_each = [r for r in rows if len(json.loads(r[9])) == 1 and len(json.loads(r[10])) == 1]
    assert len(one_each) > len(rows) * 0.8


def test_the_goalies_are_the_roster_players_with_the_position_g(conn: sqlite3.Connection) -> None:
    pbp, _ = _store(conn)
    build_game_stints(conn, GAME)
    goalies = {p for (p,) in conn.execute("SELECT player_id FROM pbp_roster WHERE position = 'G'")}
    in_net = {p for r in _rows(conn) for col in (9, 10) for p in json.loads(r[col])}
    skating = {p for r in _rows(conn) for col in (7, 8) for p in json.loads(r[col])}
    assert in_net <= goalies
    assert not skating & goalies
    assert pbp


def test_a_second_build_gives_the_same_rows(conn: sqlite3.Connection) -> None:
    _store(conn)
    build_game_stints(conn, GAME)
    first = _rows(conn)
    build_game_stints(conn, GAME)
    assert _rows(conn) == first


def test_a_newer_pair_of_observations_adds_its_own_stints(conn: sqlite3.Connection) -> None:
    old = _store(conn, T1)
    build_game_stints(conn, GAME)
    new = _store(conn, T2)
    build_game_stints(conn, GAME)
    pairs = {(r[1], r[2]) for r in _rows(conn)}
    assert pairs == {old, new}
    assert {(r[1], r[2]) for r in conn.execute("SELECT * FROM latest_stints")} == {new}


def test_a_game_with_no_shift_rows_gives_no_stints(conn: sqlite3.Connection) -> None:
    pbp = _obs(conn, NHL_PBP)
    write_pbp(conn, pbp, _pbp())
    assert game_inputs(conn, GAME) is None
    assert build_game_stints(conn, GAME) is None
    assert _rows(conn) == []


def test_a_failed_newer_fetch_does_not_hide_the_stored_rows(conn: sqlite3.Connection) -> None:
    pair = _store(conn)
    _obs(conn, NHL_SHIFTS, T2, HTTP_ERROR)
    built = build_game_stints(conn, GAME)
    assert built is not None
    assert (built.pbp_obs, built.shift_obs) == pair


def test_a_game_that_the_store_does_not_hold_is_a_named_error(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM games")
    _store(conn)
    with pytest.raises(StoreError, match=rf"game {GAME}: no row in games"):
        build_game_stints(conn, GAME)


def test_a_shift_for_a_team_of_neither_side_is_a_named_error(conn: sqlite3.Connection) -> None:
    pbp, shift = _obs(conn, NHL_PBP), _obs(conn, NHL_SHIFTS)
    write_pbp(conn, pbp, _pbp())
    stray = ShiftRow(5, "CGY", 1, 0, 30, 1)
    write_shifts(conn, shift, ShiftRows(GAME, (stray,), 1))
    with pytest.raises(StoreError, match=r"game 2025020709: .*neither the home team EDM"):
        build_game_stints(conn, GAME)
