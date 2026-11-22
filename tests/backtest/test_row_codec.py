"""The text form of the training rows of one season: exact round trip, named failures."""

import json
import sqlite3
import struct
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st
from test_runner_store import PLAN, seed

from hockey_model.backtest.row_codec import decode_rows, encode_rows
from hockey_model.game import DegradedInput, MissingInput, MissingKind
from hockey_model.game_model.features import FEATURES, FeatureRow, GoalieMode
from hockey_model.game_model.training import TrainingRow, training_rows
from hockey_model.sources.nhl import GameRow
from hockey_model.types import HockeyModelError

_FLOATS = st.floats(allow_nan=False, allow_infinity=False, width=64)
_TEAMS = st.sampled_from(["TOR", "BOS", "UTA"])
_TEXT = st.text(max_size=30)


@st.composite
def _rows(draw: st.DrawFn) -> TrainingRow:
    start = datetime(2020, 1, 1, tzinfo=UTC) + timedelta(seconds=draw(st.integers(0, 10**8)))
    game = GameRow(
        draw(st.integers(1, 10**7)),
        draw(st.integers(1, 30)),
        draw(st.sampled_from([2, 3])),
        start,
        draw(_TEAMS),
        draw(_TEAMS),
        draw(st.sampled_from(["OFF", "FINAL"])),
        draw(st.one_of(st.none(), st.integers(0, 12))),
        draw(st.one_of(st.none(), st.integers(0, 12))),
        draw(st.one_of(st.none(), st.sampled_from(["REG", "OT", "SO"]))),
    )
    missing = tuple(
        MissingInput(
            draw(st.sampled_from(list(MissingKind))), draw(st.none() | _TEAMS), draw(_TEXT)
        )
        for _ in range(draw(st.integers(0, 3)))
    )
    imputed = tuple(
        DegradedInput(
            draw(st.sampled_from(FEATURES)),
            draw(st.sampled_from(list(MissingKind))),
            tuple(draw(st.lists(_TEAMS, max_size=2))),
        )
        for _ in range(draw(st.integers(0, 2)))
    )
    features = FeatureRow(
        game.game_id,
        draw(_FLOATS),
        {name: draw(_FLOATS) for name in FEATURES},
        missing,
        draw(st.booleans()),
        imputed,
    )
    return TrainingRow(game, features, draw(st.booleans()))


@given(st.lists(_rows(), max_size=6))
def test_any_rows_come_back_equal_to_the_rows_that_went_in(rows: list[TrainingRow]) -> None:
    assert decode_rows(encode_rows(rows)) == rows


@pytest.mark.parametrize("mode", [GoalieMode.ACTUAL, GoalieMode.GUESS])
def test_the_rows_of_a_made_up_store_round_trip(
    store: sqlite3.Connection, mode: GoalieMode
) -> None:
    seed(store, range(1, 4))
    store.commit()
    rows = training_rows(store, 2, 3, mode, PLAN.burn_in, names=FEATURES)
    assert rows
    assert decode_rows(encode_rows(rows)) == rows


def _bits(number: float) -> bytes:
    return struct.pack("<d", number)


def test_a_float_comes_back_with_every_bit() -> None:
    value = 0.1 + 0.2
    row = TrainingRow(
        GameRow(1, 2, 2, datetime(2020, 1, 1, tzinfo=UTC), "TOR", "BOS", "OFF", 3, 2, "REG"),
        FeatureRow(1, value, {"rest": value}, (), False),
        True,
    )
    (back,) = decode_rows(encode_rows([row]))
    assert _bits(back.features.elo_logit) == _bits(value)
    assert _bits(back.features.values["rest"]) == _bits(value)


def test_no_rows_round_trip_as_an_empty_list() -> None:
    assert decode_rows(encode_rows([])) == []


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("not json", "not JSON"),
        ("{}", "a list"),
        ('[{"game": {}}]', "row 0"),
        ('[{"game": 1, "features": 2, "home_won": true}]', "row 0"),
    ],
)
def test_a_damaged_text_raises_a_named_error(text: str, why: str) -> None:
    with pytest.raises(HockeyModelError, match=why):
        decode_rows(text)


def test_an_unknown_kind_raises_a_named_error(store: sqlite3.Connection) -> None:
    seed(store, range(1, 3))
    store.commit()
    rows = training_rows(store, 2, 2, GoalieMode.ACTUAL, PLAN.burn_in, names=FEATURES)
    data = json.loads(encode_rows(rows))
    data[0]["features"]["missing"] = [{"kind": "not_a_kind", "team": None, "note": "x"}]
    with pytest.raises(HockeyModelError, match="row 0.*not_a_kind"):
        decode_rows(json.dumps(data))
