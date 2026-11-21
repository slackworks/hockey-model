"""Tests for saved fits."""

import math
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hockey_model.game_model.features import FEATURES, feature_name_problem
from hockey_model.game_model.logistic import FitDiagnostics, LogisticFit
from hockey_model.store.db import StoreError, connect
from hockey_model.store.fits import FitRecord, FitRef, load_fit, save_fit

T0 = datetime(2026, 1, 1, tzinfo=UTC)


@contextmanager
def _fresh_store(factory: pytest.TempPathFactory) -> Generator[sqlite3.Connection]:
    """Yield an empty store that belongs to one Hypothesis example."""
    connection = connect(factory.mktemp("store") / "store.db")
    try:
        yield connection
    finally:
        connection.close()


def _loaded(conn: sqlite3.Connection, fit_id: int) -> LogisticFit:
    """Return the fit that ``fit_id`` names, as the old `newest_fit` returned a bare fit."""
    return load_fit(conn, FitRef(fit_id=fit_id)).fit


def test_save_and_load_a_fit(store: sqlite3.Connection) -> None:
    fit = LogisticFit(("xg_form", "rest"), 0.1, 0.9, {"xg_form": 2.5, "rest": -0.05})
    fit_id = save_fit(store, FitRecord(fit, 20252026, T0))
    assert _loaded(store, fit_id) == fit


def test_a_fit_loads_by_its_own_id_not_by_its_save_order(
    store: sqlite3.Connection,
) -> None:
    old = LogisticFit((), 0.0, 1.0, {})
    new = LogisticFit(("rest",), 0.2, 1.1, {"rest": 0.01})
    save_fit(store, FitRecord(new, 20252026, T0 + timedelta(days=1)))
    old_id = save_fit(store, FitRecord(old, 20242025, T0))
    assert _loaded(store, old_id) == old


def test_save_fit_returns_increasing_ids(store: sqlite3.Connection) -> None:
    fit = LogisticFit((), 0.0, 1.0, {})
    first = save_fit(store, FitRecord(fit, 20252026, T0))
    assert first < save_fit(store, FitRecord(fit, 20252026, T0))


@pytest.mark.parametrize(
    ("features", "weights"),
    [
        ("not json", "{}"),
        ('["rest"]', "{}"),
        ('["rest"]', '{"intercept": 0, "elo_weight": 1}'),
        ('["rest"]', '{"intercept": "x", "elo_weight": 1, "features": {}}'),
    ],
)
def test_a_malformed_saved_fit_says_to_fit_again(
    store: sqlite3.Connection, features: str, weights: str
) -> None:
    store.execute(
        "INSERT INTO model_fits (fit_through_season, features, weights, created_at)"
        " VALUES (1, ?, ?, '2026-01-01T00:00:00.000000+00:00')",
        (features, weights),
    )
    with pytest.raises(StoreError, match="malformed.*run `hockey-model fit` again"):
        load_fit(store, FitRef(fit_id=1))


DIAGNOSTICS = FitDiagnostics(
    iterations=6,
    objective=9123.5,
    gradient_norm=2e-9,
    condition=4.2e3,
    l2=1.0,
    means={"elo_logit": 0.1, "rest": 0.0},
    scales={"elo_logit": 0.4, "rest": 1.0},
    stop_reason="converged: largest gradient entry below 1e-06",
    covariance=((0.5, 0.0, 0.1), (0.0, 0.25, 0.0), (0.1, 0.0, 0.3)),
)

_NO_DIAGNOSTICS = '{"intercept": 0, "elo_weight": 1, "features": {}}'
_BAD_DIAGNOSTICS = (
    '{"intercept": 0, "elo_weight": 1, "features": {}, "diagnostics": {"iterations": 1}}'
)
_INSERT = (
    "INSERT INTO model_fits (fit_through_season, features, weights, created_at)"
    " VALUES (1, '[]', ?, '2026-01-01T00:00:00.000000+00:00')"
)


def test_diagnostics_survive_a_save_and_load(store: sqlite3.Connection) -> None:
    fit = LogisticFit(("rest",), 0.1, 0.9, {"rest": 0.02}, DIAGNOSTICS)
    fit_id = save_fit(store, FitRecord(fit, 20252026, T0))
    assert _loaded(store, fit_id) == fit


def test_a_fit_saved_without_diagnostics_loads_with_none(store: sqlite3.Connection) -> None:
    store.execute(_INSERT, (_NO_DIAGNOSTICS,))
    assert _loaded(store, 1).diagnostics is None


