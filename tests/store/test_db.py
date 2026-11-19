"""Tests for the SQLite connection, schema creation, and version guard."""

import re
import sqlite3
from pathlib import Path

import pytest

import hockey_model.store.db as db_module
from hockey_model.sources.nhl import FINAL_STATES
from hockey_model.store.db import DB_ENV, SCHEMA_VERSION, StoreError, connect, db_path_from_env

EXPECTED_TABLES = {
    "schema_version",
    "source_reads",
    "games",
    "players",
    "skater_games",
    "goalie_games",
    "mp_team_games",
    "mp_goalie_games",
    "starter_reads",
    "injury_reads",
    "model_fits",
    "fit_labels",
}


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


@pytest.mark.parametrize("environ", [{}, {DB_ENV: ""}, {DB_ENV: "   "}])
def test_db_path_from_env_unset_or_empty(environ: dict[str, str]) -> None:
    with pytest.raises(StoreError, match=DB_ENV) as info:
        db_path_from_env(environ)
    assert "set" in str(info.value)


def test_db_path_from_env_returns_path() -> None:
    assert db_path_from_env({DB_ENV: "/data/hm.sqlite"}) == Path("/data/hm.sqlite")


def test_connect_creates_schema(tmp_path: Path) -> None:
    conn = connect(tmp_path / "nested" / "hm.sqlite")
    try:
        assert EXPECTED_TABLES <= _tables(conn)
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == SCHEMA_VERSION == 5
    finally:
        conn.close()


def test_connect_enables_foreign_keys_and_wal(tmp_path: Path) -> None:
    conn = connect(tmp_path / "hm.sqlite")
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_connect_reopens_existing(tmp_path: Path) -> None:
    path = tmp_path / "hm.sqlite"
    first = connect(path)
    first.execute(
        "INSERT INTO source_reads (source, url, fetched_at, ok, error) "
        "VALUES ('nhl', 'https://example.test', '2026-11-03T00:00:00+00:00', 1, NULL)"
    )
    first.commit()
    first.close()

    second = connect(path)
    try:
        count = second.execute("SELECT COUNT(*) FROM source_reads").fetchone()[0]
        assert count == 1
    finally:
        second.close()


def test_connect_rejects_other_version(tmp_path: Path) -> None:
    path = tmp_path / "hm.sqlite"
    setup = connect(path)
    setup.execute("UPDATE schema_version SET version = 99")
    setup.commit()
    setup.close()

    with pytest.raises(StoreError, match="99"):
        connect(path)


def test_connect_rejects_a_stamp_ahead_of_the_table_shape(tmp_path: Path) -> None:
    """A stamp is written by the code and the shape by the tables, so connect probes both."""
    path = tmp_path / "hm.sqlite"
    setup = connect(path)
    setup.execute("DROP TABLE model_fits")
    setup.execute(
        "CREATE TABLE model_fits (fit_id INTEGER PRIMARY KEY, fit_through_season INTEGER,"
        " features TEXT, weights TEXT, created_at TEXT)"
    )
    setup.commit()
    setup.close()

    with pytest.raises(StoreError, match=r"model_fits lacks created_at") as info:
        connect(path)
    assert "store upgrade" in str(info.value)


def test_db_path_from_env_strips_padding_and_expands_home() -> None:
    assert db_path_from_env({DB_ENV: "  ~/hm.sqlite "}) == Path.home() / "hm.sqlite"


def test_connect_rejects_schema_table_without_version_row(tmp_path: Path) -> None:
    path = tmp_path / "hm.sqlite"
    setup = sqlite3.connect(path)
    setup.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    setup.commit()
    setup.close()

    with pytest.raises(StoreError, match="no row") as info:
        connect(path)
    assert "half-initialized" in str(info.value)


def test_connect_rejects_file_that_is_not_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "hm.sqlite"
    path.write_bytes(b"this is not a database" * 100)

    with pytest.raises(StoreError, match="hm.sqlite") as info:
        connect(path)
    assert "use a new path" in str(info.value)


VIEWS = [
    "latest_games",
    "latest_final_games",
    "latest_mp_team_games",
    "latest_mp_goalie_games",
    "latest_players",
]


def _views(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'view' ORDER BY name")
    return sorted(row[0] for row in rows)


def test_connect_creates_the_latest_views(tmp_path: Path) -> None:
    conn = connect(tmp_path / "store.db")
    assert _views(conn) == sorted(VIEWS)
    conn.close()


def test_connect_makes_the_views_again_in_an_old_store(tmp_path: Path) -> None:
    path = tmp_path / "store.db"
    first = connect(path)
    for name in VIEWS:
        first.execute(f"DROP VIEW {name}")
    first.commit()
    first.close()
    second = connect(path)
    assert _views(second) == sorted(VIEWS)
    second.close()


def test_the_final_game_view_uses_the_final_states() -> None:
    text = (Path(db_module.__file__).with_name("views.sql")).read_text()
    block = text[text.index("CREATE VIEW latest_final_games") :]
    states = set(re.findall(r"'([A-Z]+)'", block[: block.index(";")]))
    assert states == set(FINAL_STATES)


def test_a_view_returns_the_newest_read_of_each_key(tmp_path: Path) -> None:
    conn = connect(tmp_path / "store.db")
    insert = (
        "INSERT INTO games (game_id, season, game_type, start_utc, home, away, state,"
        " home_score, away_score, last_period, fetched_at) VALUES (1, 1, 2, 's', 'TOR', 'BOS',"
        " ?, ?, ?, ?, ?)"
    )
    conn.execute(insert, ("LIVE", None, None, None, "2026-01-01T00:00:00.000000+00:00"))
    conn.execute(insert, ("OFF", 3, 2, "REG", "2026-01-01T05:00:00.000000+00:00"))
    assert conn.execute("SELECT state FROM latest_games").fetchall() == [("OFF",)]
    assert conn.execute("SELECT state FROM latest_final_games").fetchall() == [("OFF",)]
    conn.execute(insert, ("PPD", None, None, None, "2026-01-02T00:00:00.000000+00:00"))
    assert conn.execute("SELECT state FROM latest_games").fetchall() == [("PPD",)]
    assert conn.execute("SELECT state FROM latest_final_games").fetchall() == [("OFF",)]
    conn.close()
