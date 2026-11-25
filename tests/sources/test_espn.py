"""Tests for the ESPN parsers. They read fixtures and make no network request."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from hockey_model.game import StarterSource, StarterStatus
from hockey_model.sources.espn import (
    KNOWN_INJURY_STATUSES,
    OUT_STATUSES,
    fetch_injuries,
    fetch_scoreboard_starters,
    parse_injuries,
    parse_scoreboard_starters,
)
from hockey_model.sources.http import HttpClient
from hockey_model.sources.shape_errors import SourceParseError, SourceShapeError

FIXTURES = Path(__file__).parent.parent / "fixtures" / "espn"
SCOREBOARD = "scoreboard_20261103.json"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def reads_by_team(payload: dict) -> dict:
    return {r.team: r for r in parse_scoreboard_starters(payload)}


def test_scoreboard_maps_confirmed_and_expected() -> None:
    reads = reads_by_team(load(SCOREBOARD))
    assert reads["TOR"].goalie_name == "Sergei Bobrovsky"
    assert reads["TOR"].status is StarterStatus.CONFIRMED
    assert reads["UTA"].status is StarterStatus.LIKELY
    assert all(r.source is StarterSource.ESPN for r in reads.values())


def test_scoreboard_maps_espn_team_codes() -> None:
    teams = set(reads_by_team(load(SCOREBOARD)))
    assert {"TBL", "NJD"} <= teams
    assert not {"TB", "NJ"} & teams


def test_scoreboard_reads_start_time() -> None:
    reads = reads_by_team(load(SCOREBOARD))
    assert reads["TOR"].start == datetime(2026, 11, 4, 0, 0, tzinfo=UTC)
    assert reads["NJD"].start == datetime(2026, 11, 4, 0, 30, tzinfo=UTC)


def test_scoreboard_reads_both_sides_of_every_game() -> None:
    assert len(parse_scoreboard_starters(load(SCOREBOARD))) == 22


def test_scoreboard_skips_a_started_game() -> None:
    payload = load(SCOREBOARD)
    payload["events"][0]["status"]["type"]["state"] = "in"
    teams = set(reads_by_team(payload))
    assert "TOR" not in teams
    assert len(parse_scoreboard_starters(payload)) == 20


def test_scoreboard_game_without_probables_gives_no_read() -> None:
    payload = load(SCOREBOARD)
    for competitor in payload["events"][0]["competitions"][0]["competitors"]:
        del competitor["probables"]
    assert len(parse_scoreboard_starters(payload)) == 20


def test_scoreboard_ignores_other_probables() -> None:
    payload = load(SCOREBOARD)
    competitor = payload["events"][0]["competitions"][0]["competitors"][0]
    competitor["probables"][0]["name"] = "probablePitcher"
    assert len(parse_scoreboard_starters(payload)) == 21


def test_scoreboard_reads_unknown_status_with_a_note() -> None:
    payload = load(SCOREBOARD)
    competitor = payload["events"][0]["competitions"][0]["competitors"][0]
    competitor["probables"][0]["status"]["name"] = "Scratched"
    notes: list[str] = []
    reads = parse_scoreboard_starters(payload, notes)
    # One unnamed status must not drop the other 21 starters on the page (#325).
    assert len(reads) == 22
    # The read is kept and stored as unknown, and the label is named in a note.
    unknown = [read for read in reads if read.status is StarterStatus.UNKNOWN]
    assert len(unknown) == 1
    assert len(notes) == 1
    assert "Scratched" in notes[0]
    assert unknown[0].goalie_name in notes[0]


def test_scoreboard_rejects_missing_athlete() -> None:
    payload = load(SCOREBOARD)
    competitor = payload["events"][0]["competitions"][0]["competitors"][0]
    del competitor["probables"][0]["athlete"]
    with pytest.raises(SourceParseError, match="athlete"):
        parse_scoreboard_starters(payload)


def test_scoreboard_rejects_unknown_team() -> None:
    payload = load(SCOREBOARD)
    payload["events"][0]["competitions"][0]["competitors"][0]["team"]["abbreviation"] = "ZZZ"
    with pytest.raises(SourceShapeError, match="ZZZ"):
        parse_scoreboard_starters(payload)


@pytest.mark.parametrize("payload", [[], {}, {"events": {}}, {"events": [1]}])
def test_scoreboard_rejects_wrong_top_shape(payload: object) -> None:
    with pytest.raises(SourceParseError):
        parse_scoreboard_starters(payload)


def test_injuries_read_team_player_status_and_return_date() -> None:
    reads = parse_injuries(load("injuries.json"))
    greer = next(r for r in reads if r.player_name == "A.J. Greer")
    assert (greer.team, greer.status) == ("ANA", "Injured Reserve")
    assert greer.return_date == date(2026, 11, 11)
    assert {r.team for r in reads} >= {"LAK", "SJS", "TBL", "NJD"}


@pytest.mark.parametrize("status", sorted(OUT_STATUSES))
def test_injury_out_statuses_are_out(status: str) -> None:
    payload = load("injuries.json")
    payload["injuries"][0]["injuries"][0]["status"] = status
    assert parse_injuries(payload)[0].is_out


def test_injury_day_to_day_is_not_out() -> None:
    payload = load("injuries.json")
    payload["injuries"][0]["injuries"][0]["status"] = "Day-To-Day"
    assert not parse_injuries(payload)[0].is_out


def test_the_known_status_list_holds_every_out_status_and_day_to_day() -> None:
    """One test reads the list, so a new status changes this test and no other."""
    assert KNOWN_INJURY_STATUSES == set(OUT_STATUSES) | {"Day-To-Day"}
    assert "Day-To-Day" not in OUT_STATUSES


def test_a_suspension_parses_and_counts_as_out() -> None:
    """ESPN writes Suspension, and a suspended player does not play (#325)."""
    payload = load("injuries.json")
    payload["injuries"][0]["injuries"][0]["status"] = "Suspension"
    read = parse_injuries(payload)[0]
    assert read.status == "Suspension"
    assert read.is_out


def test_an_unknown_status_parses_and_counts_as_out() -> None:
    """One unnamed status must not kill the whole injury feed (#325)."""
    payload = load("injuries.json")
    payload["injuries"][0]["injuries"][0]["status"] = "Personal Reasons"
    read = parse_injuries(payload)[0]
    assert read.status == "Personal Reasons"
    assert read.is_out


def test_an_unknown_status_is_named_in_the_notes() -> None:
    payload = load("injuries.json")
    payload["injuries"][0]["injuries"][0]["status"] = "Personal Reasons"
    payload["injuries"][0]["injuries"][0]["athlete"]["displayName"] = "Some Player"
    notes: list[str] = []
    parse_injuries(payload, notes)
    assert len(notes) == 1
    assert "'Personal Reasons'" in notes[0]
    assert "Some Player" in notes[0]


def test_a_known_status_adds_no_note() -> None:
    notes: list[str] = []
    parse_injuries(load("injuries.json"), notes)
    assert notes == []


def test_injury_without_return_date_is_allowed() -> None:
    payload = load("injuries.json")
    del payload["injuries"][0]["injuries"][0]["details"]
    assert parse_injuries(payload)[0].return_date is None


def test_injury_rejects_bad_return_date() -> None:
    payload = load("injuries.json")
    payload["injuries"][0]["injuries"][0]["details"]["returnDate"] = "soon"
    with pytest.raises(SourceParseError, match="returnDate"):
        parse_injuries(payload)


def test_injury_rejects_missing_athlete() -> None:
    payload = load("injuries.json")
    del payload["injuries"][0]["injuries"][0]["athlete"]
    with pytest.raises(SourceParseError, match="athlete"):
        parse_injuries(payload)


class FakeClient(HttpClient):
    def __init__(self, bodies: dict[str, bytes]) -> None:
        super().__init__()
        self.bodies = bodies
        self.urls: list[str] = []

    def get(self, source: str, url: str) -> bytes:
        self.urls.append(url)
        return self.bodies[url]


def test_fetch_scoreboard_uses_dated_url() -> None:
    url = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard?dates=20261103"
    client = FakeClient({url: (FIXTURES / SCOREBOARD).read_bytes()})
    assert len(fetch_scoreboard_starters(client, date(2026, 11, 3))) == 22
    assert client.urls == [url]


def test_fetch_injuries_uses_injuries_url() -> None:
    url = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries"
    client = FakeClient({url: (FIXTURES / "injuries.json").read_bytes()})
    assert len(fetch_injuries(client)) == 20


def test_fetch_rejects_invalid_json() -> None:
    url = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries"
    with pytest.raises(SourceParseError, match="not valid JSON"):
        fetch_injuries(FakeClient({url: b"oops"}))


def test_scoreboard_rejects_a_probable_with_no_name() -> None:
    payload = load(SCOREBOARD)
    competitor = payload["events"][0]["competitions"][0]["competitors"][0]
    del competitor["probables"][0]["name"]
    with pytest.raises(SourceParseError, match="'name'"):
        parse_scoreboard_starters(payload)


def test_scoreboard_rejects_an_event_with_no_competitions() -> None:
    payload = load(SCOREBOARD)
    payload["events"][0]["competitions"] = []
    with pytest.raises(SourceParseError, match="empty 'competitions'"):
        parse_scoreboard_starters(payload)


@pytest.mark.parametrize("value", [20261103, 1.5, True, ["2026-11-03"]])
def test_injury_rejects_a_return_date_that_is_not_text(value: object) -> None:
    payload = load("injuries.json")
    payload["injuries"][0]["injuries"][0]["details"]["returnDate"] = value
    with pytest.raises(SourceParseError, match="returnDate"):
        parse_injuries(payload)


def test_scoreboard_game_with_an_empty_probables_list_gives_no_read() -> None:
    payload = load(SCOREBOARD)
    for competitor in payload["events"][0]["competitions"][0]["competitors"]:
        competitor["probables"] = []
    assert len(parse_scoreboard_starters(payload)) == 20


@pytest.mark.parametrize("value", [None, "none", {}, 3])
def test_scoreboard_probables_that_are_not_a_list_raise(value: object) -> None:
    payload = load(SCOREBOARD)
    payload["events"][0]["competitions"][0]["competitors"][0]["probables"] = value
    with pytest.raises(SourceParseError, match="probables must be a list"):
        parse_scoreboard_starters(payload)


def test_scoreboard_with_no_probables_key_anywhere_raises() -> None:
    payload = load(SCOREBOARD)
    for event in payload["events"]:
        for competitor in event["competitions"][0]["competitors"]:
            competitor.pop("probables", None)
    with pytest.raises(SourceParseError, match="probables"):
        parse_scoreboard_starters(payload)


def test_scoreboard_with_only_started_games_gives_no_read() -> None:
    payload = load(SCOREBOARD)
    for event in payload["events"]:
        event["status"]["type"]["state"] = "in"
        for competitor in event["competitions"][0]["competitors"]:
            competitor.pop("probables", None)
    assert parse_scoreboard_starters(payload) == []
