"""The match of an NHL game to its ESPN event (#35)."""

from datetime import UTC, datetime, timedelta

import pytest

from hockey_model.sources.espn_events import EspnEvent
from hockey_model.sources.nhl import GameRow
from hockey_model.store.db import StoreError
from hockey_model.store.espn_match import match_event
from hockey_model.store.starter_writes import START_TOLERANCE

START = datetime(2026, 1, 10, 0, 0, tzinfo=UTC)


def _game(home: str = "EDM", away: str = "LAK") -> GameRow:
    return GameRow(2025020709, 20252026, 2, START, home, away, "OFF", 3, 4, "SO")


def _event(event_id: str, home: str, away: str, shift: timedelta = timedelta()) -> EspnEvent:
    return EspnEvent(event_id, START + shift, home, away)


def test_the_event_with_the_same_teams_and_start_is_the_match() -> None:
    events = [_event("1", "TOR", "MTL"), _event("2", "EDM", "LAK")]
    found = match_event(_game(), events)
    assert found is not None
    assert found.event_id == "2"


def test_a_start_inside_the_tolerance_matches_and_one_outside_does_not() -> None:
    inside = _event("1", "EDM", "LAK", START_TOLERANCE)
    outside = _event("1", "EDM", "LAK", START_TOLERANCE + timedelta(seconds=1))
    assert match_event(_game(), [inside]) == inside
    assert match_event(_game(), [outside]) is None


def test_a_reversed_pair_is_another_game() -> None:
    assert match_event(_game(), [_event("1", "LAK", "EDM")]) is None


def test_no_event_gives_no_match() -> None:
    assert match_event(_game(), []) is None


def test_two_events_that_fit_are_an_error_that_names_them() -> None:
    events = [_event("1", "EDM", "LAK"), _event("2", "EDM", "LAK", timedelta(hours=1))]
    with pytest.raises(StoreError, match=r"game 2025020709: 2 ESPN events fit \(1, 2\)"):
        match_event(_game(), events)
