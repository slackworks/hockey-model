"""Tests for the payload archive: one observation per fetch, one payload per hash."""

import json
import sqlite3
import zlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from refresh_support import FakeClient, fixture
from seeding import with_market_fields

from hockey_model.sources.sanitize import strip_market_fields
from hockey_model.store.archive import (
    ARCHIVED_SOURCES,
    HTTP_ERROR,
    OK,
    SHAPE_ERROR,
    STORE_ERROR,
    Fetch,
    canonical_bytes,
    observations_for,
    payload_bytes,
    record_observation,
)
from hockey_model.store.db import SCHEMA_VERSION, StoreError, connect
from hockey_model.store.refresh_context import RefreshRun
from hockey_model.store.source_reads import KNOWN_SOURCES, MONEYPUCK
from hockey_model.store.upgrade import upgrade_store

FIRST = datetime(2026, 1, 10, 10, 0, tzinfo=UTC)
SECOND = datetime(2026, 1, 10, 11, 0, tzinfo=UTC)
THIRD = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
URL = "https://api-web.nhle.com/v1/score/2026-01-10"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "store.db")
    yield connection
    connection.close()


def _fetch(body: bytes | None, when: datetime, status: str = OK) -> Fetch:
    return Fetch("nhl_scores", URL, when, status, "nhl_score:1", key="2026-01-10", body=body)


def test_same_bytes_twice_gives_two_observations_and_one_payload(
    conn: sqlite3.Connection,
) -> None:
    body = b'{"games": []}'
    record_observation(conn, _fetch(body, FIRST))
    record_observation(conn, _fetch(body, SECOND))
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone() == (2,)
    assert conn.execute("SELECT COUNT(*) FROM payloads").fetchone() == (1,)


def test_a_then_b_then_a_gives_three_observations_and_two_payloads(
    conn: sqlite3.Connection,
) -> None:
    bodies = [b'{"n": 1}', b'{"n": 2}', b'{"n": 1}']
    for body, when in zip(bodies, (FIRST, SECOND, THIRD), strict=True):
        record_observation(conn, _fetch(body, when))
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone() == (3,)
    assert conn.execute("SELECT COUNT(*) FROM payloads").fetchone() == (2,)
    found = observations_for(conn, "nhl_scores", "2026-01-10")
    assert [o.fetched_at for o in found] == [FIRST, SECOND, THIRD]
    assert payload_bytes(conn, str(found[0].sha256)) == bodies[0]
    assert found[0].sha256 == found[2].sha256


def test_before_keeps_the_observations_that_came_first(conn: sqlite3.Connection) -> None:
    record_observation(conn, _fetch(b"a", FIRST))
    record_observation(conn, _fetch(b"b", SECOND))
    found = observations_for(conn, "nhl_scores", "2026-01-10", before=SECOND)
    assert [o.fetched_at for o in found] == [FIRST]


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(offset=st.integers(min_value=-120, max_value=120))
def test_before_keeps_exactly_the_observations_below_the_instant(
    conn: sqlite3.Connection, offset: int
) -> None:
    """The filter follows the stored time, not the order of the rows."""
    first = record_observation(conn, _fetch(b"a", FIRST))
    second = record_observation(conn, _fetch(b"b", SECOND))
    instant = FIRST + timedelta(minutes=offset)
    found = observations_for(conn, "nhl_scores", "2026-01-10", before=instant)
    # The fixture is not reset between examples, so compare the rows this example wrote.
    assert {o.obs_id for o in found} & {first, second} == {
        obs_id for obs_id, when in ((first, FIRST), (second, SECOND)) if when < instant
    }


def test_observations_for_reads_one_key_only(conn: sqlite3.Connection) -> None:
    record_observation(conn, _fetch(b"a", FIRST))
    record_observation(conn, Fetch("nhl_scores", URL, SECOND, OK, "p", key="2026-01-11"))
    assert len(observations_for(conn, "nhl_scores", "2026-01-10")) == 1


