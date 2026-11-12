"""Tests for the promotion bar by look number."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hockey_model.backtest.metrics import BOOTSTRAP_RESAMPLES
from hockey_model.experiments.looks import (
    BASE_TAIL,
    MAX_LOOKS,
    look_problem,
    promotion_tail,
)
from hockey_model.experiments.models import ExperimentRecord
from hockey_model.types import HockeyModelError

_DATA = {
    "id": "0001-first",
    "title": "T",
    "hypothesis": "H",
    "cohort": "confirmation",
    "data_cutoff": "2026-10-31",
    "primary_metric": "mean_per_game_log_loss",
    "baseline": {"fit_id": 1, "commit": "abc"},
    "selection_folds": None,
    "min_games": 10,
    "candidate_features": ["xg_form"],
    "look_number": 1,
    "expected_effect": None,
    "promotion_criterion": "95th percentile below zero",
    "failure_outcome": "ship Elo alone",
    "status": "registered",
}


def _record(**changes: object) -> ExperimentRecord:
    return ExperimentRecord.from_dict({**_DATA, **changes})


def test_the_first_look_uses_the_base_tail() -> None:
    assert promotion_tail(1) == BASE_TAIL


def test_the_tail_is_the_base_tail_divided_by_the_look_number() -> None:
    assert promotion_tail(4) == pytest.approx(BASE_TAIL / 4)


@given(st.integers(min_value=1, max_value=MAX_LOOKS - 1))
def test_each_later_look_has_a_strictly_smaller_tail(look: int) -> None:
    assert promotion_tail(look + 1) < promotion_tail(look)


@pytest.mark.parametrize("look", [0, -1, MAX_LOOKS + 1])
def test_a_look_outside_the_range_is_refused_with_the_range(look: int) -> None:
    with pytest.raises(HockeyModelError, match=rf"look_number {look}.*1 to {MAX_LOOKS}"):
        promotion_tail(look)


def test_the_first_confirmation_record_is_look_one() -> None:
    assert look_problem(_record(), earlier=[]) is None


def test_a_second_confirmation_record_must_be_look_two() -> None:
    first = _record()
    assert look_problem(_record(id="0002-b", look_number=2), earlier=[first]) is None
    problem = look_problem(_record(id="0002-b", look_number=1), earlier=[first])
    assert problem is not None
    assert "look_number is 1" in problem
    assert "expected 2" in problem
    assert "0001-first" in problem


def test_an_exploration_record_does_not_count_as_a_look() -> None:
    exploration = _record(cohort="exploration")
    assert look_problem(_record(id="0002-b"), earlier=[exploration]) is None


def test_an_exploration_record_is_not_checked() -> None:
    assert look_problem(_record(cohort="exploration", look_number=5), earlier=[]) is None


@pytest.mark.parametrize("look", range(1, MAX_LOOKS + 1))
def test_every_supported_look_gives_a_tail_the_bootstrap_accepts(look: int) -> None:
    tail = promotion_tail(look)
    assert 1 / BOOTSTRAP_RESAMPLES <= tail < 0.5


@given(
    st.lists(st.sampled_from(["confirmation", "exploration"]), max_size=6),
    st.integers(min_value=1, max_value=8),
)
def test_look_problem_accepts_exactly_the_count_of_earlier_confirmations_plus_one(
    cohorts: list[str], look: int
) -> None:
    earlier = [_record(id=f"{n:04d}-e", cohort=c) for n, c in enumerate(cohorts, start=1)]
    expected = cohorts.count("confirmation") + 1
    problem = look_problem(_record(id="0099-now", look_number=look), earlier=earlier)
    if look == expected:
        assert problem is None
    else:
        assert problem is not None
        assert f"expected {expected}" in problem


def test_the_first_look_keeps_the_registered_95th_percentile_bar() -> None:
    """Record 0001 registered the 95th percentile. A new default must not move that bar."""
    assert BASE_TAIL == 0.05
