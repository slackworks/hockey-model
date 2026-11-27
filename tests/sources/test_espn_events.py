"""The ESPN scoreboard events that find the ESPN event of an NHL game (#35)."""

import copy
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from hockey_model.sources.espn_events import (
    EspnEvent,
    parse_scoreboard_events,
    scoreboard_url,
)
from hockey_model.sources.shape_errors import SourceParseError

FIXTURES = Path(__file__).parent.parent / "fixtures" / "espn"


def _board() -> dict:
    return json.loads((FIXTURES / "scoreboard_20261103.json").read_text())


def test_an_event_has_its_id_start_and_teams() -> None:
    events = parse_scoreboard_events(
        json.loads((FIXTURES / "scoreboard_20261104.json").read_text())
    )
    assert events
    first = events[0]
    assert first.event_id.isdigit()
    assert first.start.tzinfo is not None
    assert first.home != first.away


def test_the_teams_are_nhl_codes_with_the_home_team_marked() -> None:
    board = _board()
    event = board["events"][0]
    for competitor in event["competitions"][0]["competitors"]:
        team = competitor["team"]
        team["abbreviation"] = "LA" if competitor["homeAway"] == "home" else "TB"
    parsed = parse_scoreboard_events(board)
    assert (parsed[0].home, parsed[0].away) == ("LAK", "TBL")
    assert parsed[0].start == datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
    assert isinstance(parsed[0], EspnEvent)


def test_a_day_with_no_game_gives_no_event() -> None:
    board = _board()
    board["events"] = []
    assert parse_scoreboard_events(board) == []


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda b: b.pop("events"), "events"),
        (lambda b: b["events"][0].update(id="12x"), "event id"),
        (lambda b: b["events"][0].pop("date"), "date"),
        (lambda b: b["events"][0]["competitions"][0]["competitors"].pop(), "two competitors"),
        (
            lambda b: b["events"][0]["competitions"][0]["competitors"][0].update(homeAway="x"),
            "homeAway",
        ),
        (
            lambda b: b["events"][0]["competitions"][0]["competitors"][1].update(
                homeAway=b["events"][0]["competitions"][0]["competitors"][0]["homeAway"]
            ),
            "one home and one away",
        ),
        (lambda b: b["events"].append(copy.deepcopy(b["events"][0])), "twice"),
    ],
)
def test_a_malformed_scoreboard_is_refused_with_a_named_reason(edit, message: str) -> None:
    board = _board()
    edit(board)
    with pytest.raises(SourceParseError, match=message):
        parse_scoreboard_events(board)


def test_the_url_names_the_day() -> None:
    assert scoreboard_url(date(2026, 1, 10)).endswith("/scoreboard?dates=20260110")
