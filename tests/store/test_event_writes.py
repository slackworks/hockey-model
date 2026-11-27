"""Tests for the event tables: derived rows name their observation, and a write replaces them."""

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from seeding import add_game, drop_event_tables

from hockey_model.sources.espn_summary import parse_summary
from hockey_model.sources.nhl_events import parse_pbp
from hockey_model.sources.nhl_shifts import ShiftRow, ShiftRows, parse_shifts
from hockey_model.store.archive import OK, Fetch, is_market_source, record_observation
from hockey_model.store.db import SCHEMA_VERSION, connect
from hockey_model.store.event_writes import write_espn, write_pbp, write_shifts
from hockey_model.store.recording import key_of
from hockey_model.store.source_reads import (
    ESPN_SUMMARY,
    EVENT_SOURCES,
    KNOWN_SOURCES,
    NHL_PBP,
    NHL_SHIFTS,
)
from hockey_model.store.upgrade import upgrade_store

FIXTURES = Path(__file__).parent.parent / "fixtures"
GAME = 2025020709
T1 = datetime(2026, 1, 11, 6, 0, tzinfo=UTC)
T2 = datetime(2026, 1, 13, 6, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "store.db")
    yield connection
    connection.close()


def _obs(conn: sqlite3.Connection, source: str, when: datetime = T1) -> int:
    return record_observation(
        conn, Fetch(source, "https://example.test/u", when, OK, f"{source}:1", key=str(GAME))
    )


def _pbp():
    return parse_pbp(json.loads((FIXTURES / "nhl" / "pbp_2025020709.json").read_text()))


def _shifts():
    return parse_shifts(json.loads((FIXTURES / "nhl" / "shifts_2025020709.json").read_text()), GAME)


def _espn():
    return parse_summary(
        json.loads((FIXTURES / "espn" / "summary_401803060.json").read_text()), "401803060"
    )


def _count(conn: sqlite3.Connection, table: str, obs_id: int) -> int:
    query = f"SELECT COUNT(obs_id) FROM {table} WHERE obs_id = ?"
    return int(conn.execute(query, (obs_id,)).fetchone()[0])


def test_the_event_sources_are_archived_and_stay_out_of_the_known_sources() -> None:
    assert EVENT_SOURCES == {NHL_PBP, NHL_SHIFTS, ESPN_SUMMARY}
    assert EVENT_SOURCES.isdisjoint(KNOWN_SOURCES)
    assert not is_market_source(NHL_PBP)
    assert not is_market_source(NHL_SHIFTS)
    assert is_market_source(ESPN_SUMMARY)


def test_each_parser_uses_the_source_name_of_the_store() -> None:
    from hockey_model.sources import espn_summary, nhl_events, nhl_shifts

    assert (nhl_events.SOURCE, nhl_shifts.SOURCE, espn_summary.SOURCE) == (
        NHL_PBP,
        NHL_SHIFTS,
        ESPN_SUMMARY,
    )


def test_the_key_of_a_play_by_play_and_a_shift_chart_is_the_game() -> None:
    from hockey_model.sources.nhl_events import pbp_url
    from hockey_model.sources.nhl_shifts import shifts_url

    assert key_of(NHL_PBP, pbp_url(GAME)) == str(GAME)
    assert key_of(NHL_SHIFTS, shifts_url(GAME)) == str(GAME)
    assert key_of(NHL_PBP, "https://api-web.nhle.com/v1/gamecenter/12/play-by-play") is None
    assert key_of(ESPN_SUMMARY, "https://x/summary?event=1") is None


def test_the_events_and_the_roster_are_stored_with_their_observation(
    conn: sqlite3.Connection,
) -> None:
    obs = _obs(conn, NHL_PBP)
    write_pbp(conn, obs, _pbp())
    assert _count(conn, "pbp_events", obs) == 325
    assert _count(conn, "pbp_roster", obs) == 40
    goal = conn.execute(
        "SELECT game_id, owner_team, player1, player2, player3, home_score, away_score, parser"
        " FROM pbp_events WHERE obs_id = ? AND type = 'goal' ORDER BY sort_order LIMIT 1",
        (obs,),
    ).fetchone()
    assert goal == (GAME, "EDM", 8477934, 8477953, None, 1, 0, "nhl_pbp:1")


