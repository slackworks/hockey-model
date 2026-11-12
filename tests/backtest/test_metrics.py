"""Tests for the backtest metrics."""

import math
import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hockey_model.backtest.metrics import (
    BOOTSTRAP_RESAMPLES,
    MIN_BLOCK_LENGTH,
    MIN_INDEPENDENT_BLOCKS,
    PROB_FLOOR,
    block_bootstrap_mean_ci,
    bootstrap_problem,
    brier,
    calibration,
    interval_ends,
    log_loss,
    per_game_log_loss,
)
from hockey_model.game_model.seasons import INTERVAL_TAIL


def test_log_loss_of_a_coin_flip() -> None:
    assert log_loss([0.5], [True]) == pytest.approx(math.log(2))
    assert log_loss([0.5, 0.5], [True, False]) == pytest.approx(math.log(2))


def test_log_loss_is_the_mean_of_the_games() -> None:
    expected = (-math.log(0.8) - math.log(0.7)) / 2
    assert log_loss([0.8, 0.3], [True, False]) == pytest.approx(expected)


def test_a_zero_probability_on_a_win_is_finite() -> None:
    value = log_loss([0.0], [True])
    assert math.isfinite(value)
    assert value == pytest.approx(13.815510557964274)
    assert PROB_FLOOR == 1e-6


def test_a_certain_probability_on_a_loss_is_finite() -> None:
    assert math.isfinite(log_loss([1.0], [False]))


def test_brier_scores() -> None:
    assert brier([1.0], [True]) == 0.0
    assert brier([0.0], [True]) == 1.0
    assert brier([0.5, 0.5], [True, False]) == pytest.approx(0.25)


@pytest.mark.parametrize("score", [log_loss, brier, calibration])
def test_length_mismatch_raises(score) -> None:
    with pytest.raises(ValueError, match="2 probabilities but 1 results"):
        score([0.5, 0.5], [True])


@pytest.mark.parametrize("score", [log_loss, brier, calibration, per_game_log_loss])
def test_empty_input_raises(score) -> None:
    with pytest.raises(ValueError, match="empty"):
        score([], [])


@pytest.mark.parametrize("bad", [float("nan"), -0.1, 1.1, float("inf")])
def test_a_non_probability_raises(bad: float) -> None:
    with pytest.raises(ValueError, match="not a probability"):
        log_loss([bad], [True])


def test_calibration_bins() -> None:
    bins = calibration([0.05, 0.95, 0.96], [False, True, True])
    assert len(bins) == 10
    assert (bins[0].count, bins[0].win_rate) == (1, 0.0)
    assert (bins[9].count, bins[9].win_rate) == (2, 1.0)
    assert bins[9].mean_prob == pytest.approx(0.955)
    assert (bins[0].low, bins[0].high) == (0.0, 0.1)


def test_calibration_puts_one_in_the_last_bin() -> None:
    assert calibration([1.0], [True])[9].count == 1
    assert calibration([0.0], [False])[0].count == 1


def test_empty_calibration_bins_have_no_means() -> None:
    bins = calibration([0.5], [True])
    assert bins[0].count == 0
    assert bins[0].mean_prob is None
    assert bins[0].win_rate is None


def test_bootstrap_of_a_constant_list() -> None:
    assert block_bootstrap_mean_ci([0.25] * 200, INTERVAL_TAIL) == pytest.approx((0.25, 0.25))


def test_bootstrap_is_repeatable() -> None:
    values = [math.sin(i) for i in range(200)]
    assert block_bootstrap_mean_ci(values, INTERVAL_TAIL) == block_bootstrap_mean_ci(
        values, INTERVAL_TAIL
    )


def test_bootstrap_rejects_an_empty_list() -> None:
    with pytest.raises(ValueError, match="empty"):
        block_bootstrap_mean_ci([], INTERVAL_TAIL)


def test_bootstrap_rejects_a_non_finite_value() -> None:
    with pytest.raises(ValueError, match="not finite"):
        block_bootstrap_mean_ci([0.1] * 200 + [float("nan")], INTERVAL_TAIL)


def test_bootstrap_interval_contains_the_mean_of_a_large_sample() -> None:
    values = [0.1 * ((i * 7) % 5 - 2) + 0.02 for i in range(400)]
    low, high = block_bootstrap_mean_ci(values, INTERVAL_TAIL)
    mean = sum(values) / len(values)
    assert low < mean < high
    assert high - low < 0.1


