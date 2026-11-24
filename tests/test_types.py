"""Tests for public domain value objects."""

import math
import pickle

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hockey_model.sources.errors import SourceError
from hockey_model.sources.shape_errors import SourceParseError, SourceShapeError
from hockey_model.types import (
    AmbiguousPlayerError,
    HockeyModelError,
    Player,
    Projection,
    Roster,
    Slot,
    Standing,
    Team,
    UnknownPlayerError,
)


def _player(**changes: object) -> Player:
    values = {
        "canonical_id": "p-1",
        "yahoo_id": None,
        "nhl_id": None,
        "full_name": "A Player",
        "eligible": frozenset({Slot.C}),
        "team_code": None,
    }
    values.update(changes)
    return Player(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["point", "low", "high", "level"])
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_projection_rejects_non_finite_values(field: str, value: float) -> None:
    values = {"point": 0.5, "low": 0.0, "high": 1.0, "level": 0.8}
    values[field] = value
    with pytest.raises(ValueError, match=f"{field} must be finite"):
        Projection(**values)


@pytest.mark.parametrize("level", [0.0, 1.0, -0.1, 1.5])
def test_projection_rejects_level_outside_open_unit_interval(level: float) -> None:
    with pytest.raises(ValueError, match="level"):
        Projection(point=0.5, low=0.0, high=1.0, level=level)


def test_projection_rejects_low_above_high() -> None:
    with pytest.raises(ValueError, match="low"):
        Projection(point=0.5, low=1.0, high=0.0, level=0.8)


def test_player_rejects_empty_canonical_id() -> None:
    with pytest.raises(ValueError, match="canonical_id"):
        _player(canonical_id="")


def test_player_rejects_no_eligible_positions() -> None:
    with pytest.raises(ValueError, match="eligible"):
        _player(eligible=frozenset())


def test_player_rejects_roster_only_eligibility() -> None:
    with pytest.raises(ValueError, match="roster-only"):
        _player(eligible=frozenset({Slot.BENCH}))


def test_player_source_fields_are_validated() -> None:
    with pytest.raises(ValueError, match="yahoo_id"):
        _player(yahoo_id=" ")
    with pytest.raises(ValueError, match="nhl_id"):
        _player(nhl_id=0)
    with pytest.raises(ValueError, match="team_code"):
        _player(team_code="")


def test_team_and_standing_validate_fields() -> None:
    assert Team("TOR", "Toronto Maple Leafs", "Atlantic", "Eastern").code == "TOR"
    with pytest.raises(ValueError, match="name"):
        Team("TOR", "", "Atlantic", "Eastern")
    with pytest.raises(ValueError, match="games_played"):
        Standing("TOR", -1, 0, 0, 1)
    with pytest.raises(ValueError, match="rank"):
        Standing("TOR", 0, 0, 0, 0)


def test_roster_rejects_duplicate_players() -> None:
    player = _player()
    with pytest.raises(ValueError, match="unique"):
        Roster("duplicate", (player, player))


@given(
    st.builds(
        lambda cid, fname, eligible, team: Player(cid, None, None, fname, eligible, team),
        cid=st.text(min_size=1).filter(lambda s: s.strip()),
        fname=st.text(min_size=1).filter(lambda s: s.strip()),
        eligible=st.sets(
            st.sampled_from([Slot.C, Slot.W, Slot.D, Slot.G]),
            min_size=1,
            max_size=4,
        ),
        team=st.one_of(st.just(None), st.text(min_size=1).filter(lambda s: s.strip())),
    ),
)
@settings(max_examples=500)
def test_player_eligible_contains_only_valid_slots(player: Player) -> None:
    if player.eligible is not None:
        assert player.eligible <= {Slot.C, Slot.W, Slot.D, Slot.G}


@st.composite
def valid_projections(draw):
    low = draw(st.floats(min_value=0.0, max_value=5.0))
    point = draw(st.floats(min_value=low, max_value=10.0))
    high = draw(st.floats(min_value=max(point, 5.0), max_value=10.0))
    level = draw(st.floats(min_value=0.01, max_value=0.99))
    return Projection(point, low, high, level)


@given(valid_projections())
def test_projection_satisfies_low_le_point_le_high(projection: Projection) -> None:
    assert projection.low <= projection.point <= projection.high


def _errors() -> list[HockeyModelError]:
    return [
        HockeyModelError("plain message"),
        HockeyModelError("first", "second"),
        AmbiguousPlayerError("Smith", (_player(), _player(canonical_id="p-2"))),
        UnknownPlayerError(8471234),
        SourceShapeError("espn", "XYZ"),
        SourceParseError("nhl", "the games list is missing"),
        SourceError("nhl", "https://example.test/x", "HTTP 403", permanent=True, status=403),
    ]


# A worker process sends its error back by pickle, so each error must come back the same.
@pytest.mark.parametrize("error", _errors(), ids=lambda error: type(error).__name__)
def test_an_error_comes_back_from_a_pickle_with_its_type_message_and_fields(
    error: HockeyModelError,
) -> None:
    copy = pickle.loads(pickle.dumps(error))
    assert type(copy) is type(error)
    assert (str(copy), copy.args) == (str(error), error.args)
    assert vars(copy) == vars(error)
