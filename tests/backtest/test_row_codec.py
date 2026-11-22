"""The text form of the training rows of one season: exact round trip, named failures."""

import json
import sqlite3
import struct
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

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


def _bits(number: float) -> bytes:
    return struct.pack("<d", number)


_FLOATS = st.floats(allow_nan=False, allow_infinity=False, width=64)
_TEAMS = st.sampled_from(["TOR", "BOS", "UTA"])
_TEXT = st.text(max_size=30)


@st.composite
def _rows(draw: st.DrawFn) -> TrainingRow:
    start = datetime(2020, 1, 1, tzinfo=UTC) + timedelta(
        seconds=draw(st.integers(0, 10**8)), microseconds=draw(st.integers(0, 999_999))
    )
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


def _all_bits(rows: list[TrainingRow]) -> list[bytes]:
    """Return every float of the rows as bytes, so -0.0 and 0.0 differ."""
    bits: list[bytes] = []
    for row in rows:
        bits.append(_bits(row.features.elo_logit))
        bits.extend(_bits(value) for value in row.features.values.values())
    return bits


@given(st.lists(_rows(), max_size=6))
def test_any_rows_come_back_equal_to_the_rows_that_went_in(rows: list[TrainingRow]) -> None:
    back = decode_rows(encode_rows(rows))
    assert back == rows
    assert _all_bits(back) == _all_bits(rows)


@pytest.mark.parametrize("mode", [GoalieMode.ACTUAL, GoalieMode.GUESS])
def test_the_rows_of_a_made_up_store_round_trip(
    store: sqlite3.Connection, mode: GoalieMode
) -> None:
    seed(store, range(1, 4))
    store.commit()
    rows = training_rows(store, 2, 3, mode, PLAN.burn_in, names=FEATURES)
    assert rows
    assert decode_rows(encode_rows(rows)) == rows


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
        ("not json", "not valid JSON"),
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


def _check_types(rows: list[TrainingRow]) -> None:
    """Assert that every field of the rows has the type that its dataclass declares."""
    for row in rows:
        game, features = row.game, row.features
        for number in (game.game_id, game.season, game.game_type):
            assert type(number) is int
        for text in (game.home, game.away, game.state):
            assert type(text) is str
        for score in (game.home_score, game.away_score):
            assert score is None or type(score) is int
        assert game.last_period is None or type(game.last_period) is str
        assert type(features.game_id) is int
        assert type(features.elo_logit) is float
        assert all(type(v) is float for v in features.values.values())
        assert type(features.uses_moneypuck) is bool
        assert type(row.home_won) is bool
        assert all(type(m.note) is str for m in features.missing)
        assert all(type(t) is str for d in features.imputed for t in d.teams)


def _leaves(node: object, path: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    if isinstance(node, dict):
        return [leaf for key, child in node.items() for leaf in _leaves(child, (*path, key))]
    if isinstance(node, list):
        return [leaf for i, child in enumerate(node) for leaf in _leaves(child, (*path, i))]
    return [path]


_WRONG = st.sampled_from([None, True, False, 0, 7, 1.5, "x", "", [], {}, {"a": 1}])


@given(st.lists(_rows(), min_size=1, max_size=3), st.data(), _WRONG)
def test_a_changed_leaf_is_refused_or_still_has_the_declared_types(
    rows: list[TrainingRow], data: st.DataObject, wrong: object
) -> None:
    tree = json.loads(encode_rows(rows))
    path = data.draw(st.sampled_from(_leaves(tree)))
    parent = tree
    for step in path[:-1]:
        parent = parent[step]
    parent[path[-1]] = wrong
    try:
        back = decode_rows(json.dumps(tree))
    except HockeyModelError:
        return
    _check_types(back)


@pytest.mark.parametrize(
    "change",
    [
        lambda row: row.update(home_won="no"),
        lambda row: row["game"].update(game_id="1"),
        lambda row: row["features"].update(elo_logit="x"),
        lambda row: row["features"].update(elo_logit=1),
        lambda row: row["features"].update(uses_moneypuck=1),
        lambda row: row["features"]["values"].update(rest=None),
        lambda row: row["game"].update(home_score=True),
        lambda row: row["game"].update(start="2020-01-01"),
    ],
    ids=[
        "string home_won",
        "string game_id",
        "string elo_logit",
        "int elo_logit",
        "int flag",
        "null value",
        "bool score",
        "naive start",
    ],
)
def test_a_wrong_type_in_one_field_raises_a_named_error(
    store: sqlite3.Connection, change: Callable[[dict[str, Any]], object]
) -> None:
    seed(store, range(1, 3))
    store.commit()
    rows = training_rows(store, 2, 2, GoalieMode.ACTUAL, PLAN.burn_in, names=FEATURES)
    tree = json.loads(encode_rows(rows))
    change(tree[0])
    with pytest.raises(HockeyModelError, match="row 0"):
        decode_rows(json.dumps(tree))


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_a_constant_that_json_reads_but_a_row_never_holds_is_refused(
    store: sqlite3.Connection, constant: str
) -> None:
    seed(store, range(1, 3))
    store.commit()
    rows = training_rows(store, 2, 2, GoalieMode.ACTUAL, PLAN.burn_in, names=FEATURES)
    text = encode_rows(rows)
    bad = text.replace(
        f'"elo_logit":{json.dumps(rows[0].features.elo_logit)}', f'"elo_logit":{constant}'
    )
    assert bad != text
    with pytest.raises(HockeyModelError, match="not valid JSON"):
        decode_rows(bad)


def test_a_number_with_thousands_of_digits_is_a_named_error_and_not_a_crash() -> None:
    with pytest.raises(HockeyModelError, match="not valid JSON"):
        decode_rows("[" + "9" * 5000 + "]")
