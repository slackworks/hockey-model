"""Tests for ``store rebuild`` on the event sources: archived bytes parsed again (#35)."""

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hockey_model.sources.espn_summary import SUMMARY_PARSER
from hockey_model.sources.nhl_events import PBP_PARSER, pbp_url
from hockey_model.sources.nhl_shifts import SHIFTS_PARSER, shifts_url
from hockey_model.store import event_rebuild, event_writes
from hockey_model.store.archive import OK, SHAPE_ERROR, Fetch, record_observation
from hockey_model.store.db import StoreError, connect
from hockey_model.store.rebuild import rebuild_source
from hockey_model.store.source_reads import ESPN_SUMMARY, NHL_PBP, NHL_SHIFTS

FIXTURES = Path(__file__).parent.parent / "fixtures"
GAME = 2025020709
WHEN = datetime(2026, 1, 11, 6, 0, tzinfo=UTC)
ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/summary?event=401803060"
SOURCES = {
    NHL_PBP: (PBP_PARSER, "nhl/pbp_2025020709.json", pbp_url(GAME), "pbp_events"),
    NHL_SHIFTS: (SHIFTS_PARSER, "nhl/shifts_2025020709.json", shifts_url(GAME), "shifts"),
    ESPN_SUMMARY: (SUMMARY_PARSER, "espn/summary_401803060.json", ESPN_URL, "espn_skater_toi"),
}


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "store.db")
    yield connection
    connection.close()


def _archive(conn: sqlite3.Connection, source: str, body: bytes | None = None, status: str = OK):
    parser, name, url, _ = SOURCES[source]
    data = (FIXTURES / name).read_bytes() if body is None else body
    obs = record_observation(conn, Fetch(source, url, WHEN, status, parser, str(GAME), data))
    conn.commit()
    return obs


def _rows(conn: sqlite3.Connection, table: str) -> list[tuple]:
    return conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2, 3, 4").fetchall()


@pytest.mark.parametrize("source", sorted(SOURCES))
def test_a_rebuild_gives_the_rows_of_a_first_parse(conn: sqlite3.Connection, source: str) -> None:
    parser, _, _, table = SOURCES[source]
    _archive(conn, source)
    report = rebuild_source(conn, source, parser)
    first = _rows(conn, table)
    assert first
    assert (report.games, report.derived, report.rejected) == (1, 1, 0)
    rebuild_source(conn, source, parser)
    assert _rows(conn, table) == first


@pytest.mark.parametrize("source", sorted(SOURCES))
def test_a_rebuild_replaces_stale_rows_of_the_observation(
    conn: sqlite3.Connection, source: str
) -> None:
    parser, _, _, table = SOURCES[source]
    obs = _archive(conn, source)
    rebuild_source(conn, source, parser)
    good = _rows(conn, table)
    conn.execute(f"DELETE FROM {table} WHERE obs_id = ? AND rowid % 2 = 0", (obs,))
    conn.commit()
    assert _rows(conn, table) != good
    rebuild_source(conn, source, parser)
    assert _rows(conn, table) == good


def test_a_rebuild_keeps_the_rows_of_each_observation(conn: sqlite3.Connection) -> None:
    first = _archive(conn, NHL_PBP)
    second = _archive(conn, NHL_PBP)
    report = rebuild_source(conn, NHL_PBP, PBP_PARSER)
    assert report.games == 2
    counts = dict(conn.execute("SELECT obs_id, COUNT(obs_id) FROM pbp_events GROUP BY obs_id"))
    assert counts == {first: 325, second: 325}


