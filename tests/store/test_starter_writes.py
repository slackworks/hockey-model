"""Tests for the starter and injury writes."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from hockey_model.game import StarterSource, StarterStatus
from hockey_model.sources.espn import InjuryRead
from hockey_model.sources.nhl import GameRow
from hockey_model.sources.starters import StarterRead
from hockey_model.store.db import StoreError, connect
from hockey_model.store.espn_writes import write_injury_reads
from hockey_model.store.nhl_writes import write_games
from hockey_model.store.starter_writes import START_TOLERANCE, write_starter_reads

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
START = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "store.db")
    yield connection
    connection.close()


def game(game_id: int, start: datetime, home: str, away: str) -> GameRow:
    return GameRow(game_id, 20262027, 2, start, home, away, "FUT", None, None, None)


def read(team: str, name: str, start: datetime = START, source=StarterSource.ESPN) -> StarterRead:
    return StarterRead(start, team, name, StarterStatus.LIKELY, source)


def test_write_starter_reads_matches_home_and_away(conn: sqlite3.Connection) -> None:
    write_games(conn, [game(2026020001, START, "TOR", "UTA")], T0)
    result = write_starter_reads(conn, [read("TOR", "A"), read("UTA", "B")], T0)
    assert (result.written, result.unmatched) == (2, ())
    rows = conn.execute(
        "SELECT game_id, team, source, goalie_name, status FROM starter_reads ORDER BY team"
    ).fetchall()
    assert rows == [
        (2026020001, "TOR", "espn", "A", "likely"),
        (2026020001, "UTA", "espn", "B", "likely"),
    ]


def test_write_starter_reads_keeps_every_read(conn: sqlite3.Connection) -> None:
    write_games(conn, [game(2026020001, START, "TOR", "UTA")], T0)
    write_starter_reads(conn, [read("TOR", "A")], T0)
    write_starter_reads(conn, [read("TOR", "B")], T0 + timedelta(hours=3))
    names = conn.execute("SELECT goalie_name FROM starter_reads ORDER BY fetched_at").fetchall()
    assert names == [("A",), ("B",)]


def test_write_starter_reads_reports_unmatched_read(conn: sqlite3.Connection) -> None:
    write_games(conn, [game(2026020001, START, "TOR", "UTA")], T0)
    result = write_starter_reads(conn, [read("TOR", "A"), read("BOS", "C")], T0)
    assert result.written == 1
    assert len(result.unmatched) == 1
    assert result.unmatched[0].startswith("BOS ")


def test_write_starter_reads_tolerates_small_start_shift(conn: sqlite3.Connection) -> None:
    write_games(conn, [game(2026020001, START, "TOR", "UTA")], T0)
    shifted = read("TOR", "A", START + timedelta(minutes=30))
    assert write_starter_reads(conn, [shifted], T0).written == 1


def test_write_starter_reads_rejects_a_far_start(conn: sqlite3.Connection) -> None:
    write_games(conn, [game(2026020001, START, "TOR", "UTA")], T0)
    far = read("TOR", "A", START + START_TOLERANCE + timedelta(minutes=1))
    assert write_starter_reads(conn, [far], T0).unmatched


def test_write_starter_reads_uses_newest_schedule_read(conn: sqlite3.Connection) -> None:
    write_games(conn, [game(2026020001, START, "TOR", "UTA")], T0)
    moved = START + timedelta(days=2)
    write_games(conn, [game(2026020001, moved, "TOR", "UTA")], T0 + timedelta(hours=1))
    assert write_starter_reads(conn, [read("TOR", "A", START)], T0).unmatched
    assert write_starter_reads(conn, [read("TOR", "A", moved)], T0).written == 1


def test_write_starter_reads_refuses_to_guess_between_games(conn: sqlite3.Connection) -> None:
    write_games(
        conn,
        [
            game(2026020001, START, "TOR", "UTA"),
            game(2026020002, START + timedelta(hours=2), "TOR", "BOS"),
        ],
        T0,
    )
    with pytest.raises(StoreError, match="2 stored games"):
        write_starter_reads(conn, [read("TOR", "A")], T0)


def test_write_injury_reads_stores_each_player(conn: sqlite3.Connection) -> None:
    reads = [
        InjuryRead("ANA", "A.J. Greer", "Injured Reserve", date(2026, 11, 11)),
        InjuryRead("BOS", "Some Player", "Day-To-Day", None),
    ]
    assert write_injury_reads(conn, reads, T0) == 2
    rows = conn.execute(
        "SELECT team, player_name, status, return_date FROM injury_reads ORDER BY team"
    ).fetchall()
    assert rows == [
        ("ANA", "A.J. Greer", "Injured Reserve", "2026-11-11"),
        ("BOS", "Some Player", "Day-To-Day", None),
    ]


def test_write_injury_reads_keeps_earlier_reads(conn: sqlite3.Connection) -> None:
    reads = [InjuryRead("ANA", "A.J. Greer", "Out", None)]
    write_injury_reads(conn, reads, T0)
    write_injury_reads(conn, reads, T0 + timedelta(days=1))
    (count,) = conn.execute("SELECT COUNT(*) FROM injury_reads").fetchone()
    assert count == 2


def test_write_starter_reads_matches_a_renamed_franchise(conn: sqlite3.Connection) -> None:
    write_games(conn, [game(2026020001, START, "ARI", "BOS")], T0)
    assert write_starter_reads(conn, [read("UTA", "A")], T0).written == 1
