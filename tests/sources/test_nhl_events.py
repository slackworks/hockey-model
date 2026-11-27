"""Tests for the play-by-play parser. They read a saved game and make no network request."""

import copy
import json
from pathlib import Path

import pytest

from hockey_model.sources.http import HttpClient
from hockey_model.sources.nhl_events import (
    MAX_PERIOD,
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
    rows = parse_pbp(load(), GAME)
    assert (rows.game_id, rows.home, rows.away) == (GAME, "EDM", "LAK")
    assert len(rows.roster) == 40
    assert len(rows.events) == 325
    assert {s.team for s in rows.roster} == {"EDM", "LAK"}
    assert any(s.position == "G" for s in rows.roster)


def test_the_goals_carry_their_players_and_the_score() -> None:
    goals = [e for e in parse_pbp(load(), GAME).events if e.type == "goal"]
    assert len(goals) == 7
    first = goals[0]
    assert (first.owner_team, first.period, first.period_second) == ("EDM", 1, 552)
    assert first.players == (8477934, 8477953, None)
    assert (first.home_score, first.away_score) == (1, 0)


def test_a_shootout_event_has_no_second_of_the_game() -> None:
    events = parse_pbp(load(), GAME).events
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
    events = [e for e in parse_pbp(load(), GAME).events if e.period_type == "OT"]
    assert events
    assert all(3600 <= (e.game_second or 0) <= 3900 for e in events)


def test_the_attack_direction_follows_the_side_that_the_home_team_defends() -> None:
    payload = load()
    shot = next(p for p in payload["plays"] if p["typeDescKey"] == "shot-on-goal")
    owner = shot["details"]["eventOwnerTeamId"]
    home_id = payload["homeTeam"]["id"]
    shot["homeTeamDefendingSide"] = "left"
    left = next(e for e in parse_pbp(payload, GAME).events if e.event_id == shot["eventId"])
    shot["homeTeamDefendingSide"] = "right"
    right = next(e for e in parse_pbp(payload, GAME).events if e.event_id == shot["eventId"])
    # The home team defends the left side, so it attacks toward positive x.
    assert left.attack_x_sign == (1 if owner == home_id else -1)
    assert right.attack_x_sign == -left.attack_x_sign


def test_an_event_with_no_defending_side_has_no_direction() -> None:
    payload = load()
    shot = next(p for p in payload["plays"] if p["typeDescKey"] == "shot-on-goal")
    del shot["homeTeamDefendingSide"]
    found = next(e for e in parse_pbp(payload, GAME).events if e.event_id == shot["eventId"])
    assert found.attack_x_sign is None


def test_an_event_with_no_owner_has_no_direction() -> None:
    events = parse_pbp(load(), GAME).events
    stoppage = next(e for e in events if e.type == "stoppage" and e.owner_team is None)
    assert stoppage.attack_x_sign is None


def test_a_bad_defending_side_is_a_named_error() -> None:
    payload = load()
    shot = next(p for p in payload["plays"] if p["typeDescKey"] == "shot-on-goal")
    shot["homeTeamDefendingSide"] = "up"
    with pytest.raises(SourceParseError, match="left or right"):
        parse_pbp(payload, GAME)


@pytest.mark.parametrize("key", ["id", "homeTeam", "awayTeam", "plays", "rosterSpots"])
def test_a_renamed_top_level_key_is_a_named_error(key: str) -> None:
    payload = load()
    payload[f"{key}_renamed"] = payload.pop(key)
    with pytest.raises(SourceParseError, match=key):
        parse_pbp(payload, GAME)


@pytest.mark.parametrize("key", ["eventId", "periodDescriptor", "timeInPeriod", "typeDescKey"])
def test_a_renamed_play_key_is_a_named_error(key: str) -> None:
    payload = load()
    payload["plays"][3][f"{key}_renamed"] = payload["plays"][3].pop(key)
    with pytest.raises(SourceParseError, match=key):
        parse_pbp(payload, GAME)


@pytest.mark.parametrize("value", ["7:5", "07:60", "x", "٠٠:٠٠", 5, None])
def test_a_bad_clock_is_a_named_error(value: object) -> None:
    payload = load()
    payload["plays"][3]["timeInPeriod"] = value
    with pytest.raises(SourceParseError, match="MM:SS"):
        parse_pbp(payload, GAME)


def test_clock_seconds_reads_minutes_and_seconds() -> None:
    assert clock_seconds("19:59", "x") == 1199
    assert clock_seconds("0:05", "x") == 5


@pytest.mark.parametrize("period_type", ["PLAYOFF", "", None])
def test_an_unknown_period_type_is_a_named_error(period_type: object) -> None:
    payload = load()
    payload["plays"][3]["periodDescriptor"]["periodType"] = period_type
    with pytest.raises(SourceParseError, match="period"):
        parse_pbp(payload, GAME)


def test_a_period_below_one_is_a_named_error() -> None:
    payload = load()
    payload["plays"][3]["periodDescriptor"]["number"] = 0
    with pytest.raises(SourceParseError, match="not a known period"):
        parse_pbp(payload, GAME)


@pytest.mark.parametrize("code", ["155", "15511", "abcd", 1551])
def test_a_bad_situation_code_is_a_named_error(code: object) -> None:
    payload = load()
    payload["plays"][3]["situationCode"] = code
    with pytest.raises(SourceParseError, match="situationCode"):
        parse_pbp(payload, GAME)


def test_an_unknown_team_code_is_a_named_error() -> None:
    payload = load()
    payload["homeTeam"]["abbrev"] = "XXX"
    with pytest.raises(SourceShapeError):
        parse_pbp(payload, GAME)


def test_an_unknown_owner_team_id_is_a_named_error() -> None:
    payload = load()
    shot = next(p for p in payload["plays"] if p["typeDescKey"] == "shot-on-goal")
    shot["details"]["eventOwnerTeamId"] = 999
    with pytest.raises(SourceParseError, match="unknown team id 999"):
        parse_pbp(payload, GAME)


def test_a_roster_spot_for_an_unknown_team_is_a_named_error() -> None:
    payload = load()
    payload["rosterSpots"][0]["teamId"] = 999
    with pytest.raises(SourceParseError, match="unknown team id 999"):
        parse_pbp(payload, GAME)


def test_an_event_listed_twice_is_a_named_error() -> None:
    payload = load()
    payload["plays"].append(copy.deepcopy(payload["plays"][0]))
    with pytest.raises(SourceParseError, match="lists event .* twice"):
        parse_pbp(payload, GAME)


def test_both_teams_must_be_different() -> None:
    payload = load()
    payload["awayTeam"] = dict(payload["homeTeam"])
    with pytest.raises(SourceParseError, match="both teams"):
        parse_pbp(payload, GAME)


def test_a_coordinate_that_is_not_a_number_is_a_named_error() -> None:
    payload = load()
    shot = next(p for p in payload["plays"] if p["typeDescKey"] == "shot-on-goal")
    shot["details"]["xCoord"] = "far"
    with pytest.raises(SourceParseError, match="xCoord"):
        parse_pbp(payload, GAME)


def test_the_parser_does_not_change_its_input() -> None:
    payload = load()
    before = copy.deepcopy(payload)
    parse_pbp(payload, GAME)
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


def test_a_payload_that_names_another_game_is_a_named_error() -> None:
    with pytest.raises(SourceParseError, match=f"names game {GAME}"):
        parse_pbp(load(), GAME + 1)


def test_a_feed_with_no_play_is_a_named_error() -> None:
    """A game that has not started lists no play. A later read can fix the refusal."""
    payload = load()
    payload["plays"] = []
    with pytest.raises(SourceParseError, match="lists no play"):
        parse_pbp(payload, GAME)


def test_a_feed_with_no_roster_is_a_named_error() -> None:
    payload = load()
    payload["rosterSpots"] = []
    with pytest.raises(SourceParseError, match="lists no roster spot"):
        parse_pbp(payload, GAME)


def test_a_player_listed_twice_in_the_roster_is_a_named_error() -> None:
    payload = load()
    payload["rosterSpots"].append(dict(payload["rosterSpots"][0]))
    with pytest.raises(SourceParseError, match="a player twice in its roster"):
        parse_pbp(payload, GAME)


def test_two_teams_with_one_code_are_a_named_error() -> None:
    payload = load()
    payload["awayTeam"]["abbrev"] = payload["homeTeam"]["abbrev"]
    with pytest.raises(SourceParseError, match="both teams"):
        parse_pbp(payload, GAME)


@pytest.mark.parametrize("value", ["20:01", "99:59", "100:00"])
def test_a_period_clock_past_the_period_is_a_named_error(value: str) -> None:
    payload = load()
    payload["plays"][3]["timeInPeriod"] = value
    with pytest.raises(SourceParseError, match="past the limit"):
        parse_pbp(payload, GAME)


def test_a_total_may_pass_one_period_when_the_caller_names_the_limit() -> None:
    assert clock_seconds("25:00", "x", limit=6000) == 1500


@pytest.mark.parametrize("value", [-1, 2**63, 10**30])
def test_an_id_outside_the_store_range_is_a_named_error(value: int) -> None:
    payload = load()
    payload["plays"][3]["eventId"] = value
    with pytest.raises(SourceParseError, match="out of range"):
        parse_pbp(payload, GAME)


def test_the_largest_id_that_the_store_holds_is_accepted() -> None:
    payload = load()
    payload["plays"][3]["eventId"] = 2**63 - 1
    assert any(e.event_id == 2**63 - 1 for e in parse_pbp(payload, GAME).events)


@pytest.mark.parametrize("period", [0, 21, 10**20])
def test_a_period_outside_the_range_is_a_named_error(period: int) -> None:
    payload = load()
    payload["plays"][3]["periodDescriptor"]["number"] = period
    with pytest.raises(SourceParseError, match="period|out of range"):
        parse_pbp(payload, GAME)


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity"])
def test_a_coordinate_that_is_not_finite_is_a_named_error(text: str) -> None:
    """json.loads accepts NaN and Infinity, and SQLite would store NaN as NULL."""
    raw = FIXTURE.read_text().replace('"xCoord":71', f'"xCoord":{text}', 1)
    payload = json.loads(raw)
    with pytest.raises(SourceParseError, match="xCoord"):
        parse_pbp(payload, GAME)


def test_a_play_with_null_details_has_no_details() -> None:
    payload = load()
    stoppage = next(p for p in payload["plays"] if p["typeDescKey"] == "stoppage")
    stoppage["details"] = None
    found = next(e for e in parse_pbp(payload, GAME).events if e.event_id == stoppage["eventId"])
    assert (found.owner_team, found.players) == (None, (None, None, None))


def test_an_event_type_with_no_player_keys_in_the_map_keeps_its_type_and_no_players() -> None:
    """The raw payload stays in the archive, so a later parser can read the players."""
    payload = load()
    shot = next(p for p in payload["plays"] if p["typeDescKey"] == "shot-on-goal")
    shot["typeDescKey"] = "failed-shot-attempt"
    found = next(e for e in parse_pbp(payload, GAME).events if e.event_id == shot["eventId"])
    assert (found.type, found.players) == ("failed-shot-attempt", (None, None, None))


def test_a_period_at_the_limit_is_read_and_one_past_it_is_a_named_error() -> None:
    payload = load()
    descriptor = payload["plays"][3]["periodDescriptor"]
    descriptor["number"] = MAX_PERIOD
    descriptor["periodType"] = "OT"
    event = parse_pbp(payload, GAME).events[3]
    assert event.period == MAX_PERIOD
    assert event.game_second == game_second(MAX_PERIOD, event.period_second)
    descriptor["number"] = MAX_PERIOD + 1
    with pytest.raises(SourceParseError, match="not a known period"):
        parse_pbp(payload, GAME)


def test_each_event_keeps_the_coordinates_and_the_goalie_of_its_play() -> None:
    payload = load()
    details = {p["eventId"]: p.get("details", {}) for p in payload["plays"]}
    events = parse_pbp(payload, GAME).events
    assert any(e.y is not None for e in events)
    assert any(e.goalie_in_net is not None for e in events)
    for event in events:
        raw = details[event.event_id]
        assert (event.x, event.y) == (raw.get("xCoord"), raw.get("yCoord"))
        assert event.goalie_in_net == raw.get("goalieInNetId")


def test_each_event_keeps_the_situation_code_of_its_play() -> None:
    payload = load()
    codes = {p["eventId"]: p.get("situationCode") for p in payload["plays"]}
    events = parse_pbp(payload, GAME).events
    assert any(e.situation_code is not None for e in events)
    assert all(e.situation_code == codes[e.event_id] for e in events)


def test_an_offensive_zone_event_is_at_the_end_of_the_rink_that_its_team_attacks() -> None:
    events = parse_pbp(load(), GAME).events
    zone = [
        (e.x, e.attack_x_sign)
        for e in events
        if e.zone == "O" and e.x and e.attack_x_sign is not None
    ]
    assert len(zone) > 20
    assert {sign for _, sign in zone} == {-1, 1}
    assert all(x * sign > 0 for x, sign in zone)


def test_a_fetch_of_a_body_that_is_not_json_names_the_source() -> None:
    with pytest.raises(SourceParseError, match="^read nhl_pbp data: "):
        fetch_pbp(_Client(b"not json"), GAME)


def test_the_game_state_of_the_feed_is_kept() -> None:
    assert parse_pbp(load(), GAME).game_state == "OFF"


@pytest.mark.parametrize("value", [None, "", 3, []])
def test_a_game_state_that_is_not_text_is_a_named_error(value: object) -> None:
    payload = load()
    payload["gameState"] = value
    with pytest.raises(SourceParseError, match="gameState"):
        parse_pbp(payload, GAME)


def test_a_feed_with_no_game_state_is_a_named_error() -> None:
    payload = load()
    del payload["gameState"]
    with pytest.raises(SourceParseError, match="gameState"):
        parse_pbp(payload, GAME)


@pytest.mark.parametrize("value", [0, "", [], False])
def test_a_details_value_of_the_wrong_type_is_a_named_error_even_when_it_is_falsy(
    value: object,
) -> None:
    payload = load()
    payload["plays"][3]["details"] = value
    with pytest.raises(SourceParseError, match="details"):
        parse_pbp(payload, GAME)


def test_a_play_with_null_or_no_details_has_no_details() -> None:
    payload = load()
    payload["plays"][3]["details"] = None
    del payload["plays"][4]["details"]
    events = parse_pbp(payload, GAME).events
    assert (events[3].x, events[3].goalie_in_net) == (None, None)
    assert (events[4].x, events[4].goalie_in_net) == (None, None)
