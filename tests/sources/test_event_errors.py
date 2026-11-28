"""Pin the error messages of the event parsers (#35).

The sweep puts each fault at each distinct field path of a saved game. It collects the set of
messages that the parser raises, and the set must equal ``fixtures/golden/event_errors.json``. A
message names the source and the place of the fault, so a changed label changes the set.
Run ``UPDATE_GOLDEN=1 uv run pytest tests/sources/test_event_errors.py`` to write the file, and
read the diff before a commit.
"""

import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from json_mutation import sweep

from hockey_model.sources.espn_events import parse_scoreboard_events
from hockey_model.sources.espn_summary import parse_summary
from hockey_model.sources.nhl_events import parse_pbp
from hockey_model.sources.nhl_shifts import parse_shifts
from hockey_model.sources.shape_errors import SourceParseError, SourceShapeError

FIXTURES = Path(__file__).parent.parent / "fixtures"
GOLDEN = FIXTURES / "golden" / "event_errors.json"
GAME = 2025020709


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _first_of_each(rows: list[dict], key: str) -> list[dict]:
    """Keep the first row of each kind, so the sweep reads a small game with every shape."""
    seen: dict[object, dict] = {}
    for row in rows:
        seen.setdefault(row.get(key), row)
    return list(seen.values())


def _pbp() -> dict:
    document = _load("nhl/pbp_2025020709.json")
    document["plays"] = _first_of_each(document["plays"], "typeDescKey")
    return document


def _shifts() -> dict:
    document = _load("nhl/shifts_2025020709.json")
    rows = document["data"]
    shifts = [row for row in rows if row["typeCode"] == 517][:6]
    document["data"] = shifts + _first_of_each(rows, "typeCode")
    return document


def _espn() -> dict:
    return _load("espn/summary_401803060.json")


def _scoreboard() -> dict:
    document = _load("espn/scoreboard_20261103.json")
    document["events"] = document["events"][:2]
    return document


CASES: dict[str, tuple[Callable[[], dict], Callable[[object], object]]] = {
    "pbp": (_pbp, lambda doc: parse_pbp(doc, GAME)),
    "shifts": (_shifts, lambda doc: parse_shifts(doc, GAME)),
    "espn": (_espn, lambda doc: parse_summary(doc, "401803060")),
    "scoreboard": (_scoreboard, parse_scoreboard_events),
}


def _shot(document: dict) -> dict:
    return next(p for p in document["plays"] if p["typeDescKey"] == "shot-on-goal")


def _edit_clock(document: dict) -> None:
    document["plays"][0]["timeInPeriod"] = "21:00"


def _edit_same_team(document: dict) -> None:
    document["awayTeam"] = dict(document["homeTeam"])


def _edit_roster_team(document: dict) -> None:
    document["rosterSpots"][0]["teamId"] = 999


def _edit_roster_twice(document: dict) -> None:
    document["rosterSpots"].append(dict(document["rosterSpots"][0]))


def _edit_side(document: dict) -> None:
    _shot(document)["homeTeamDefendingSide"] = "up"


def _edit_owner(document: dict) -> None:
    _shot(document)["details"]["eventOwnerTeamId"] = 999


def _edit_game(document: dict) -> None:
    document["id"] += 1


def _edit_play_twice(document: dict) -> None:
    document["plays"].append(dict(document["plays"][0]))


def _edit_skater_twice(document: dict) -> None:
    group = document["boxscore"]["players"][0]["statistics"][0]
    group["athletes"].append(dict(group["athletes"][0]))


def _edit_event_twice(document: dict) -> None:
    document["events"].append(dict(document["events"][0]))


def _edit_no_games(document: dict) -> None:
    document["events"][0]["competitions"] = []


def _edit_one_competitor(document: dict) -> None:
    document["events"][0]["competitions"][0]["competitors"].pop()


def _edit_same_side(document: dict) -> None:
    competitors = document["events"][0]["competitions"][0]["competitors"]
    competitors[1]["homeAway"] = competitors[0]["homeAway"]


def _edit_event_id(document: dict) -> None:
    document["events"][0]["id"] = "1234567890123"


SCENARIOS: dict[str, list[Callable[[dict], None]]] = {
    "pbp": [
        _edit_clock,
        _edit_same_team,
        _edit_roster_team,
        _edit_roster_twice,
        _edit_side,
        _edit_owner,
        _edit_game,
        _edit_play_twice,
    ],
    "shifts": [],
    "espn": [_edit_skater_twice],
    "scoreboard": [
        _edit_event_twice,
        _edit_no_games,
        _edit_one_competitor,
        _edit_same_side,
        _edit_event_id,
    ],
}


def _documents(name: str) -> Iterator[object]:
    build, _ = CASES[name]
    yield from sweep(build())
    for edit in SCENARIOS[name]:
        document = build()
        edit(document)
        yield document


def _messages(name: str) -> list[str]:
    _, parse = CASES[name]
    found: set[str] = set()
    for document in _documents(name):
        try:
            parse(document)
        except (SourceParseError, SourceShapeError) as exc:
            found.add(f"{type(exc).__name__}: {exc}")
    return sorted(found)


def test_the_saved_games_parse_without_a_fault() -> None:
    for build, parse in CASES.values():
        assert parse(build())


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_messages_of_the_parser_are_the_pinned_set(name: str) -> None:
    messages = _messages(name)
    if os.environ.get("UPDATE_GOLDEN"):
        golden = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
        golden[name] = messages
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(golden, indent=2, sort_keys=True) + "\n")
    assert messages == json.loads(GOLDEN.read_text())[name]