def test_a_second_write_for_one_observation_replaces_its_rows(conn: sqlite3.Connection) -> None:
    obs = _obs(conn, NHL_PBP)
    rows = _pbp()
    write_pbp(conn, obs, rows)
    write_pbp(conn, obs, replace(rows, events=rows.events[:10]))
    assert _count(conn, "pbp_events", obs) == 10
    write_pbp(conn, obs, rows)
    assert _count(conn, "pbp_events", obs) == 325


def test_two_observations_keep_their_own_rows(conn: sqlite3.Connection) -> None:
    first, second = _obs(conn, NHL_PBP, T1), _obs(conn, NHL_PBP, T2)
    write_pbp(conn, first, _pbp())
    write_pbp(conn, second, _pbp())
    assert (_count(conn, "pbp_events", first), _count(conn, "pbp_events", second)) == (325, 325)


def test_shifts_keep_the_order_of_the_feed_and_a_repeated_row(conn: sqlite3.Connection) -> None:
    shift = ShiftRow(8470621, "LAK", 1, 0, 35, 1)
    obs = _obs(conn, NHL_SHIFTS)
    write_shifts(conn, obs, ShiftRows(GAME, (shift, shift), 2))
    rows = conn.execute(
        "SELECT row_no, player_id, start_second, end_second FROM shifts WHERE obs_id = ?"
        " ORDER BY row_no",
        (obs,),
    ).fetchall()
    assert rows == [(0, 8470621, 0, 35), (1, 8470621, 0, 35)]


def test_the_saved_chart_is_stored_in_full(conn: sqlite3.Connection) -> None:
    obs = _obs(conn, NHL_SHIFTS)
    write_shifts(conn, obs, _shifts())
    assert _count(conn, "shifts", obs) == 740


def test_the_saved_summary_is_stored_in_full(conn: sqlite3.Connection) -> None:
    obs = _obs(conn, ESPN_SUMMARY)
    write_espn(conn, obs, GAME, _espn())
    assert _count(conn, "espn_skater_toi", obs) == 36


def test_a_view_gives_the_rows_of_the_newest_observation_of_each_game(
    conn: sqlite3.Connection,
) -> None:
    first, second = _obs(conn, NHL_PBP, T1), _obs(conn, NHL_PBP, T2)
    rows = _pbp()
    write_pbp(conn, first, rows)
    write_pbp(conn, second, replace(rows, events=rows.events[:5]))
    assert conn.execute("SELECT COUNT(obs_id), MIN(obs_id) FROM latest_pbp_events").fetchone() == (
        5,
        second,
    )
    shifts_one, shifts_two = _obs(conn, NHL_SHIFTS, T1), _obs(conn, NHL_SHIFTS, T2)
    write_shifts(conn, shifts_one, _shifts())
    write_shifts(conn, shifts_two, ShiftRows(GAME, _shifts().shifts[:3], 3))
    assert conn.execute("SELECT COUNT(obs_id) FROM latest_shifts").fetchone() == (3,)


def test_the_upgrade_from_version_ten_adds_empty_tables_and_keeps_every_row(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v10.db"
    store = connect(path)
    add_game(store, 7, "2026-01-05T00:00:00+00:00")
    drop_event_tables(store)
    store.execute("UPDATE schema_version SET version = 10")
    store.commit()
    store.close()
    report = upgrade_store(path)
    assert (report.from_version, report.to_version) == (10, SCHEMA_VERSION)
    upgraded = connect(path)
    try:
        for table in ("pbp_events", "pbp_roster", "shifts", "espn_skater_toi"):
            assert upgraded.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
        assert upgraded.execute("SELECT COUNT(*) FROM games").fetchone() == (1,)
    finally:
        upgraded.close()
