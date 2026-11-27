"""Tests for the play-by-play parser. They read a saved game and make no network request."""

import copy
import json
from pathlib import Path

import pytest

from hockey_model.sources.http import HttpClient
from hockey_model.sources.nhl_events import (
    PBP_PARSER,
    clock_seconds,
    fetch_pbp,
    game_second,
    parse_pbp,
    pbp_url,
)
from hockey_model.sources.shape_errors import SourceParseError, SourceShapeError

FIXTURE = Path(__file__).parent.parent / "fixtures" / "nhl" / "pbp_2025020709.json"
GAME = 2025020709


def load() -> dict:
    return json.loads(FIXTURE.read_text())


def test_the_parser_names_its_version() -> None:
    assert PBP_PARSER.startswith("nhl_pbp:")


def test_a_saved_game_gives_its_teams_roster_and_events() -> None:
    rows = parse_pbp(load())
    assert (rows.game_id, rows.home, rows.away) == (GAME, "EDM", "LAK")
    assert len(rows.roster) == 40
    assert len(rows.events) == 325
    assert {s.team for s in rows.roster} == {"EDM", "LAK"}
    assert any(s.position == "G" for s in rows.roster)


def test_the_goals_carry_their_players_and_the_score() -> None:
    goals = [e for e in parse_pbp(load()).events if e.type == "goal"]
    assert len(goals) == 7
    first = goals[0]
    assert (first.owner_team, first.period, first.period_second) == ("EDM", 1, 552)
    assert first.players == (8477934, 8477953, None)
    assert (first.home_score, first.away_score) == (1, 0)


def test_a_shootout_event_has_no_second_of_the_game() -> None:
    events = parse_pbp(load()).events
    shootout = [e for e in events if e.period_type == "SO"]
    assert shootout
    assert all(e.game_second is None for e in shootout)
    assert all(e.game_second is not None for e in events if e.period_type != "SO")


@pytest.mark.parametrize(
    ("period", "second", "expected"),
    [(1, 0, 0), (1, 1200, 1200), (2, 0, 1200), (3, 600, 3000), (4, 0, 3600), (5, 0, 4800)],
)
def test_the_second_of_the_game_follows_the_period(period: int, second: int, expected: int) -> None:
    """A regular season overtime period starts at 3600, and playoff overtime period 5 at 4800."""
    assert game_second(period, second) == expected


def test_an_overtime_event_of_the_saved_game_sits_after_regulation() -> None:
    events = [e for e in parse_pbp(load()).events if e.period_type == "OT"]
    assert events
    assert all(3600 <= (e.game_second or 0) <= 3900 for e in events)


def test_the_attack_direction_follows_the_side_that_the_home_team_defends() -> None:
    payload = load()
    shot = next(p for p in payload["plays"] if p["typeDescKey"] == "shot-on-goal")
    owner = shot["details"]["eventOwnerTeamId"]
    home_id = payload["homeTeam"]["id"]
    shot["homeTeamDefendingSide"] = "left"
    left = next(e for e in parse_pbp(payload).events if e.event_id == shot["eventId"])
    shot["homeTeamDefendingSide"] = "right"
    right = next(e for e in parse_pbp(payload).events if e.event_id == shot["eventId"])
    # The home team defends the left side, so it attacks toward positive x.
    assert left.attack_x_sign == (1 if owner == home_id else -1)
    assert right.attack_x_sign == -left.attack_x_sign


def test_an_event_with_no_defending_side_has_no_direction() -> None:
    payload = load()
    shot = next(p for p in payload["plays"] if p["typeDescKey"] == "shot-on-goal")
    del shot["homeTeamDefendingSide"]
    found = next(e for e in parse_pbp(payload).events if e.event_id == shot["eventId"])
    assert found.attack_x_sign is None


def test_an_event_with_no_owner_has_no_direction() -> None:
    events = parse_pbp(load()).events
    stoppage = next(e for e in events if e.type == "stoppage" and e.owner_team is None)
    assert stoppage.attack_x_sign is None


def test_a_bad_defending_side_is_a_named_error() -> None:
    payload = load()
    shot = next(p for p in payload["plays"] if p["typeDescKey"] == "shot-on-goal")
    shot["homeTeamDefendingSide"] = "up"
    with pytest.raises(SourceParseError, match="left or right"):
        parse_pbp(payload)


@pytest.mark.parametrize("key", ["id", "homeTeam", "awayTeam", "plays", "rosterSpots"])
def test_a_renamed_top_level_key_is_a_named_error(key: str) -> None:
    payload = load()
    payload[f"{key}_renamed"] = payload.pop(key)
    with pytest.raises(SourceParseError, match=key):
        parse_pbp(payload)