def test_a_key_of_none_reads_the_rows_with_no_key(conn: sqlite3.Connection) -> None:
    """The ESPN feeds carry no key, and ``IS`` matches the NULL rows."""
    record_observation(conn, Fetch("espn_injuries", "u", FIRST, OK, "espn:1"))
    found = observations_for(conn, "espn_injuries", None)
    assert [o.key for o in found] == [None]


def test_a_shape_failure_keeps_the_bytes(conn: sqlite3.Connection) -> None:
    body = b'{"unexpected": "shape"}'
    obs_id = record_observation(conn, _fetch(body, FIRST, SHAPE_ERROR))
    (row,) = observations_for(conn, "nhl_scores", "2026-01-10")
    assert (row.obs_id, row.status) == (obs_id, SHAPE_ERROR)
    assert payload_bytes(conn, str(row.sha256)) == body


def test_an_http_failure_stores_no_bytes(conn: sqlite3.Connection) -> None:
    record_observation(conn, _fetch(None, FIRST, HTTP_ERROR))
    (row,) = observations_for(conn, "nhl_scores", "2026-01-10")
    assert row.sha256 is None
    assert conn.execute("SELECT COUNT(*) FROM payloads").fetchone() == (0,)


def test_a_store_failure_stores_its_error_text(conn: sqlite3.Connection) -> None:
    fetch = Fetch("nhl_scores", URL, FIRST, STORE_ERROR, "nhl_score:1", body=b"oops", error="bad")
    record_observation(conn, fetch)
    (row,) = observations_for(conn, "nhl_scores", None)
    assert row.error == "bad"


def test_the_stored_payload_is_the_exact_bytes(conn: sqlite3.Connection) -> None:
    body = json.dumps({"games": [{"id": 1}], "z": "é"}).encode()
    record_observation(conn, _fetch(body, FIRST))
    (row,) = observations_for(conn, "nhl_scores", "2026-01-10")
    assert payload_bytes(conn, str(row.sha256)) == body


def test_the_stored_body_is_compressed_and_records_its_size(conn: sqlite3.Connection) -> None:
    body = b"x" * 10_000
    record_observation(conn, _fetch(body, FIRST))
    stored, size = conn.execute("SELECT body, size FROM payloads").fetchone()
    assert size == len(body)
    assert len(stored) < len(body)


def test_a_missing_hash_is_named(conn: sqlite3.Connection) -> None:
    with pytest.raises(StoreError, match="0" * 64):
        payload_bytes(conn, "0" * 64)


def test_a_corrupt_body_is_refused_and_named(conn: sqlite3.Connection) -> None:
    """A body that is not zlib data must name its hash, not raise a bare ``zlib.error``."""
    sha = "a" * 64
    conn.execute("INSERT INTO payloads (sha256, body, size) VALUES (?, ?, ?)", (sha, b"junk", 4))
    with pytest.raises(StoreError, match=sha):
        payload_bytes(conn, sha)


def test_a_body_that_expands_past_its_size_is_refused(conn: sqlite3.Connection) -> None:
    """The recorded size bounds the output, so a row cannot expand without a limit."""
    sha = "b" * 64
    body = zlib.compress(b"x" * 1000, 6)
    conn.execute("INSERT INTO payloads (sha256, body, size) VALUES (?, ?, ?)", (sha, body, 10))
    with pytest.raises(StoreError, match="more than the 10 bytes"):
        payload_bytes(conn, sha)


def test_an_unknown_status_is_refused(conn: sqlite3.Connection) -> None:
    with pytest.raises(StoreError, match="not one of"):
        record_observation(conn, _fetch(b"a", FIRST, "timeout"))


def test_canonical_bytes_ignores_the_key_order() -> None:
    assert canonical_bytes({"a": 1, "b": 2}) == canonical_bytes({"b": 2, "a": 1})


_SCALARS = st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text()


def _json_value(depth: int = 3) -> st.SearchStrategy[object]:
    """Build a JSON value, nested to ``depth``, with keys that differ only in order."""
    if depth == 0:
        return _SCALARS
    branch: st.SearchStrategy[object] = st.lists(_json_value(depth - 1), max_size=3)
    mapping = st.dictionaries(st.text(), _json_value(depth - 1), max_size=3)
    return _SCALARS | branch | mapping