def test_bootstrap_interval_is_about_1_645_standard_errors_wide() -> None:
    rng = random.Random(1)
    values = [rng.gauss(0.0, 1.0) for _ in range(1600)]
    low, high = block_bootstrap_mean_ci(values, INTERVAL_TAIL)
    mean = sum(values) / len(values)
    assert 0.02 < high - mean < 0.07
    assert 0.02 < mean - low < 0.07


def test_bootstrap_of_a_clearly_negative_mean_is_below_zero() -> None:
    values = [-0.05 + 0.01 * ((i * 7) % 5 - 2) for i in range(500)]
    assert block_bootstrap_mean_ci(values, INTERVAL_TAIL)[1] < 0


def test_bootstrap_of_a_zero_mean_straddles_zero() -> None:
    values = [0.1 * ((i * 7) % 5 - 2) for i in range(500)]
    low, high = block_bootstrap_mean_ci(values, INTERVAL_TAIL)
    assert low < 0 < high


@pytest.mark.parametrize(
    "values",
    [[0.3], [-1.0, 0.5], [0.1, 0.5, 0.9], [0.1 * (i % 3) for i in range(MIN_BLOCK_LENGTH)]],
)
def test_a_series_with_too_few_blocks_has_no_interval(values: list[float]) -> None:
    # One block repeated gives (-0.25, -0.25) for [-1.0, 0.5]: certainty from no data.
    with pytest.raises(ValueError, match="independent blocks"):
        block_bootstrap_mean_ci(values, INTERVAL_TAIL)


def test_the_smallest_series_with_enough_blocks_gets_an_interval() -> None:
    enough = MIN_INDEPENDENT_BLOCKS * MIN_BLOCK_LENGTH
    values = [math.sin(i) for i in range(enough)]
    low, high = block_bootstrap_mean_ci(values, INTERVAL_TAIL)
    assert low <= high
    with pytest.raises(ValueError, match="independent blocks"):
        block_bootstrap_mean_ci(values[:-1], INTERVAL_TAIL)


def test_bootstrap_problem_names_the_counts() -> None:
    assert bootstrap_problem(MIN_INDEPENDENT_BLOCKS * MIN_BLOCK_LENGTH) is None
    problem = bootstrap_problem(33)
    assert problem is not None
    assert "33 scored games hold 2 independent blocks" in problem


def test_a_block_is_one_nights_slate() -> None:
    assert MIN_BLOCK_LENGTH == 16


def test_the_number_of_resamples() -> None:
    assert BOOTSTRAP_RESAMPLES == 1000


@settings(max_examples=30, deadline=None)
@given(st.lists(st.floats(min_value=-10, max_value=10), min_size=160, max_size=240))
def test_bootstrap_interval_is_ordered_and_inside_the_data(values: list[float]) -> None:
    low, high = block_bootstrap_mean_ci(values, INTERVAL_TAIL)
    assert low <= high
    assert min(values) - 1e-9 <= low
    assert high <= max(values) + 1e-9


@given(
    st.lists(st.floats(min_value=0, max_value=1), min_size=1, max_size=50),
    st.data(),
)
def test_scores_are_non_negative(probs: list[float], data: st.DataObject) -> None:
    wins = data.draw(st.lists(st.booleans(), min_size=len(probs), max_size=len(probs)))
    assert log_loss(probs, wins) >= 0
    assert 0 <= brier(probs, wins) <= 1
    assert sum(b.count for b in calibration(probs, wins)) == len(probs)


@given(
    st.lists(st.floats(min_value=0, max_value=1), min_size=1, max_size=50),
    st.data(),
)
def test_each_calibration_mean_lies_inside_its_bin(probs: list[float], data: st.DataObject) -> None:
    wins = data.draw(st.lists(st.booleans(), min_size=len(probs), max_size=len(probs)))
    for bin_ in calibration(probs, wins):
        if bin_.count:
            assert bin_.mean_prob is not None
            assert bin_.win_rate is not None
            assert bin_.low - 1e-12 <= bin_.mean_prob <= bin_.high + 1e-12
            assert 0.0 <= bin_.win_rate <= 1.0


def test_brier_counts_a_loss_as_zero() -> None:
    assert brier([0.2], [False]) == pytest.approx(0.04)
    assert brier([0.2, 0.9], [False, True]) == pytest.approx((0.04 + 0.01) / 2)


