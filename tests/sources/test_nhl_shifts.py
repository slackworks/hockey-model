"""Tests for the shift chart parser. They read a saved game and make no network request."""

import copy
import json
from pathlib import Path

import pytest

from hockey_model.sources.http import HttpClient
from hockey_model.sources.nhl_events import MAX_PERIOD
from hockey_model.sources.nhl_shifts import (
    SHIFTS_PARSER,
    fetch_shifts,
    parse_shifts,
    shifts_url,
)
from hockey_model.sources.shape_errors import SourceParseError, SourceShapeError

FIXTURE = Path(__file__).parent.parent / "fixtures" / "nhl" / "shifts_2025020709.json"
GAME = 2025020709


def load() -> dict:
    return json.loads(FIXTURE.read_text())


def _shift(payload: dict) -> dict:
    """The first row that is a shift. The first row of the saved chart is a goal row."""
    return next(r for r in payload["data"] if r["typeCode"] == 517)


def test_the_parser_names_its_version() -> None:
    assert SHIFTS_PARSER.startswith("nhl_shifts:")


def test_a_saved_chart_gives_the_shifts_and_counts_every_row() -> None:
    rows = parse_shifts(load(), GAME)
    assert rows.game_id == GAME
    assert rows.rows == 747
    # 740 rows are shifts. The 7 goal rows (type code 505) are not shifts.
    assert len(rows.shifts) == 740
    assert {s.team for s in rows.shifts} == {"EDM", "LAK"}
    assert {s.period for s in rows.shifts} == {1, 2, 3, 4}


def test_a_shift_is_in_seconds_of_the_game() -> None:
    first = parse_shifts(load(), GAME).shifts[0]
    assert (first.period, first.start_second, first.end_second) == (1, 0, 35)
    third = next(s for s in parse_shifts(load(), GAME).shifts if s.period == 3)
    assert 2400 <= third.start_second < 3600


def test_the_length_comes_from_the_ends_when_the_duration_is_null() -> None:
    payload = load()
    for row in payload["data"]:
        row["duration"] = None
    rows = parse_shifts(payload, GAME)
    assert [s.end_second - s.start_second for s in rows.shifts[:2]] == [35, 29]


def test_an_interval_with_no_length_is_kept_for_the_stint_step() -> None:
    payload = load()
    row = _shift(payload)
    row["endTime"] = row["startTime"]
    shift = parse_shifts(payload, GAME).shifts[0]
    assert shift.end_second == shift.start_second


@pytest.mark.parametrize("key", ["data"])
def test_a_renamed_top_level_key_is_a_named_error(key: str) -> None:
    payload = load()
    payload[f"{key}_renamed"] = payload.pop(key)
    with pytest.raises(SourceParseError, match=key):
        parse_shifts(payload, GAME)


@pytest.mark.parametrize(
    "key", ["typeCode", "gameId", "teamAbbrev", "period", "startTime", "endTime", "playerId"]
)
def test_a_renamed_row_key_is_a_named_error(key: str) -> None:
    payload = load()
    row = _shift(payload)
    row[f"{key}_renamed"] = row.pop(key)
    with pytest.raises(SourceParseError, match=key):
        parse_shifts(payload, GAME)


@pytest.mark.parametrize("value", ["7:5", "00:61", "x", None, 3])
def test_a_bad_clock_is_a_named_error(value: object) -> None:
    payload = load()
    _shift(payload)["startTime"] = value
    with pytest.raises(SourceParseError, match="MM:SS"):
        parse_shifts(payload, GAME)


def test_a_row_of_another_game_is_a_named_error() -> None:
    payload = load()
    _shift(payload)["gameId"] = GAME + 1
    with pytest.raises(SourceParseError, match="another game"):
        parse_shifts(payload, GAME)


def test_an_unknown_team_code_is_a_named_error() -> None:
    payload = load()
    _shift(payload)["teamAbbrev"] = "XXX"
    with pytest.raises(SourceShapeError):
        parse_shifts(payload, GAME)


def test_a_shift_number_that_is_not_an_integer_is_a_named_error() -> None:
    payload = load()
    _shift(payload)["shiftNumber"] = "one"
    with pytest.raises(SourceParseError, match="shiftNumber"):
        parse_shifts(payload, GAME)


def test_an_empty_chart_is_a_named_error() -> None:
    with pytest.raises(SourceParseError, match="lists no shift"):
        parse_shifts({"data": [], "total": 0}, GAME)


def test_a_chart_with_only_goal_rows_is_a_named_error() -> None:
    payload = load()
    payload["data"] = [r for r in payload["data"] if r["typeCode"] == 505]
    with pytest.raises(SourceParseError, match="lists no shift"):
        parse_shifts(payload, GAME)


@pytest.mark.parametrize("value", [-1, 2**63, 10**30])
def test_a_player_id_outside_the_store_range_is_a_named_error(value: int) -> None:
    payload = load()
    _shift(payload)["playerId"] = value
    with pytest.raises(SourceParseError, match="out of range"):
        parse_shifts(payload, GAME)


@pytest.mark.parametrize("value", ["20:01", "99:59"])
def test_a_clock_past_the_period_is_a_named_error(value: str) -> None:
    payload = load()
    _shift(payload)["endTime"] = value
    with pytest.raises(SourceParseError, match="past the limit"):
        parse_shifts(payload, GAME)


@pytest.mark.parametrize("period", [0, 21, 10**20])
def test_a_period_outside_the_range_is_a_named_error(period: int) -> None:
    payload = load()
    _shift(payload)["period"] = period
    with pytest.raises(SourceParseError, match="period must be"):
        parse_shifts(payload, GAME)


def test_the_parser_does_not_change_its_input() -> None:
    payload = load()
    before = copy.deepcopy(payload)
    parse_shifts(payload, GAME)
    assert payload == before


class _Client(HttpClient):
    def __init__(self, body: bytes) -> None:
        super().__init__()
        self.body = body
        self.urls: list[str] = []

    def get(self, source: str, url: str) -> bytes:
        self.urls.append(url)
        return self.body


def test_fetch_reads_the_shift_chart_url_of_the_game() -> None:
    client = _Client(FIXTURE.read_bytes())
    assert len(fetch_shifts(client, GAME).shifts) == 740
    assert client.urls == [shifts_url(GAME)]
    assert shifts_url(GAME) == (
        f"https://api.nhle.com/stats/rest/en/shiftcharts?cayenneExp=gameId={GAME}"
    )


def test_a_period_at_the_limit_is_read() -> None:
    payload = load()
    _shift(payload)["period"] = MAX_PERIOD
    assert MAX_PERIOD in {s.period for s in parse_shifts(payload, GAME).shifts}


def test_each_shift_keeps_the_shift_number_of_its_row() -> None:
    payload = load()
    numbers = [r["shiftNumber"] for r in payload["data"] if r["typeCode"] == 517]
    parsed = [s.shift_number for s in parse_shifts(payload, GAME).shifts]
    assert any(n is not None for n in numbers)
    assert parsed == numbers


def test_a_fetch_of_a_body_that_is_not_json_names_the_source() -> None:
    with pytest.raises(SourceParseError, match="^read nhl_shifts data: "):
        fetch_shifts(_Client(b"not json"), GAME)
