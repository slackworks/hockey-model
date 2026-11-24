"""Tests for the capture at the HTTP boundary: the parser name, the key, and the sanitizer."""

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from refresh_support import FakeClient, fixture
from seeding import with_market_fields

from hockey_model.sources.errors import SourceError
from hockey_model.sources.http import HttpClient
from hockey_model.sources.shape_errors import SourceParseError
from hockey_model.store import refresh_other
from hockey_model.store.archive import OK, observations_for, payload_bytes
from hockey_model.store.db import StoreError, connect
from hockey_model.store.recording import Reading, RecordingClient, key_of
from hockey_model.store.refresh_context import RefreshRun

NOW = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
SCORE_URL = "https://api-web.nhle.com/v1/score/2026-01-10"
BOX_URL = "https://api-web.nhle.com/v1/gamecenter/2025020697/boxscore"
ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "store.db")
    yield connection
    connection.close()


class _OneBody(HttpClient):
    """A client that returns one body for every URL."""

    def __init__(self, body: bytes) -> None:
        super().__init__()
        self.body = body

    def get(self, source: str, url: str) -> bytes:
        return self.body


class _Fails(HttpClient):
    """A client that fails every request."""

    def get(self, source: str, url: str) -> bytes:
        raise SourceError(source, url, "HTTP 403")


def test_a_score_day_key_is_its_date() -> None:
    assert key_of("nhl_scores", SCORE_URL) == "2026-01-10"


def test_a_boxscore_key_is_its_game() -> None:
    assert key_of("nhl_boxscores", BOX_URL) == "2025020697"


def test_a_live_feed_key_is_none() -> None:
    assert key_of("espn_injuries", ESPN_URL) is None


def test_a_url_that_names_no_day_gets_no_key() -> None:
    """The pattern accepts a date that is not a day, and the key must not carry it."""
    assert key_of("nhl_scores", "https://api-web.nhle.com/v1/score/2026-13-45") is None


def test_a_url_with_no_date_suffix_gets_no_key() -> None:
    assert key_of("nhl_scores", "https://api-web.nhle.com/v1/score/now") is None


def test_a_market_source_loses_its_market_fields() -> None:
    """The unit reads no odds, and the archive keeps no odds either (#82)."""
    payload = {"events": [{"odds": [{"spread": -1.5}]}], "pickcenter": {"providers": []}}
    client = RecordingClient(_OneBody(json.dumps(payload).encode()), "espn_injuries")
    body = client.get("espn_injuries", ESPN_URL)
    assert json.loads(body) == {"events": [{}]}


@pytest.mark.parametrize("source", ["nhl_scores", "daily_faceoff", "moneypuck", "nhl_boxscores"])
def test_a_source_with_no_market_keeps_every_field(source: str) -> None:
    payload = {"odds": [{"spread": -1.5}]}
    client = RecordingClient(_OneBody(json.dumps(payload).encode()), source)
    assert json.loads(client.get(source, SCORE_URL)) == payload


def test_a_body_that_is_not_json_passes_through_sanitized() -> None:
    """The sanitizer must not turn a non-JSON body into an error the parser cannot name."""
    client = RecordingClient(_OneBody(b"<html>not json</html>"), "espn_injuries")
    assert client.get("espn_injuries", ESPN_URL) == b"<html>not json</html>"


def test_a_source_outside_the_archive_is_refused() -> None:
    """A new source cannot reach the store without a decision on its market fields."""
    with pytest.raises(StoreError, match="not in ARCHIVED_SOURCES"):
        RecordingClient(_OneBody(b"{}"), "a_new_source")


def test_a_failed_request_is_recorded_with_no_body() -> None:
    client = RecordingClient(_Fails(), "nhl_scores")
    with pytest.raises(SourceError):
        client.get("nhl_scores", SCORE_URL)
    assert [(r.url, r.status, r.body) for r in client.records] == [(SCORE_URL, "http_error", None)]


def test_a_refresh_writes_one_observation_for_each_request(conn: sqlite3.Connection) -> None:
    """The unit asks for two URLs, so the archive holds two rows with the same parser."""
    first = "https://example.test/a"
    second = "https://example.test/b"
    run = RefreshRun(conn, _OneBody(b'{"n": 1}'), NOW)

    def unit(stamp: datetime) -> int:
        run.client.get("espn_injuries", first)
        run.client.get("espn_injuries", second)
        return 1

    assert run.attempt("espn_injuries", first, unit, Reading("espn_injuries:2"))
    found = observations_for(conn, "espn_injuries", None)
    assert [o.url for o in found] == [first, second]
    assert {o.parser for o in found} == {"espn_injuries:2"}
    assert all(o.status == "ok" for o in found)