def test_every_calibration_bin_states_its_edges() -> None:
    bins = calibration([0.5], [True])
    assert [(b.low, b.high) for b in bins][:2] == [(0.0, 0.1), (0.1, 0.2)]
    assert all(b.low is not None and b.high is not None for b in bins)
    assert (bins[3].low, bins[3].high) == (0.3, 0.4)


_OUTCOMES = st.lists(st.booleans(), min_size=2, max_size=60).filter(lambda w: 0 < sum(w) < len(w))


@given(wins=_OUTCOMES, other=st.floats(min_value=0.02, max_value=0.98))
def test_the_win_rate_is_the_best_constant_forecast(wins: list[bool], other: float) -> None:
    rate = sum(wins) / len(wins)
    at_rate = [rate] * len(wins)
    at_other = [other] * len(wins)
    assert log_loss(at_rate, wins) <= log_loss(at_other, wins) + 1e-12
    assert brier(at_rate, wins) <= brier(at_other, wins) + 1e-12


@given(
    probs=st.lists(st.floats(min_value=0.01, max_value=0.99), min_size=1, max_size=40),
    data=st.data(),
)
def test_swapping_both_sides_leaves_the_scores_unchanged(probs: list[float], data) -> None:
    wins = data.draw(st.lists(st.booleans(), min_size=len(probs), max_size=len(probs)))
    flipped_probs = [1.0 - p for p in probs]
    flipped_wins = [not w for w in wins]
    assert log_loss(flipped_probs, flipped_wins) == pytest.approx(log_loss(probs, wins))
    assert brier(flipped_probs, flipped_wins) == pytest.approx(brier(probs, wins))


@settings(max_examples=40, deadline=None)
@given(st.integers(min_value=1, max_value=300))
def test_the_bootstrap_raises_exactly_when_the_sample_is_too_small(count: int) -> None:
    values = [math.sin(i) for i in range(count)]
    if bootstrap_problem(count) is None:
        block_bootstrap_mean_ci(values, INTERVAL_TAIL)
    else:
        with pytest.raises(ValueError, match="independent blocks"):
            block_bootstrap_mean_ci(values, INTERVAL_TAIL)


def test_a_smaller_tail_gives_a_wider_interval() -> None:
    rng = random.Random(3)
    values = [rng.gauss(0.0, 1.0) for _ in range(400)]
    low5, high5 = block_bootstrap_mean_ci(values, INTERVAL_TAIL)
    low05, high05 = block_bootstrap_mean_ci(values, tail=0.005)
    assert low05 < low5 < high5 < high05


@pytest.mark.parametrize("tail", [0.0, -0.1, 0.0004, 0.4996, 0.5, 0.9, float("nan")])
def test_bootstrap_rejects_a_tail_outside_the_open_half(tail: float) -> None:
    with pytest.raises(ValueError, match="tail"):
        block_bootstrap_mean_ci([0.1] * 200, tail=tail)


@given(st.integers(min_value=1, max_value=499))
def test_each_end_leaves_the_same_count_of_means_outside(cut: int) -> None:
    means = list(range(1000))
    low, high = interval_ends(means, cut / 1000)
    assert low == cut
    assert 999 - high == cut


@given(
    st.lists(st.floats(min_value=-1.0, max_value=1.0), min_size=200, max_size=300),
    st.floats(min_value=1 / BOOTSTRAP_RESAMPLES, max_value=0.4994),
)
@settings(max_examples=20, deadline=None)
def test_the_interval_is_ordered_and_inside_the_data_for_any_tail(
    values: list[float], tail: float
) -> None:
    low, high = block_bootstrap_mean_ci(values, tail)
    assert low <= high
    assert min(values) - 1e-9 <= low
    assert high <= max(values) + 1e-9


@given(
    st.floats(min_value=1 / BOOTSTRAP_RESAMPLES, max_value=0.2),
    st.floats(min_value=1 / BOOTSTRAP_RESAMPLES, max_value=0.2),
)
@settings(max_examples=20, deadline=None)
def test_a_smaller_tail_gives_an_interval_that_contains_the_larger_tail_interval(
    a: float, b: float
) -> None:
    values = [random.Random(7).gauss(0.0, 1.0) for _ in range(300)]
    small, large = sorted((a, b))
    wide = block_bootstrap_mean_ci(values, small)
    narrow = block_bootstrap_mean_ci(values, large)
    assert wide[0] <= narrow[0]
    assert narrow[1] <= wide[1]