def test_a_new_parser_rewrites_the_rows_with_its_name(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _archive(conn, NHL_PBP)
    rebuild_source(conn, NHL_PBP, PBP_PARSER)
    assert conn.execute("SELECT DISTINCT parser FROM pbp_events").fetchall() == [(PBP_PARSER,)]
    kind = event_rebuild.KINDS[NHL_PBP]
    monkeypatch.setitem(event_rebuild.KINDS, NHL_PBP, replace(kind, parser="nhl_pbp:99"))
    monkeypatch.setattr(event_writes, "PBP_PARSER", "nhl_pbp:99")
    rebuild_source(conn, NHL_PBP, "nhl_pbp:99")
    assert conn.execute("SELECT DISTINCT parser FROM pbp_events").fetchall() == [("nhl_pbp:99",)]
    assert conn.execute("SELECT COUNT(obs_id) FROM pbp_events").fetchone() == (325,)


def test_a_stale_parser_name_and_an_unknown_source_are_refused(conn: sqlite3.Connection) -> None:
    with pytest.raises(StoreError, match=f"--parser {PBP_PARSER}"):
        rebuild_source(conn, NHL_PBP, "nhl_pbp:0")
    with pytest.raises(StoreError, match="nhl_pbp"):
        rebuild_source(conn, "moneypuck", "x:1")


def test_a_payload_that_was_a_shape_error_and_stays_one_is_named(conn: sqlite3.Connection) -> None:
    _archive(conn, NHL_PBP)
    broken = json.loads((FIXTURES / "nhl" / "pbp_2025020709.json").read_text())
    del broken["plays"]
    bad = _archive(conn, NHL_PBP, json.dumps(broken).encode(), SHAPE_ERROR)
    report = rebuild_source(conn, NHL_PBP, PBP_PARSER)
    assert (report.games, report.derived, report.rejected) == (2, 1, 1)
    assert f"observation {bad}" in report.refused[0]
    query = "SELECT COUNT(obs_id) FROM pbp_events WHERE obs_id = ?"
    assert conn.execute(query, (bad,)).fetchone() == (0,)


def test_a_payload_that_was_ok_and_is_refused_now_stops_the_rebuild(
    conn: sqlite3.Connection,
) -> None:
    broken = json.loads((FIXTURES / "nhl" / "pbp_2025020709.json").read_text())
    del broken["plays"]
    bad = _archive(conn, NHL_PBP, json.dumps(broken).encode(), OK)
    with pytest.raises(StoreError, match=rf"observation {bad}: the parser .* --allow-refused"):
        rebuild_source(conn, NHL_PBP, PBP_PARSER)
    report = rebuild_source(conn, NHL_PBP, PBP_PARSER, allow_refused=True)
    assert report.rejected == 1


def test_a_deeply_nested_archived_body_is_a_refusal_and_not_a_crash(
    conn: sqlite3.Connection,
) -> None:
    _archive(conn, NHL_SHIFTS, b"[" * 200_000, SHAPE_ERROR)
    report = rebuild_source(conn, NHL_SHIFTS, SHIFTS_PARSER)
    assert report.rejected == 1
    assert "not valid JSON" in report.refused[0]


def test_an_observation_with_no_game_key_is_a_named_error(conn: sqlite3.Connection) -> None:
    parser, name, url, _ = SOURCES[NHL_SHIFTS]
    body = (FIXTURES / name).read_bytes()
    record_observation(conn, Fetch(NHL_SHIFTS, url, WHEN, OK, parser, None, body))
    with pytest.raises(StoreError, match="not a game id"):
        rebuild_source(conn, NHL_SHIFTS, SHIFTS_PARSER)


def test_an_espn_observation_with_no_event_in_its_url_is_a_refusal(
    conn: sqlite3.Connection,
) -> None:
    parser, name, _, _ = SOURCES[ESPN_SUMMARY]
    body = (FIXTURES / name).read_bytes()
    record_observation(
        conn,
        Fetch(ESPN_SUMMARY, "https://x.test/summary", WHEN, SHAPE_ERROR, parser, str(GAME), body),
    )
    report = rebuild_source(conn, ESPN_SUMMARY, SUMMARY_PARSER)
    assert report.rejected == 1


def test_a_damaged_payload_row_names_the_observation(conn: sqlite3.Connection) -> None:
    obs = _archive(conn, NHL_PBP)
    conn.execute("UPDATE payloads SET body = x'00'")
    conn.commit()
    with pytest.raises(StoreError, match=rf"rebuild nhl_pbp observation {obs}"):
        rebuild_source(conn, NHL_PBP, PBP_PARSER)


def test_an_observation_with_no_bytes_is_skipped(conn: sqlite3.Connection) -> None:
    record_observation(
        conn, Fetch(NHL_PBP, pbp_url(GAME), WHEN, "http_error", PBP_PARSER, str(GAME), None, "503")
    )
    report = rebuild_source(conn, NHL_PBP, PBP_PARSER)
    assert (report.games, report.derived) == (0, 0)