def test_a_good_request_beside_a_failed_one_keeps_its_own_outcome(
    conn: sqlite3.Connection,
) -> None:
    """The espn_starters step reads two days, so one day can fail while the other is good."""
    good = "https://example.test/a"
    bad = "https://example.test/b"
    overrides: dict[str, bytes | Exception] = {
        good: b'{"n": 1}',
        bad: SourceError("espn_starters", bad, "HTTP 503"),
    }
    run = RefreshRun(conn, FakeClient(overrides), NOW)

    def unit(stamp: datetime) -> int:
        run.client.get("espn_starters", good)
        run.client.get("espn_starters", bad)
        return 1

    assert run.attempt("espn_starters", good, unit, Reading("espn_starters:2")) is False
    first, second = observations_for(conn, "espn_starters", None)
    assert (first.url, first.status, first.error) == (good, "ok", None)
    assert first.sha256 is not None
    assert (second.url, second.status, second.sha256) == (bad, "http_error", None)
    assert "HTTP 503" in (second.error or "")


def test_a_failed_unit_still_archives_the_bytes_that_arrived(conn: sqlite3.Connection) -> None:
    """A failed unit keeps the payload, so a second read has the bytes that broke it."""
    body = b'{"unexpected": true}'
    run = RefreshRun(conn, _OneBody(body), NOW)

    def unit(stamp: datetime) -> int:
        run.client.get("nhl_scores", SCORE_URL)
        raise SourceError("nhl_scores", SCORE_URL, "HTTP 500")

    assert run.attempt("nhl_scores", SCORE_URL, unit, Reading("nhl_score:1")) is False
    (found,) = observations_for(conn, "nhl_scores", "2026-01-10")
    assert found.status == "http_error"
    assert payload_bytes(conn, str(found.sha256)) == body


def test_a_parser_failure_marks_the_bytes_that_the_parser_rejected(
    conn: sqlite3.Connection,
) -> None:
    """The parser rejected the good response, so the good response is the shape failure."""
    good = "https://example.test/a"
    other = "https://example.test/b"
    run = RefreshRun(conn, FakeClient({good: b'{"n": 1}', other: b'{"n": 2}'}), NOW)

    def unit(stamp: datetime) -> int:
        run.client.get("espn_starters", good)
        run.client.get("espn_starters", other)
        raise SourceParseError("espn_starters", "a field is missing")

    assert run.attempt("espn_starters", good, unit, Reading("espn_starters:2")) is False
    first, second = observations_for(conn, "espn_starters", None)
    for row in (first, second):
        assert row.status == "shape_error"
        assert "a field is missing" in (row.error or "")
        assert row.sha256 is not None


def test_the_client_is_the_inner_one_again_after_a_unit(conn: sqlite3.Connection) -> None:
    """A recording client that stayed in place would wrap the next unit's client too."""
    inner = FakeClient()
    run = RefreshRun(conn, inner, NOW)
    run.attempt("espn_injuries", ESPN_URL, lambda stamp: 0, Reading("espn_injuries:2"))
    assert run.client is inner


def test_a_reading_that_keeps_no_body_still_writes_the_observation(
    conn: sqlite3.Connection,
) -> None:
    """The MoneyPuck team file writes a row for the fetch and stores no bytes of it."""
    body = b'{"changed": true}'
    run = RefreshRun(conn, _OneBody(body), NOW)

    def unit(stamp: datetime) -> int:
        run.client.get("moneypuck", "https://example.test/teams")
        return 0

    assert run.attempt("moneypuck", "https://example.test/teams", unit, Reading("team:1"))
    assert run.attempt(
        "moneypuck",
        "https://example.test/teams",
        unit,
        Reading("team:1", keep_body=False),
    )
    kept, dropped = observations_for(conn, "moneypuck", None)
    assert payload_bytes(conn, str(kept.sha256)) == body
    assert (dropped.status, dropped.sha256) == ("ok", None)


def test_the_stored_scoreboard_holds_no_market_field(conn: sqlite3.Connection) -> None:
    """The live espn_starters step archives the real fixture with its market keys gone (#82)."""
    payload = with_market_fields(json.loads(fixture("espn/scoreboard_20261103.json")))
    base = ESPN_URL.rsplit("/injuries", 1)[0]
    body = json.dumps(payload).encode()
    # The step asks for today and tomorrow, each in a URL that carries its date.
    dates = (f"{base}/scoreboard?dates=20260110", f"{base}/scoreboard?dates=20260111")
    run = RefreshRun(conn, FakeClient({url: body for url in dates}), NOW)
    refresh_other.espn_starters(run)
    found = observations_for(conn, "espn_starters", None)
    assert [row.url for row in found] == list(dates)
    for row in found:
        assert row.status == OK
        stored = payload_bytes(conn, str(row.sha256))
        for key in (b"pickcenter", b"odds", b"againstTheSpread"):
            assert key not in stored


def test_the_reading_defaults_to_no_parser_and_kept_bytes() -> None:
    reading = Reading("nhl_score:1")
    assert reading.keep_body is True
    assert reading.expected_absence is None
