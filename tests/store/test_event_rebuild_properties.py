"""Property tests: a rebuild gives the rows of a first parse of the same bytes (#35)."""

import json
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from json_mutation import mutations

from hockey_model.sources.espn_summary import SUMMARY_PARSER, parse_summary
from hockey_model.sources.nhl_events import PBP_PARSER, parse_pbp, pbp_url
from hockey_model.sources.nhl_shifts import SHIFTS_PARSER, parse_shifts, shifts_url
from hockey_model.sources.shape_errors import SourceParseError, SourceShapeError
from hockey_model.store import event_writes
from hockey_model.store.archive import SHAPE_ERROR, Fetch, record_observation
from hockey_model.store.db import connect
from hockey_model.store.event_rebuild import KINDS
from hockey_model.store.rebuild import rebuild_source
from hockey_model.store.source_reads import ESPN_SUMMARY, NHL_PBP, NHL_SHIFTS

FIXTURES = Path(__file__).parent.parent / "fixtures"
EVENT = "401803060"
GAME = 2025020709
WHEN = datetime(2026, 1, 11, 6, 0, tzinfo=UTC)
ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/summary?event=401803060"
# source: (parser, fixture, url, table)
SOURCES = {
    NHL_PBP: (PBP_PARSER, "nhl/pbp_2025020709.json", pbp_url(GAME), "pbp_events"),
    NHL_SHIFTS: (SHIFTS_PARSER, "nhl/shifts_2025020709.json", shifts_url(GAME), "shifts"),
    ESPN_SUMMARY: (SUMMARY_PARSER, "espn/summary_401803060.json", ESPN_URL, "espn_skater_toi"),
}
DOCS = {source: json.loads((FIXTURES / SOURCES[source][1]).read_text()) for source in SOURCES}
FIRST_PARSE = {
    NHL_PBP: lambda conn, obs, doc: event_writes.write_pbp(conn, obs, parse_pbp(doc, GAME)),
    NHL_SHIFTS: lambda conn, obs, doc: event_writes.write_shifts(
        conn, obs, parse_shifts(doc, GAME)
    ),
    ESPN_SUMMARY: lambda conn, obs, doc: event_writes.write_espn(
        conn, obs, GAME, parse_summary(doc, EVENT)
    ),
}
PROPERTY = settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)


def _rows(conn: sqlite3.Connection, table: str) -> list[tuple]:
    return conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2, 3, 4").fetchall()


def _archived(conn: sqlite3.Connection, source: str, body: bytes) -> int:
    parser, _, url, _ = SOURCES[source]
    fetch = Fetch(source, url, WHEN, SHAPE_ERROR, parser, str(GAME), body)
    obs = record_observation(conn, fetch)
    conn.commit()
    return obs


def _check(source: str, doc: object) -> None:
    parser, _, _, table = SOURCES[source]
    with tempfile.TemporaryDirectory() as tmp:
        conn = connect(Path(tmp) / "store.db")
        try:
            obs = _archived(conn, source, json.dumps(doc).encode())
            rebuild_source(conn, source, parser)
            rebuilt = _rows(conn, table)
            rebuild_source(conn, source, parser)
            assert _rows(conn, table) == rebuilt
            conn.execute(f"DELETE FROM {table}")
            try:
                FIRST_PARSE[source](conn, obs, doc)
            except SourceParseError, SourceShapeError:
                assert rebuilt == []
                return
            assert _rows(conn, table) == rebuilt
        finally:
            conn.close()


@PROPERTY
@given(doc=mutations(DOCS[NHL_PBP]))
def test_a_rebuild_of_a_changed_play_by_play_equals_a_first_parse(doc: object) -> None:
    _check(NHL_PBP, doc)


@PROPERTY
@given(doc=mutations(DOCS[NHL_SHIFTS]))
def test_a_rebuild_of_a_changed_shift_chart_equals_a_first_parse(doc: object) -> None:
    _check(NHL_SHIFTS, doc)


@PROPERTY
@given(doc=mutations(DOCS[ESPN_SUMMARY]))
def test_a_rebuild_of_a_changed_espn_summary_equals_a_first_parse(doc: object) -> None:
    _check(ESPN_SUMMARY, doc)


def test_every_event_source_has_a_property() -> None:
    assert set(FIRST_PARSE) == set(KINDS) == set(SOURCES) == set(DOCS)