def test_malformed_diagnostics_say_to_fit_again(store: sqlite3.Connection) -> None:
    store.execute(_INSERT, (_BAD_DIAGNOSTICS,))
    with pytest.raises(StoreError, match="malformed.*run `hockey-model fit` again"):
        load_fit(store, FitRef(fit_id=1))


def test_a_fit_saved_before_the_covariance_existed_loads_with_none(
    store: sqlite3.Connection,
) -> None:
    stored = (
        '{"intercept": 0, "elo_weight": 1, "features": {}, "diagnostics": {"iterations": 1,'
        ' "objective": 1.0, "gradient_norm": 0.0, "condition": 1.0, "l2": 1.0, "means": {},'
        ' "scales": {}, "stop_reason": "converged"}}'
    )
    store.execute(_INSERT, (stored,))
    loaded = _loaded(store, 1)
    assert loaded.diagnostics is not None and loaded.diagnostics.covariance is None


@pytest.mark.parametrize(
    "covariance",
    ["[[1.0]]", "[[1.0, 0.0], [0.0]]", "[[1.0, NaN], [0.0, 1.0]]", '"x"', "[1.0, 2.0]"],
)
def test_a_saved_covariance_of_the_wrong_shape_says_to_fit_again(
    store: sqlite3.Connection, covariance: str
) -> None:
    stored = (
        '{"intercept": 0, "elo_weight": 1, "features": {}, "diagnostics": {"iterations": 1,'
        ' "objective": 1.0, "gradient_norm": 0.0, "condition": 1.0, "l2": 1.0, "means":'
        ' {"elo_logit": 0.0}, "scales": {"elo_logit": 1.0}, "stop_reason": "converged",'
        f' "covariance": {covariance}}}}}'
    )
    store.execute(_INSERT, (stored,))
    with pytest.raises(StoreError, match="malformed.*run `hockey-model fit` again"):
        load_fit(store, FitRef(fit_id=1))


_FINITE = st.floats(allow_nan=False, allow_infinity=False)


@settings(max_examples=40, deadline=None)
@given(
    iterations=st.integers(min_value=0, max_value=50),
    numbers=st.lists(_FINITE, min_size=4, max_size=4),
    scaling=st.dictionaries(st.text(min_size=1, max_size=8), _FINITE, max_size=3),
)
def test_any_diagnostics_round_trip_through_the_store(
    tmp_path_factory: pytest.TempPathFactory,
    iterations: int,
    numbers: list[float],
    scaling: dict[str, float],
) -> None:
    objective, gradient_norm, condition, l2 = numbers
    diagnostics = FitDiagnostics(
        iterations, objective, gradient_norm, condition, l2, scaling, dict(scaling), "stop"
    )
    fit = LogisticFit((), 0.0, 1.0, {}, diagnostics)
    with _fresh_store(tmp_path_factory) as store:
        fit_id = save_fit(store, FitRecord(fit, 20252026, T0))
        assert _loaded(store, fit_id) == fit


@pytest.mark.parametrize(
    "weights",
    [
        '{"intercept": NaN, "elo_weight": 1, "features": {}}',
        '{"intercept": 0, "elo_weight": Infinity, "features": {}}',
        '{"intercept": 0, "elo_weight": 1, "features": {"rest": NaN}}',
    ],
)
def test_a_saved_fit_with_a_value_that_is_not_finite_says_to_fit_again(
    store: sqlite3.Connection, weights: str
) -> None:
    store.execute(
        "INSERT INTO model_fits (fit_through_season, features, weights, created_at)"
        " VALUES (1, '[\"rest\"]', ?, '2026-01-01T00:00:00.000000+00:00')",
        (weights,),
    )
    with pytest.raises(StoreError, match="malformed.*run `hockey-model fit` again"):
        load_fit(store, FitRef(fit_id=1))


@pytest.mark.parametrize(
    ("features", "weights"),
    [
        ('["rest"]', '{"intercept": 0, "elo_weight": 1, "features": {}}'),
        ("[]", '{"intercept": 0, "elo_weight": 1, "features": {"rest": 1.0}}'),
    ],
)
def test_a_saved_fit_whose_features_and_weights_disagree_says_to_fit_again(
    store: sqlite3.Connection, features: str, weights: str
) -> None:
    store.execute(
        "INSERT INTO model_fits (fit_through_season, features, weights, created_at)"
        " VALUES (1, ?, ?, '2026-01-01T00:00:00.000000+00:00')",
        (features, weights),
    )
    with pytest.raises(StoreError, match="malformed.*run `hockey-model fit` again"):
        load_fit(store, FitRef(fit_id=1))