@pytest.mark.parametrize("key", ["eventId", "periodDescriptor", "timeInPeriod", "typeDescKey"])
def test_a_renamed_play_key_is_a_named_error(key: str) -> None:
    payload = load()
    payload["plays"][3][f"{key}_renamed"] = payload["plays"][3].pop(key)
    with pytest.raises(SourceParseError, match=key):
        parse_pbp(payload)


@pytest.mark.parametrize("value", ["7:5", "07:60", "x", "٠٠:٠٠", 5, None])
def test_a_bad_clock_is_a_named_error(value: object) -> None:
    payload = load()
    payload["plays"][3]["timeInPeriod"] = value
    with pytest.raises(SourceParseError, match="MM:SS"):
        parse_pbp(payload)


def test_clock_seconds_reads_minutes_and_seconds() -> None:
    assert clock_seconds("19:59", "x") == 1199
    assert clock_seconds("0:05", "x") == 5


@pytest.mark.parametrize("period_type", ["PLAYOFF", "", None])
def test_an_unknown_period_type_is_a_named_error(period_type: object) -> None:
    payload = load()
    payload["plays"][3]["periodDescriptor"]["periodType"] = period_type
    with pytest.raises(SourceParseError, match="period"):
        parse_pbp(payload)


def test_a_period_below_one_is_a_named_error() -> None:
    payload = load()
    payload["plays"][3]["periodDescriptor"]["number"] = 0
    with pytest.raises(SourceParseError, match="not a known period"):
        parse_pbp(payload)


@pytest.mark.parametrize("code", ["155", "15511", "abcd", 1551])
def test_a_bad_situation_code_is_a_named_error(code: object) -> None:
    payload = load()
    payload["plays"][3]["situationCode"] = code
    with pytest.raises(SourceParseError, match="situationCode"):
        parse_pbp(payload)


def test_an_unknown_team_code_is_a_named_error() -> None:
    payload = load()
    payload["homeTeam"]["abbrev"] = "XXX"
    with pytest.raises(SourceShapeError):
        parse_pbp(payload)


def test_an_unknown_owner_team_id_is_a_named_error() -> None:
    payload = load()
    shot = next(p for p in payload["plays"] if p["typeDescKey"] == "shot-on-goal")
    shot["details"]["eventOwnerTeamId"] = 999
    with pytest.raises(SourceParseError, match="unknown team id 999"):
        parse_pbp(payload)


def test_a_roster_spot_for_an_unknown_team_is_a_named_error() -> None:
    payload = load()
    payload["rosterSpots"][0]["teamId"] = 999
    with pytest.raises(SourceParseError, match="unknown team id 999"):
        parse_pbp(payload)


def test_an_event_listed_twice_is_a_named_error() -> None:
    payload = load()
    payload["plays"].append(copy.deepcopy(payload["plays"][0]))
    with pytest.raises(SourceParseError, match="lists event .* twice"):
        parse_pbp(payload)


def test_both_teams_must_be_different() -> None:
    payload = load()
    payload["awayTeam"] = dict(payload["homeTeam"])
    with pytest.raises(SourceParseError, match="both teams"):
        parse_pbp(payload)


def test_a_coordinate_that_is_not_a_number_is_a_named_error() -> None:
    payload = load()
    shot = next(p for p in payload["plays"] if p["typeDescKey"] == "shot-on-goal")
    shot["details"]["xCoord"] = "far"
    with pytest.raises(SourceParseError, match="xCoord"):
        parse_pbp(payload)


def test_the_parser_does_not_change_its_input() -> None:
    payload = load()
    before = copy.deepcopy(payload)
    parse_pbp(payload)
    assert payload == before


class _Client(HttpClient):
    def __init__(self, body: bytes) -> None:
        super().__init__()
        self.body = body
        self.urls: list[str] = []

    def get(self, source: str, url: str) -> bytes:
        self.urls.append(url)
        return self.body


def test_fetch_reads_the_url_of_the_game() -> None:
    client = _Client(FIXTURE.read_bytes())
    assert fetch_pbp(client, GAME).game_id == GAME
    assert client.urls == [pbp_url(GAME)]
    assert pbp_url(GAME).endswith(f"/gamecenter/{GAME}/play-by-play")


def test_fetch_checks_that_the_payload_names_the_game() -> None:
    with pytest.raises(SourceParseError, match="names game"):
        fetch_pbp(_Client(FIXTURE.read_bytes()), 2025020710)
