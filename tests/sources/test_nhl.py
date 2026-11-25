"""Tests for the NHL API parsers. They read fixtures and make no network request."""

import copy
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from hockey_model.sources.http import HttpClient
from hockey_model.sources.nhl import (
    fetch_boxscore,
    fetch_score_day,
    parse_boxscore,
    parse_score_day,
)
from hockey_model.sources.payload import get_json_text
from hockey_model.sources.shape_errors import SourceParseError, SourceShapeError

FIXTURES = Path(__file__).parent.parent / "fixtures" / "nhl"
FINAL_DAY = "score_2026-01-10.json"
FUTURE_DAY = "score_2026-11-05.json"
BOX_ID = 2025020709


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def game_with_id(payload: dict, game_id: int) -> dict:
    return next(game for game in payload["games"] if game["id"] == game_id)


def test_parse_score_day_reads_final_scores() -> None:
    rows = {row.game_id: row for row in parse_score_day(load(FINAL_DAY))}
    row = rows[2025020697]
    assert (row.away, row.away_score, row.home, row.home_score) == ("NYR", 2, "BOS", 10)
    assert row.is_final
    assert row.season == 20252026
    assert row.game_type == 2
    assert row.start == datetime(2026, 1, 10, 18, 0, tzinfo=UTC)
    assert row.last_period == "REG"


def test_parse_score_day_marks_ot_and_so() -> None:
    rows = {row.game_id: row for row in parse_score_day(load(FINAL_DAY))}
    assert rows[2025020700].last_period == "OT"
    assert rows[2025020709].last_period == "SO"
    assert {row.last_period for row in rows.values()} == {"REG", "OT", "SO"}


def test_parse_score_day_future_game_is_not_final() -> None:
    rows = parse_score_day(load(FUTURE_DAY))
    assert rows
    assert all(not row.is_final for row in rows)
    assert all(row.home_score is None and row.away_score is None for row in rows)
    assert all(row.last_period is None for row in rows)


def test_parse_score_day_live_game_keeps_no_score() -> None:
    payload = load(FINAL_DAY)
    game = game_with_id(payload, 2025020697)
    game["gameState"] = "LIVE"
    row = next(r for r in parse_score_day(payload) if r.game_id == 2025020697)
    assert not row.is_final
    assert (row.home_score, row.away_score, row.last_period) == (None, None, None)


@pytest.mark.parametrize("game_type", [1, 4, 9, 19])
def test_parse_score_day_drops_other_game_types(game_type: int) -> None:
    payload = load(FINAL_DAY)
    game_with_id(payload, 2025020697)["gameType"] = game_type
    ids = {row.game_id for row in parse_score_day(payload)}
    assert 2025020697 not in ids
    assert 2025020698 in ids


def test_parse_score_day_keeps_playoff_games() -> None:
    payload = load(FINAL_DAY)
    game_with_id(payload, 2025020697)["gameType"] = 3
    assert 2025020697 in {row.game_id for row in parse_score_day(payload)}


def test_parse_score_day_rejects_missing_id() -> None:
    payload = load(FINAL_DAY)
    del payload["games"][0]["id"]
    with pytest.raises(SourceParseError, match="'id'"):
        parse_score_day(payload)


def test_parse_score_day_rejects_unknown_team_code() -> None:
    payload = load(FINAL_DAY)
    payload["games"][0]["homeTeam"]["abbrev"] = "XXX"
    with pytest.raises(SourceShapeError, match="XXX"):
        parse_score_day(payload)


def test_parse_score_day_rejects_final_game_without_score() -> None:
    payload = load(FINAL_DAY)
    del payload["games"][0]["homeTeam"]["score"]
    with pytest.raises(SourceParseError, match="'score'"):
        parse_score_day(payload)


def test_parse_score_day_reads_unknown_last_period_with_a_note() -> None:
    """A new period type is stored as written and named, so it never fails the whole day."""
    payload = load(FINAL_DAY)
    payload["games"][0]["gameOutcome"]["lastPeriodType"] = "XO"
    notes: list[str] = []
    rows = parse_score_day(payload, notes)
    assert len(notes) == 1
    assert "XO" in notes[0]
    assert all(isinstance(row.last_period, str | None) for row in rows)