@settings(max_examples=100, deadline=None)
@given(value=_json_value())
def test_canonical_bytes_ignores_key_order_at_every_depth(value: object) -> None:
    """Two payloads that differ only in key order share one hash, at any nesting depth."""

    def reorder(node: object) -> object:
        if isinstance(node, dict):
            return {key: reorder(node[key]) for key in sorted(node, reverse=True)}
        if isinstance(node, list):
            return [reorder(item) for item in node]
        return node

    assert canonical_bytes(value) == canonical_bytes(reorder(value))


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(body=st.binary(max_size=4096))
def test_a_stored_body_round_trips_exactly(conn: sqlite3.Connection, body: bytes) -> None:
    """Every body that the archive accepts reads back as the same bytes (#82)."""
    record_observation(conn, _fetch(body, FIRST))
    # The fixture is not reset between examples, so the newest row is the one this example wrote.
    last = observations_for(conn, "nhl_scores", "2026-01-10")[-1]
    assert payload_bytes(conn, str(last.sha256)) == body


def test_an_espn_payload_stores_no_market_field(conn: sqlite3.Connection) -> None:
    """The sanitizer runs before the hash, so the stored bytes hold no market key (#82)."""
    payload = with_market_fields(json.loads(fixture("espn/scoreboard_20261103.json")))
    body = canonical_bytes(strip_market_fields(payload))
    fetch = Fetch("espn_starters", "u", FIRST, OK, "espn_scoreboard:1", body=body)
    record_observation(conn, fetch)
    (row,) = observations_for(conn, "espn_starters", None)
    stored = payload_bytes(conn, str(row.sha256))
    for key in (b"odds", b"pickcenter", b"againstTheSpread"):
        assert key not in stored


# Each source whose refresh step keeps no body, with the reason. A new source must join
# ARCHIVED_SOURCES, and must join this table when its bytes do not go to ``payloads``.
NO_BODY_KEPT = {
    "moneypuck": "the team file is large and changes daily, so its tables keep each row",
}


def test_every_source_that_a_refresh_records_is_archived() -> None:
    """A new source cannot skip the archive by accident (docs/design/payload-archive.md)."""
    assert ARCHIVED_SOURCES == KNOWN_SOURCES


def test_attempt_refuses_a_source_that_is_not_archived(conn: sqlite3.Connection) -> None:
    """The gate runs before the request, so a new source fails loud instead of losing bytes."""
    run = RefreshRun(conn, FakeClient(), FIRST)
    with pytest.raises(StoreError, match="ARCHIVED_SOURCES"):
        run.attempt("not_a_source", "https://example.test/a", lambda stamp: 0)


def test_the_one_source_with_no_body_is_the_one_the_design_names() -> None:
    """The MoneyPuck team file is the only source that writes an observation and no bytes."""
    assert set(NO_BODY_KEPT) == {MONEYPUCK}


def test_the_archive_tables_reach_a_version_8_store(tmp_path: Path) -> None:
    """The 8->9 step adds the two tables and the index, and keeps every row."""
    path = tmp_path / "v8.db"
    conn = connect(path)
    conn.execute("DROP TABLE observations")
    conn.execute("DROP TABLE payloads")
    conn.execute("UPDATE schema_version SET version = 8")
    conn.execute(
        "INSERT INTO games (game_id, season, start_utc, fetched_at) VALUES (1, 2026, 'z', 'z')"
    )
    conn.commit()
    conn.close()
    report = upgrade_store(path)
    assert (report.from_version, report.to_version) == (8, SCHEMA_VERSION)
    upgraded = connect(path)
    try:
        tables = {
            row[0]
            for row in upgraded.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"payloads", "observations"} <= tables
        assert upgraded.execute("SELECT COUNT(*) FROM games").fetchone() == (1,)
        indexes = {
            row[0]
            for row in upgraded.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert "observations_key" in indexes
    finally:
        upgraded.close()
