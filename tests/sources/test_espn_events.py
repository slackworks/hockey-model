"""The ESPN scoreboard events that find the ESPN event of an NHL game (#35)."""

import copy
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hockey_model.sources.espn_events import (
    EspnEvent,
    fetch_scoreboard_events,
    parse_scoreboard_events,
    scoreboard_url,
)
from hockey_model.sources.http import HttpClient
from hockey_model.sources.nhl import GameRow
from hockey_model.sources.shape_errors import SourceParseError, SourceShapeError
from hockey_model.sources.teams import from_espn
from hockey_model.store.db import StoreError
from hockey_model.store.espn_match import match_event

FIXTURES = Path(__file__).parent.parent / "fixtures" / "espn"
START = datetime(2026, 1, 10, 1, 0, tzinfo=UTC)


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


def test_an_event_with_an_unknown_team_is_skipped_and_named() -> None:
    board = _board()
    extra = copy.deepcopy(board["events"][0])
    extra["id"] = "999"
    extra["competitions"][0]["competitors"][0]["team"]["abbreviation"] = "ASG"
    board["events"].append(extra)
    kept = [event["id"] for event in board["events"][:-1]]
    notes: list[str] = []
    events = parse_scoreboard_events(board, notes)
    assert [event.event_id for event in events] == kept
    assert len(notes) == 1 and "ASG" in notes[0]


TEAMS = st.sampled_from(["TOR", "MTL", "BOS", "NYR", "LA", "TB", "EDM", "VGK"])
DATES = st.datetimes(
    min_value=datetime(2015, 1, 1), max_value=datetime(2030, 1, 1), timezones=st.just(UTC)
)


@st.composite
def _pages(draw: st.DrawFn) -> list[tuple[str, datetime, str, str, bool]]:
    ids = draw(st.lists(st.integers(1, 10**12 - 1), unique=True, max_size=6))
    return [(str(i), draw(DATES), draw(TEAMS), draw(TEAMS), draw(st.booleans())) for i in ids]


def _page(rows: list[tuple[str, datetime, str, str, bool]]) -> dict:
    events = []
    for event_id, start, home, away, home_first in rows:
        sides = [("home", home), ("away", away)]
        if not home_first:
            sides.reverse()
        events.append(
            {
                "id": event_id,
                "date": start.strftime("%Y-%m-%dT%H:%MZ"),
                "competitions": [
                    {
                        "competitors": [
                            {"homeAway": s, "team": {"abbreviation": c}} for s, c in sides
                        ]
                    }
                ],
            }
        )
    return {"events": events}


@given(_pages())
def test_a_valid_page_keeps_the_order_ids_starts_and_sides(
    rows: list[tuple[str, datetime, str, str, bool]],
) -> None:
    parsed = parse_scoreboard_events(_page(rows))
    assert [e.event_id for e in parsed] == [r[0] for r in rows]
    assert [e.start for e in parsed] == [r[1].replace(second=0, microsecond=0) for r in rows]
    assert [(e.home, e.away) for e in parsed] == [(from_espn(r[2]), from_espn(r[3])) for r in rows]


JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=8),
    lambda inner: (
        st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=8), inner, max_size=3)
    ),
    max_leaves=12,
)


@given(JSON)
def test_arbitrary_json_gives_events_or_a_named_error(payload: object) -> None:
    try:
        parse_scoreboard_events(payload)
    except SourceParseError, SourceShapeError:
        pass


@given(_pages(), st.data())
def test_the_page_order_does_not_change_which_event_a_game_matches(
    rows: list[tuple[str, datetime, str, str, bool]], data: st.DataObject
) -> None:
    events = parse_scoreboard_events(_page(rows))
    shuffled = data.draw(st.permutations(events))
    game = GameRow(1, 20252026, 2, START, "TOR", "MTL", "OFF", 1, 0, "REG")
    first = _outcome(game, events)
    assert _outcome(game, shuffled) == first


def _outcome(game: GameRow, events: list[EspnEvent]) -> str | None:
    try:
        found = match_event(game, events)
    except StoreError:
        return "ambiguous"
    return None if found is None else found.event_id


class _Page(HttpClient):
    """A client that answers every request with one body, and keeps the requests."""

    def __init__(self, body: dict) -> None:
        super().__init__()
        self.body = json.dumps(body).encode()
        self.requests: list[tuple[str, str]] = []

    def get(self, source: str, url: str) -> bytes:
        self.requests.append((source, url))
        return self.body


def test_a_fetch_makes_one_request_for_the_day_and_returns_its_events() -> None:
    client = _Page(_board())
    events = fetch_scoreboard_events(client, date(2026, 11, 3))
    assert client.requests == [("espn", scoreboard_url(date(2026, 11, 3)))]
    assert [event.event_id for event in events] == [e["id"] for e in _board()["events"]]


def test_a_fetch_passes_the_notes_on_and_collects_the_skipped_events() -> None:
    board = _board()
    board["events"][0]["competitions"][0]["competitors"][0]["team"]["abbreviation"] = "ASG"
    notes: list[str] = []
    events = fetch_scoreboard_events(_Page(board), date(2026, 11, 3), notes)
    assert len(events) == len(board["events"]) - 1
    assert len(notes) == 1 and "ASG" in notes[0]
    assert fetch_scoreboard_events(_Page(board), date(2026, 11, 3)) == events


@pytest.mark.parametrize(
    ("event_id", "valid"),
    [("1", True), ("123456789012", True), ("1234567890123", False), ("", False), ("12a", False)],
)
def test_an_event_id_is_one_to_twelve_ascii_digits(event_id: str, valid: bool) -> None:
    board = _board()
    board["events"] = board["events"][:1]
    board["events"][0]["id"] = event_id
    if valid:
        assert parse_scoreboard_events(board)[0].event_id == event_id
    else:
        with pytest.raises(SourceParseError, match="event id"):
            parse_scoreboard_events(board)


def test_the_message_of_each_fault_names_the_event_and_the_place() -> None:
    board = _board()
    first = board["events"][0]["id"]
    board["events"][0]["competitions"][0]["competitors"][0]["homeAway"] = "x"
    with pytest.raises(SourceParseError, match=rf"event {first} has homeAway 'x'"):
        parse_scoreboard_events(board)
    board = _board()
    board["events"][0]["competitions"][0]["competitors"].pop()
    with pytest.raises(SourceParseError, match=rf"event {first} must list two competitors, got 1"):
        parse_scoreboard_events(board)
    board = _board()
    board["events"][0]["competitions"] = []
    with pytest.raises(SourceParseError, match=rf"event {first} has an empty 'competitions'"):
        parse_scoreboard_events(board)
