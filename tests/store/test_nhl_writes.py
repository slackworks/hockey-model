"""Tests for the NHL writes. They use a store in a temporary directory."""

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hockey_model.sources.nhl import parse_boxscore, parse_score_day
from hockey_model.store.db import connect
from hockey_model.store.nhl_writes import (
    game_is_missing_boxscore,
    games_missing_boxscore,
    write_boxscore,
    write_games,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "nhl"
T0 = datetime(2026, 1, 11, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "store.db")
    yield connection
    connection.close()


def day_rows(name: str = "score_2026-01-10.json"):
    return parse_score_day(json.loads((FIXTURES / name).read_text()))


def test_write_games_keeps_both_reads(conn: sqlite3.Connection) -> None:
    final = day_rows()
    live = [r for r in final if r.game_id == 2025020697]
    payload = json.loads((FIXTURES / "score_2026-01-10.json").read_text())
    for game in payload["games"]:
        game["gameState"] = "LIVE"
    nonfinal = [r for r in parse_score_day(payload) if r.game_id == 2025020697]
    assert write_games(conn, nonfinal, T0) == 1
    assert write_games(conn, live, T0 + timedelta(hours=3)) == 1
    states = conn.execute(
        "SELECT state, home_score FROM games WHERE game_id = 2025020697 ORDER BY fetched_at"
    ).fetchall()
    assert states == [("LIVE", None), ("OFF", 10)]


def test_write_games_stores_start_as_utc_text(conn: sqlite3.Connection) -> None:
    write_games(conn, day_rows()[:1], T0)
    (start,) = conn.execute("SELECT start_utc FROM games").fetchone()
    assert start == "2026-01-10T18:00:00.000000+00:00"


def test_write_games_rejects_naive_fetch_time(conn: sqlite3.Connection) -> None:
    with pytest.raises(Exception, match="no timezone"):
        write_games(conn, day_rows()[:1], datetime(2026, 1, 11, 12, 0))


def test_games_missing_boxscore(conn: sqlite3.Connection) -> None:
    write_games(conn, day_rows() + day_rows("score_2026-11-05.json"), T0)
    missing = games_missing_boxscore(conn)
    assert 2025020709 in missing
    assert len(missing) == 14
    assert not any(game_id >= 2026000000 for game_id in missing)


def test_games_missing_boxscore_drops_stored_game(conn: sqlite3.Connection) -> None:
    write_games(conn, day_rows(), T0)
    rows = parse_boxscore(json.loads((FIXTURES / "boxscore_2025020709.json").read_text()))
    write_boxscore(conn, 2025020709, rows, T0)
    assert 2025020709 not in games_missing_boxscore(conn)
    assert len(games_missing_boxscore(conn)) == 13


def test_games_missing_boxscore_lists_game_once_across_reads(conn: sqlite3.Connection) -> None:
    write_games(conn, day_rows()[:1], T0)
    write_games(conn, day_rows()[:1], T0 + timedelta(hours=1))
    assert games_missing_boxscore(conn) == [2025020697]


def test_write_boxscore_stores_lines_and_players(conn: sqlite3.Connection) -> None:
    rows = parse_boxscore(json.loads((FIXTURES / "boxscore_2025020709.json").read_text()))
    write_boxscore(conn, 2025020709, rows, T0)
    (skaters,) = conn.execute("SELECT COUNT(*) FROM skater_games").fetchone()
    started = conn.execute("SELECT team FROM goalie_games WHERE started = 1 ORDER BY team")
    assert skaters == len(rows.skaters)
    assert [r[0] for r in started] == ["EDM", "LAK"]
    (players,) = conn.execute("SELECT COUNT(*) FROM players").fetchone()
    assert players == len(rows.players)


def test_write_boxscore_adds_player_row_only_on_change(conn: sqlite3.Connection) -> None:
    payload = json.loads((FIXTURES / "boxscore_2025020709.json").read_text())
    rows = parse_boxscore(payload)
    write_boxscore(conn, 2025020709, rows, T0)
    payload["id"] = 2025020799
    other = parse_boxscore(payload)
    write_boxscore(conn, 2025020799, other, T0 + timedelta(days=1))
    (count,) = conn.execute("SELECT COUNT(*) FROM players").fetchone()
    assert count == len(rows.players)
    moved = other.players[0]._replace(team="TOR")
    other.players[0] = moved
    write_boxscore(conn, 2025020798, other, T0 + timedelta(days=2))
    (count,) = conn.execute("SELECT COUNT(*) FROM players").fetchone()
    assert count == len(rows.players) + 1


def test_writing_the_same_boxscore_twice_adds_nothing_and_keeps_the_first_read(
    conn: sqlite3.Connection,
) -> None:
    rows = parse_boxscore(json.loads((FIXTURES / "boxscore_2025020709.json").read_text()))
    write_boxscore(conn, 2025020709, rows, T0)
    before = conn.execute("SELECT * FROM skater_games ORDER BY nhl_id").fetchall()
    write_boxscore(conn, 2025020709, rows, T0 + timedelta(days=1))
    assert conn.execute("SELECT * FROM skater_games ORDER BY nhl_id").fetchall() == before


def test_write_games_skips_a_game_that_did_not_change(conn: sqlite3.Connection) -> None:
    assert write_games(conn, day_rows(), T0) == 14
    assert write_games(conn, day_rows(), T0 + timedelta(hours=1)) == 0
    (count,) = conn.execute("SELECT COUNT(*) FROM games").fetchone()
    assert count == 14


def test_write_games_appends_a_changed_start_time(conn: sqlite3.Connection) -> None:
    write_games(conn, day_rows("score_2026-11-05.json"), T0)
    moved = [
        replace(r, start=r.start + timedelta(hours=1)) for r in day_rows("score_2026-11-05.json")
    ]
    assert write_games(conn, moved, T0 + timedelta(hours=1)) == len(moved)


def _full_rows():
    return parse_boxscore(json.loads((FIXTURES / "boxscore_2025020709.json").read_text()))


def _one_side(rows, team: str):
    """The rows of one team only, as an old partial boxscore stored them."""
    return replace(
        rows,
        players=[p for p in rows.players if p.team == team],
        skaters=[s for s in rows.skaters if s.team == team],
        goalies=[g for g in rows.goalies if g.team == team],
    )


def _stored_game(conn: sqlite3.Connection, rows) -> int:
    """Store a final game whose teams are the boxscore's, and return its id."""
    game = replace(
        next(r for r in day_rows() if r.game_id == 2025020697),
        game_id=rows.game_id,
        home=rows.home,
        away=rows.away,
    )
    write_games(conn, [game], T0)
    return rows.game_id


def test_a_game_with_only_one_side_stored_is_queued_again(conn: sqlite3.Connection) -> None:
    rows = _full_rows()
    game_id = _stored_game(conn, rows)
    write_boxscore(conn, game_id, _one_side(rows, rows.home), T0)
    assert game_id in games_missing_boxscore(conn)


def test_a_game_with_goalies_but_no_skaters_on_a_side_is_queued_again(
    conn: sqlite3.Connection,
) -> None:
    rows = _full_rows()
    game_id = _stored_game(conn, rows)
    no_away_skaters = replace(rows, skaters=[s for s in rows.skaters if s.team == rows.home])
    write_boxscore(conn, game_id, no_away_skaters, T0)
    assert game_id in games_missing_boxscore(conn)


def test_a_game_with_both_sides_stored_is_not_queued(conn: sqlite3.Connection) -> None:
    rows = _full_rows()
    game_id = _stored_game(conn, rows)
    write_boxscore(conn, game_id, rows, T0)
    assert game_id not in games_missing_boxscore(conn)


def test_repairing_a_partial_game_keeps_the_original_rows(conn: sqlite3.Connection) -> None:
    rows = _full_rows()
    game_id = _stored_game(conn, rows)
    write_boxscore(conn, game_id, _one_side(rows, rows.home), T0)
    later = T0 + timedelta(days=2)
    write_boxscore(conn, game_id, rows, later)
    stamps = dict(
        conn.execute(
            "SELECT team, MIN(fetched_at) FROM skater_games WHERE game_id = ? GROUP BY team",
            (game_id,),
        ).fetchall()
    )
    assert stamps[rows.home].startswith("2026-01-11")
    assert stamps[rows.away].startswith("2026-01-13")
    assert game_id not in games_missing_boxscore(conn)
    (home_rows,) = conn.execute(
        "SELECT COUNT(*) FROM skater_games WHERE game_id = ? AND team = ?", (game_id, rows.home)
    ).fetchone()
    assert home_rows == len(_one_side(rows, rows.home).skaters)


def test_games_missing_boxscore_can_be_limited_to_one_game(conn: sqlite3.Connection) -> None:
    write_games(conn, day_rows(), T0)
    assert games_missing_boxscore(conn, game_id=2025020709) == [2025020709]
    assert games_missing_boxscore(conn, game_id=1) == []
    assert game_is_missing_boxscore(conn, 2025020709)
    rows = parse_boxscore(json.loads((FIXTURES / "boxscore_2025020709.json").read_text()))
    write_boxscore(conn, 2025020709, rows, T0)
    assert not game_is_missing_boxscore(conn, 2025020709)
    assert game_is_missing_boxscore(conn, 2025020697)
