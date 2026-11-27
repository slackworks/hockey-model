"""Tests for the ESPN summary parser. They read a saved game and make no network request."""

import copy
import json
from pathlib import Path

import pytest

from hockey_model.sources.espn_summary import (
    SUMMARY_PARSER,
    fetch_summary,
    parse_summary,
    summary_url,
)
from hockey_model.sources.http import HttpClient
from hockey_model.sources.shape_errors import SourceParseError, SourceShapeError

FIXTURE = Path(__file__).parent.parent / "fixtures" / "espn" / "summary_401803060.json"
EVENT = "401803060"


def load() -> dict:
    return json.loads(FIXTURE.read_text())


def test_the_parser_names_its_version() -> None:
    assert SUMMARY_PARSER.startswith("espn_summary:")


def test_a_saved_summary_gives_the_skaters_of_both_teams() -> None:
    rows = parse_summary(load(), EVENT)
    assert len(rows.skaters) == 36
    assert {s.team for s in rows.skaters} == {"EDM", "LAK"}


def test_a_skater_has_the_time_on_ice_split_by_strength() -> None:
    byfield = next(s for s in parse_summary(load(), EVENT).skaters if s.name == "Quinton Byfield")
    assert byfield.team == "LAK"
    assert (byfield.toi, byfield.pp_toi, byfield.sh_toi, byfield.es_toi) == (1276, 45, 175, 1056)
    assert byfield.shifts == 27
    assert byfield.toi == byfield.pp_toi + byfield.sh_toi + byfield.es_toi


def test_the_goalies_are_not_skaters() -> None:
    names = {s.name for s in parse_summary(load(), EVENT).skaters}
    payload = load()
    goalies = [
        a["athlete"]["displayName"]
        for side in payload["boxscore"]["players"]
        for group in side["statistics"]
        if group["name"] == "goalies"
        for a in group["athletes"]
    ]
    assert goalies
    assert names.isdisjoint(goalies)


def test_the_market_keys_are_in_the_saved_payload_and_the_parser_reads_none() -> None:
    payload = load()
    assert {"pickcenter", "odds", "againstTheSpread"} <= payload.keys()
    for key in ("pickcenter", "odds", "againstTheSpread"):
        del payload[key]
    assert parse_summary(payload, EVENT) == parse_summary(load(), EVENT)


@pytest.mark.parametrize("key", ["boxscore"])
def test_a_renamed_top_level_key_is_a_named_error(key: str) -> None:
    payload = load()
    payload[f"{key}_renamed"] = payload.pop(key)
    with pytest.raises(SourceParseError, match=key):
        parse_summary(payload, EVENT)


def test_a_renamed_stat_column_is_a_named_error() -> None:
    payload = load()
    group = payload["boxscore"]["players"][0]["statistics"][0]
    group["keys"][group["keys"].index("timeOnIce")] = "timeOnIce_renamed"
    with pytest.raises(SourceParseError, match="no stat column timeOnIce"):
        parse_summary(payload, EVENT)


@pytest.mark.parametrize("value", ["7", "21:61", "x", 21, None])
def test_a_bad_time_is_a_named_error(value: object) -> None:
    payload = load()
    group = payload["boxscore"]["players"][0]["statistics"][0]
    index = group["keys"].index("timeOnIce")
    group["athletes"][0]["stats"][index] = value
    with pytest.raises(SourceParseError, match="M:SS"):
        parse_summary(payload, EVENT)


def test_a_bad_shift_count_is_a_named_error() -> None:
    payload = load()
    group = payload["boxscore"]["players"][0]["statistics"][0]
    index = group["keys"].index("shifts")
    group["athletes"][0]["stats"][index] = "many"
    with pytest.raises(SourceParseError, match="digits"):
        parse_summary(payload, EVENT)


def test_a_short_stat_row_is_a_named_error() -> None:
    payload = load()
    group = payload["boxscore"]["players"][0]["statistics"][0]
    group["athletes"][0]["stats"] = group["athletes"][0]["stats"][:3]
    with pytest.raises(SourceParseError, match="no value for"):
        parse_summary(payload, EVENT)


def test_an_unknown_team_code_is_a_named_error() -> None:
    payload = load()
    payload["boxscore"]["players"][0]["team"]["abbreviation"] = "XXX"
    with pytest.raises(SourceShapeError):
        parse_summary(payload, EVENT)


def test_a_summary_with_no_skater_is_a_named_error() -> None:
    payload = load()
    for side in payload["boxscore"]["players"]:
        side["statistics"] = [g for g in side["statistics"] if g["name"] == "goalies"]
    with pytest.raises(SourceParseError, match="lists no skater"):
        parse_summary(payload, EVENT)


def test_the_parser_does_not_change_its_input() -> None:
    payload = load()
    before = copy.deepcopy(payload)
    parse_summary(payload, EVENT)
    assert payload == before


class _Client(HttpClient):
    def __init__(self, body: bytes) -> None:
        super().__init__()
        self.body = body
        self.urls: list[str] = []

    def get(self, source: str, url: str) -> bytes:
        self.urls.append(url)
        return self.body


def test_fetch_reads_the_summary_url_of_the_event() -> None:
    client = _Client(FIXTURE.read_bytes())
    assert len(fetch_summary(client, EVENT).skaters) == 36
    assert client.urls == [summary_url(EVENT)]
    assert summary_url(EVENT).endswith(f"/summary?event={EVENT}")


def test_a_summary_for_another_event_is_a_named_error() -> None:
    with pytest.raises(SourceParseError, match=f"names event {EVENT}"):
        parse_summary(load(), "401803061")