def test_a_known_last_period_adds_no_note() -> None:
    notes: list[str] = []
    parse_score_day(load(FINAL_DAY), notes)
    assert notes == []


def test_parse_score_day_rejects_bool_as_id() -> None:
    payload = load(FINAL_DAY)
    payload["games"][0]["id"] = True
    with pytest.raises(SourceParseError, match="integer"):
        parse_score_day(payload)


@pytest.mark.parametrize("payload", [[], "x", {"games": {}}, {}])
def test_parse_score_day_rejects_wrong_top_shape(payload: object) -> None:
    with pytest.raises(SourceParseError):
        parse_score_day(payload)


def test_parse_score_day_rejects_naive_start() -> None:
    payload = load(FINAL_DAY)
    payload["games"][0]["startTimeUTC"] = "2026-01-10T18:00:00"
    with pytest.raises(SourceParseError, match="timezone"):
        parse_score_day(payload)


def test_parse_boxscore_one_starter_per_team() -> None:
    rows = parse_boxscore(load(f"boxscore_{BOX_ID}.json"))
    assert rows.game_id == BOX_ID
    for team in {g.team for g in rows.goalies}:
        assert sum(g.started for g in rows.goalies if g.team == team) == 1
    assert {g.team for g in rows.goalies} == {"EDM", "LAK"}


def test_parse_boxscore_toi_seconds() -> None:
    rows = parse_boxscore(load(f"boxscore_{BOX_ID}.json"))
    frederic = next(s for s in rows.skaters if s.nhl_id == 8479365)
    assert frederic.toi_seconds == 9 * 60 + 19
    ingram = next(g for g in rows.goalies if g.nhl_id == 8478971)
    assert ingram.toi_seconds == 65 * 60


def test_parse_boxscore_names_every_player() -> None:
    rows = parse_boxscore(load(f"boxscore_{BOX_ID}.json"))
    assert len(rows.players) == len(rows.skaters) + len(rows.goalies)
    by_id = {p.nhl_id: p for p in rows.players}
    assert by_id[8478971].position == "G"
    assert by_id[8479365].full_name == "T. Frederic"


@pytest.mark.parametrize("toi", ["1830", "18:3", "ab:cd", "18:60", "18:75", "١٨:٣٠", None, 1110])
def test_parse_boxscore_rejects_bad_toi(toi: object) -> None:
    payload = load(f"boxscore_{BOX_ID}.json")
    payload["playerByGameStats"]["homeTeam"]["forwards"][0]["toi"] = toi
    with pytest.raises(SourceParseError, match="MM:SS"):
        parse_boxscore(payload)


def test_parse_boxscore_rejects_non_final_game() -> None:
    payload = load(f"boxscore_{BOX_ID}.json")
    payload["gameState"] = "LIVE"
    with pytest.raises(SourceParseError, match="not final"):
        parse_boxscore(payload)


def test_parse_boxscore_rejects_non_bool_starter() -> None:
    payload = load(f"boxscore_{BOX_ID}.json")
    payload["playerByGameStats"]["homeTeam"]["goalies"][0]["starter"] = "yes"
    with pytest.raises(SourceParseError, match="starter"):
        parse_boxscore(payload)


class FakeClient(HttpClient):
    """Serve scripted bodies by URL."""

    def __init__(self, bodies: dict[str, bytes]) -> None:
        super().__init__()
        self.bodies = bodies
        self.urls: list[str] = []

    def get(self, source: str, url: str) -> bytes:
        self.urls.append(url)
        return self.bodies[url]


def test_fetch_score_day_uses_dated_url() -> None:
    url = "https://api-web.nhle.com/v1/score/2026-01-10"
    client = FakeClient({url: (FIXTURES / FINAL_DAY).read_bytes()})
    rows = fetch_score_day(client, date(2026, 1, 10))
    assert client.urls == [url]
    assert len(rows) == 14


