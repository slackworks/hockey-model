"""Tests for the goalie starts a caller supplies on a game."""

import sqlite3
from datetime import timedelta

import pytest
from seeding import artifact, utc

from hockey_model.game import (
    Game,
    GoalieStart,
    MissingKind,
    StarterSource,
    StarterStatus,
    game_win_probability,
)
from hockey_model.game_model.features import GoalieMode
from hockey_model.game_model.goalies import resolve_goalie
from hockey_model.game_model.logistic import ELO_ONLY
from hockey_model.game_model.nhl_model import NhlGameModel
from hockey_model.types import HockeyModelError, Player, Slot

START = utc("2026-01-20T00:00")
BEFORE_DROP = utc("2026-01-19T16:00")
AFTER_DROP = utc("2026-01-20T02:00")
STEP = timedelta(minutes=1)
PLAYER = Player("x", None, None, "Given Goalie", frozenset({Slot.G}), "TOR")


def _game(side: str, start: GoalieStart) -> Game:
    if side == "home":
        return Game(100, "TOR", "BOS", START, home_goalie=start)
    return Game(100, "BOS", "TOR", START, away_goalie=start)


def _given(status: StarterStatus, read_at) -> GoalieStart:
    return GoalieStart(PLAYER, status, StarterSource.ESPN, read_at)


@pytest.mark.parametrize("side", ["home", "away"])
@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (StarterStatus.CONFIRMED, None),
        (StarterStatus.LIKELY, MissingKind.GOALIE_UNCONFIRMED),
        (StarterStatus.UNKNOWN, MissingKind.GOALIE_UNKNOWN),
    ],
)
def test_a_start_read_before_the_cutoff_is_used_with_its_uncertainty_note(
    store: sqlite3.Connection, side: str, status: StarterStatus, kind: MissingKind | None
) -> None:
    given = _given(status, BEFORE_DROP - STEP)
    start, missing = resolve_goalie(store, _game(side, given), "TOR", BEFORE_DROP, use_reads=True)
    assert start is given
    assert [m.kind for m in missing] == ([] if kind is None else [kind])
    assert all(m.team == "TOR" for m in missing)


@pytest.mark.parametrize("side", ["home", "away"])
@pytest.mark.parametrize("status", list(StarterStatus))
@pytest.mark.parametrize("offset", [timedelta(0), STEP])
def test_a_start_read_at_or_after_the_cutoff_is_an_error(
    store: sqlite3.Connection, side: str, status: StarterStatus, offset: timedelta
) -> None:
    given = _given(status, BEFORE_DROP + offset)
    with pytest.raises(HockeyModelError, match=rf"{side} goalie.*read at .*cutoff"):
        resolve_goalie(store, _game(side, given), "TOR", BEFORE_DROP, use_reads=True)


def test_after_puck_drop_the_cutoff_is_the_start_time(store: sqlite3.Connection) -> None:
    # A read between puck drop and as_of was not known at puck drop.
    late = _given(StarterStatus.CONFIRMED, START + STEP)
    with pytest.raises(HockeyModelError, match="cutoff"):
        resolve_goalie(store, _game("home", late), "TOR", AFTER_DROP, use_reads=True)
    equal = _given(StarterStatus.CONFIRMED, START)
    with pytest.raises(HockeyModelError, match="cutoff"):
        resolve_goalie(store, _game("home", equal), "TOR", AFTER_DROP, use_reads=True)
    early = _given(StarterStatus.CONFIRMED, START - STEP)
    start, _ = resolve_goalie(store, _game("home", early), "TOR", AFTER_DROP, use_reads=True)
    assert start is early


def test_the_error_names_the_read_time_and_the_cutoff(store: sqlite3.Connection) -> None:
    given = _given(StarterStatus.CONFIRMED, BEFORE_DROP + STEP)
    with pytest.raises(HockeyModelError) as caught:
        resolve_goalie(store, _game("away", given), "TOR", BEFORE_DROP, use_reads=True)
    text = str(caught.value)
    assert "2026-01-19T16:01:00" in text
    assert "2026-01-19T16:00:00" in text


def _public(store: sqlite3.Connection, given: GoalieStart, as_of):
    model = NhlGameModel(store, artifact(ELO_ONLY), GoalieMode.GUESS)
    return game_win_probability(_game("home", given), model, as_of=as_of)


def test_the_public_call_reports_a_likely_supplied_start(store: sqlite3.Connection) -> None:
    given = _given(StarterStatus.LIKELY, BEFORE_DROP - STEP)
    kinds = [m.kind for m in _public(store, given, BEFORE_DROP).missing]
    assert MissingKind.GOALIE_UNCONFIRMED in kinds


def test_the_public_call_keeps_the_late_run_note(store: sqlite3.Connection) -> None:
    given = _given(StarterStatus.CONFIRMED, START - STEP)
    result = _public(store, given, AFTER_DROP)
    assert result.made_after_start
    assert MissingKind.LATE_RUN in [m.kind for m in result.missing]


def test_the_public_call_rejects_a_start_read_after_as_of(store: sqlite3.Connection) -> None:
    given = _given(StarterStatus.CONFIRMED, BEFORE_DROP + STEP)
    with pytest.raises(HockeyModelError, match="cutoff"):
        _public(store, given, BEFORE_DROP)