def test_a_summary_with_no_header_id_is_a_named_error() -> None:
    payload = load()
    del payload["header"]["id"]
    with pytest.raises(SourceParseError, match="id"):
        parse_summary(payload, EVENT)


def test_a_skater_listed_twice_for_one_team_is_a_named_error() -> None:
    payload = load()
    group = payload["boxscore"]["players"][0]["statistics"][0]
    group["athletes"].append(copy.deepcopy(group["athletes"][0]))
    with pytest.raises(SourceParseError, match="a skater twice"):
        parse_summary(payload, EVENT)


def test_a_summary_with_one_team_is_a_named_error() -> None:
    payload = load()
    payload["boxscore"]["players"] = payload["boxscore"]["players"][:1]
    with pytest.raises(SourceParseError, match="two teams"):
        parse_summary(payload, EVENT)


def test_a_stat_key_list_with_a_repeated_key_is_a_named_error() -> None:
    payload = load()
    group = payload["boxscore"]["players"][0]["statistics"][0]
    group["keys"][0] = group["keys"][1]
    with pytest.raises(SourceParseError, match="unique strings"):
        parse_summary(payload, EVENT)


def test_a_time_on_ice_past_the_longest_game_is_a_named_error() -> None:
    payload = load()
    group = payload["boxscore"]["players"][0]["statistics"][0]
    index = group["keys"].index("timeOnIce")
    group["athletes"][0]["stats"][index] = "121:00"
    with pytest.raises(SourceParseError, match="past the limit"):
        parse_summary(payload, EVENT)


@pytest.mark.parametrize("event", ["", "40180&x=1", "4018030601234", "٤٠١", "abc"])
def test_a_url_for_an_event_id_that_is_not_digits_is_a_named_error(event: str) -> None:
    with pytest.raises(SourceParseError, match="1 to 12 ASCII digits"):
        summary_url(event)


@pytest.mark.parametrize("name", [None, [], 3])
def test_a_stat_group_whose_name_is_not_text_is_a_named_error(name: object) -> None:
    payload = load()
    payload["boxscore"]["players"][0]["statistics"][0]["name"] = name
    with pytest.raises(SourceParseError, match="stat group"):
        parse_summary(payload, EVENT)


def test_a_stat_group_with_no_name_is_a_named_error() -> None:
    payload = load()
    del payload["boxscore"]["players"][0]["statistics"][0]["name"]
    with pytest.raises(SourceParseError, match="stat group"):
        parse_summary(payload, EVENT)


def test_a_stat_row_that_ends_just_before_a_column_is_a_named_error() -> None:
    payload = load()
    group = payload["boxscore"]["players"][0]["statistics"][0]
    last = max(group["keys"].index(k) for k in ("timeOnIce", "shifts"))
    group["athletes"][0]["stats"] = group["athletes"][0]["stats"][:last]
    with pytest.raises(SourceParseError, match="no value for"):
        parse_summary(payload, EVENT)


def test_a_group_that_is_not_skaters_does_not_hide_the_groups_after_it() -> None:
    payload = load()
    for side in payload["boxscore"]["players"]:
        side["statistics"].sort(key=lambda g: g["name"] != "goalies")
        assert side["statistics"][0]["name"] == "goalies"
    assert parse_summary(payload, EVENT) == parse_summary(load(), EVENT)


def test_a_fetch_of_a_body_that_is_not_json_names_the_source() -> None:
    with pytest.raises(SourceParseError, match="^read espn_summary data: "):
        fetch_summary(_Client(b"not json"), EVENT)


def test_an_event_id_that_is_not_digits_names_the_source() -> None:
    with pytest.raises(SourceParseError, match="^read espn_summary data: ESPN event id"):
        summary_url("abc")


def test_a_skater_has_the_espn_athlete_id() -> None:
    byfield = next(s for s in parse_summary(load(), EVENT).skaters if s.name == "Quinton Byfield")
    assert byfield.espn_id == 4697383


def test_two_skaters_with_one_name_on_one_team_are_both_kept() -> None:
    payload = load()
    group = payload["boxscore"]["players"][0]["statistics"][0]
    twin = copy.deepcopy(group["athletes"][0])
    twin["athlete"]["id"] = "999000111"
    group["athletes"].append(twin)
    skaters = parse_summary(payload, EVENT).skaters
    assert len(skaters) == 37
    assert len({s.espn_id for s in skaters}) == 37


@pytest.mark.parametrize("value", [None, 4697383, "", "x", "٤٦٩", "1" * 13, []])
def test_an_athlete_id_that_is_not_digits_is_a_named_error(value: object) -> None:
    payload = load()
    group = payload["boxscore"]["players"][0]["statistics"][0]
    group["athletes"][0]["athlete"]["id"] = value
    with pytest.raises(SourceParseError, match="athlete id"):
        parse_summary(payload, EVENT)


def test_an_athlete_with_no_id_is_a_named_error() -> None:
    payload = load()
    group = payload["boxscore"]["players"][0]["statistics"][0]
    del group["athletes"][0]["athlete"]["id"]
    with pytest.raises(SourceParseError, match="athlete"):
        parse_summary(payload, EVENT)


def test_an_athlete_id_listed_under_two_teams_is_a_named_error() -> None:
    payload = load()
    first = payload["boxscore"]["players"][0]["statistics"][0]["athletes"][0]
    other = payload["boxscore"]["players"][1]["statistics"][0]["athletes"][0]
    other["athlete"]["id"] = first["athlete"]["id"]
    with pytest.raises(SourceParseError, match="a skater twice"):
        parse_summary(payload, EVENT)