@pytest.mark.parametrize("name", ["attempt_form", "luck"])
def test_a_saved_fit_that_names_a_removed_feature_says_which_one(
    store: sqlite3.Connection, name: str
) -> None:
    """A well-formed fit is not malformed. It names a feature that this version lacks."""
    store.execute(
        "INSERT INTO model_fits (fit_through_season, features, weights, created_at)"
        " VALUES (1, ?, ?, '2026-01-01T00:00:00.000000+00:00')",
        (f'["{name}"]', f'{{"intercept": 0, "elo_weight": 1, "features": {{"{name}": 1.0}}}}'),
    )
    with pytest.raises(StoreError) as caught:
        load_fit(store, FitRef(fit_id=1))
    message = str(caught.value)
    assert f"the saved fit names '{name}', which this version does not have" in message
    assert "malformed" not in message
    assert "features from xg_form, goalie_gsax, rest" in message


_WEIGHT = st.floats(min_value=-1e3, max_value=1e3, allow_nan=False)


@settings(max_examples=40, deadline=None)
@given(
    features=st.lists(st.sampled_from(FEATURES), unique=True, max_size=len(FEATURES)),
    intercept=_WEIGHT,
    elo_weight=_WEIGHT,
    data=st.data(),
)
def test_any_fit_round_trips_through_the_store(
    tmp_path_factory: pytest.TempPathFactory,
    features: list[str],
    intercept: float,
    elo_weight: float,
    data: Any,
) -> None:
    weights = {name: data.draw(_WEIGHT) for name in features}
    fit = LogisticFit(tuple(features), intercept, elo_weight, weights)
    with _fresh_store(tmp_path_factory) as store:
        fit_id = save_fit(store, FitRecord(fit, 20252026, T0))
        assert _loaded(store, fit_id) == fit


def _row_count(store: sqlite3.Connection) -> int:
    return store.execute("SELECT COUNT(*) FROM model_fits").fetchone()[0]


@pytest.mark.parametrize(
    ("fit", "reason"),
    [
        (LogisticFit(("luck",), 0.0, 1.0, {"luck": 1.0}), "unknown feature 'luck'"),
        (LogisticFit(("rest", "rest"), 0.0, 1.0, {"rest": 1.0}), "named more than once"),
        (LogisticFit(("rest",), 0.0, 1.0, {}), "different features"),
        (LogisticFit(("rest",), 0.0, 1.0, {"rest": float("nan")}), "not a finite number"),
        (LogisticFit(("rest",), float("inf"), 1.0, {"rest": 0.1}), "not a finite number"),
    ],
)
def test_save_fit_refuses_an_invalid_fit_and_leaves_no_row(
    store: sqlite3.Connection, fit: LogisticFit, reason: str
) -> None:
    with pytest.raises(StoreError, match=f"save the model fit.*{reason}"):
        save_fit(store, FitRecord(fit, 20252026, T0))
    assert _row_count(store) == 0


_NAMES = st.lists(st.one_of(st.sampled_from(FEATURES), st.text(max_size=6)), max_size=5)
_NUMBER = st.one_of(
    st.floats(allow_nan=False, allow_infinity=False, width=32), st.just(float("nan"))
)


@settings(max_examples=100, deadline=None)
@given(names=_NAMES, intercept=_NUMBER, weight=_NUMBER, drop_a_weight=st.booleans())
def test_save_fit_accepts_a_fit_exactly_when_it_follows_the_four_rules(
    tmp_path_factory: pytest.TempPathFactory,
    names: list[str],
    intercept: float,
    weight: float,
    drop_a_weight: bool,
) -> None:
    """Acceptance matches a plain reading of the rules: known, once each, weighed, finite."""
    weights = {name: weight for name in names}
    if drop_a_weight and weights:
        weights.pop(next(iter(weights)))
    fit = LogisticFit(tuple(names), intercept, 1.0, weights)
    valid = (
        all(name in FEATURES for name in names)
        and len(set(names)) == len(names)
        and set(weights) == set(names)
        and all(map(math.isfinite, [intercept, *weights.values()]))
    )
    with _fresh_store(tmp_path_factory) as store:
        if valid:
            save_fit(store, FitRecord(fit, 20252026, T0))
            assert _row_count(store) == 1
        else:
            with pytest.raises(StoreError):
                save_fit(store, FitRecord(fit, 20252026, T0))
            assert _row_count(store) == 0


@given(names=_NAMES)
def test_feature_name_problem_is_none_exactly_for_distinct_known_names(names: list[str]) -> None:
    ok = all(n in FEATURES for n in names) and len(set(names)) == len(names)
    assert (feature_name_problem(names) is None) == ok
