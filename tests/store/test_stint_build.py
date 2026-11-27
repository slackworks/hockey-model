"""Tests for the stint builder over stored rows (#35)."""

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from seeding import add_game

from hockey_model.sources.nhl_events import parse_pbp
from hockey_model.sources.nhl_shifts import ShiftRow, ShiftRows, parse_shifts
from hockey_model.store.archive import HTTP_ERROR, OK, Fetch, record_observation
from hockey_model.store.db import StoreError, connect
from hockey_model.store.event_writes import write_pbp, write_shifts
from hockey_model.store.source_reads import NHL_PBP, NHL_SHIFTS
from hockey_model.store.stint_build import (
    GameStints,
    _ids,
    build_game_stints,
    game_inputs,
    write_stints,
)

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


def _build(conn: sqlite3.Connection) -> GameStints | None:
    built = build_game_stints(conn, GAME)
    if built is not None:
        write_stints(conn, built)
    return built


def _rows(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute("SELECT * FROM stints ORDER BY pbp_obs, shift_obs, stint").fetchall()


def test_a_saved_game_gives_stints_with_the_right_sides(conn: sqlite3.Connection) -> None:
    pbp, shift = _store(conn)
    built = _build(conn)
    assert built is not None
    assert (built.inputs.pbp_obs, built.inputs.shift_obs) == (pbp, shift)
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
    _build(conn)
    rows = _rows(conn)
    one_each = [r for r in rows if len(json.loads(r[9])) == 1 and len(json.loads(r[10])) == 1]
    assert len(one_each) > len(rows) * 0.8


def test_the_goalies_are_the_roster_players_with_the_position_g(conn: sqlite3.Connection) -> None:
    pbp, _ = _store(conn)
    _build(conn)
    goalies = {p for (p,) in conn.execute("SELECT player_id FROM pbp_roster WHERE position = 'G'")}
    in_net = {p for r in _rows(conn) for col in (9, 10) for p in json.loads(r[col])}
    skating = {p for r in _rows(conn) for col in (7, 8) for p in json.loads(r[col])}
    assert in_net <= goalies
    assert not skating & goalies
    assert pbp


def test_a_second_build_gives_the_same_rows(conn: sqlite3.Connection) -> None:
    _store(conn)
    _build(conn)
    first = _rows(conn)
    _build(conn)
    assert _rows(conn) == first


def test_a_newer_pair_of_observations_adds_its_own_stints(conn: sqlite3.Connection) -> None:
    old = _store(conn, T1)
    _build(conn)
    new = _store(conn, T2)
    _build(conn)
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
    built = _build(conn)
    assert built is not None
    assert (built.inputs.pbp_obs, built.inputs.shift_obs) == pair


def test_a_game_that_the_store_does_not_hold_is_a_named_error(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM games")
    _store(conn)
    with pytest.raises(StoreError, match=rf"game {GAME}: no row in games"):
        _build(conn)


def test_a_shift_for_a_team_of_neither_side_is_a_named_error(conn: sqlite3.Connection) -> None:
    pbp, shift = _obs(conn, NHL_PBP), _obs(conn, NHL_SHIFTS)
    write_pbp(conn, pbp, _pbp())
    stray = ShiftRow(5, "CGY", 1, 0, 30, 1)
    write_shifts(conn, shift, ShiftRows(GAME, (stray,), 1))
    with pytest.raises(StoreError, match=r"game 2025020709: .*neither the home team EDM"):
        _build(conn)


@given(st.lists(st.integers(0, 2**63 - 1), max_size=12).map(sorted).map(tuple))
def test_the_player_ids_of_a_stint_read_back_from_their_text(players: tuple[int, ...]) -> None:
    text = _ids(players)
    assert json.loads(text) == list(players)
    assert " " not in text