def test_fetch_boxscore_checks_game_id() -> None:
    url = "https://api-web.nhle.com/v1/gamecenter/2025020710/boxscore"
    client = FakeClient({url: (FIXTURES / f"boxscore_{BOX_ID}.json").read_bytes()})
    with pytest.raises(SourceParseError, match="names game"):
        fetch_boxscore(client, 2025020710)


def test_fetch_rejects_invalid_json() -> None:
    url = "https://api-web.nhle.com/v1/score/2026-01-10"
    client = FakeClient({url: b"<html>"})
    with pytest.raises(SourceParseError, match="not valid JSON"):
        fetch_score_day(client, date(2026, 1, 10))


def test_parse_does_not_mutate_payload() -> None:
    payload = load(FINAL_DAY)
    before = copy.deepcopy(payload)
    parse_score_day(payload)
    assert payload == before


def test_deeply_nested_json_is_a_named_error() -> None:
    with pytest.raises(SourceParseError, match="not valid JSON"):
        get_json_text("nhl", "[" * 200_000, "the body")


def test_parse_boxscore_rejects_a_team_with_no_goalie() -> None:
    payload = load(f"boxscore_{BOX_ID}.json")
    payload["playerByGameStats"]["homeTeam"]["goalies"] = []
    with pytest.raises(SourceParseError, match="0 starting goalies for EDM"):
        parse_boxscore(payload)


def test_parse_boxscore_rejects_two_starters_for_one_team() -> None:
    payload = load(f"boxscore_{BOX_ID}.json")
    for goalie in payload["playerByGameStats"]["homeTeam"]["goalies"]:
        goalie["starter"] = True
    with pytest.raises(SourceParseError, match="starting goalies"):
        parse_boxscore(payload)


def _side(payload: dict, key: str) -> dict:
    return payload["playerByGameStats"][key]


def test_parse_boxscore_names_the_declared_teams() -> None:
    payload = load(f"boxscore_{BOX_ID}.json")
    rows = parse_boxscore(payload)
    assert (rows.home, rows.away) == (payload["homeTeam"]["abbrev"], payload["awayTeam"]["abbrev"])
    assert rows.home != rows.away


@pytest.mark.parametrize("key", ["homeTeam", "awayTeam"])
def test_parse_boxscore_rejects_a_side_with_no_players(key: str) -> None:
    """The team comes from the declared header, so an empty side cannot hide."""
    payload = load(f"boxscore_{BOX_ID}.json")
    team = payload[key]["abbrev"]
    _side(payload, key).update({"forwards": [], "defense": [], "goalies": []})
    with pytest.raises(SourceParseError, match=f"no skater rows for {team}"):
        parse_boxscore(payload)


@pytest.mark.parametrize("key", ["homeTeam", "awayTeam"])
def test_parse_boxscore_rejects_a_side_with_goalies_but_no_skaters(key: str) -> None:
    payload = load(f"boxscore_{BOX_ID}.json")
    team = payload[key]["abbrev"]
    _side(payload, key).update({"forwards": [], "defense": []})
    with pytest.raises(SourceParseError, match=f"no skater rows for {team}"):
        parse_boxscore(payload)


def test_parse_boxscore_rejects_both_sides_empty() -> None:
    payload = load(f"boxscore_{BOX_ID}.json")
    for key in ("homeTeam", "awayTeam"):
        _side(payload, key).update({"forwards": [], "defense": [], "goalies": []})
    with pytest.raises(SourceParseError, match="no skater rows for"):
        parse_boxscore(payload)


def test_parse_boxscore_keeps_an_unused_backup_goalie() -> None:
    payload = load(f"boxscore_{BOX_ID}.json")
    rows = parse_boxscore(payload)
    assert any(not goalie.started for goalie in rows.goalies)
    assert {goalie.team for goalie in rows.goalies} == {rows.home, rows.away}


def test_parse_boxscore_rejects_a_game_that_plays_itself() -> None:
    payload = load(f"boxscore_{BOX_ID}.json")
    payload["awayTeam"]["abbrev"] = payload["homeTeam"]["abbrev"]
    with pytest.raises(SourceParseError, match="names .* as both teams"):
        parse_boxscore(payload)
