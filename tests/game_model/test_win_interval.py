"""Tests for the model-uncertainty interval of the home win chance (#212)."""

import math
import random
from dataclasses import replace
from statistics import NormalDist

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from test_logistic import row, sample

from hockey_model.game_model.logistic import (
    ELO_ONLY,
    FitDiagnostics,
    LogisticFit,
    fit_logistic,
    linear_score,
    sigmoid,
)
from hockey_model.game_model.win_interval import home_win_interval

Z80 = NormalDist().inv_cdf(0.9)


def _width(interval: tuple[float, float]) -> float:
    return interval[1] - interval[0]


def _fitted(n: int, seed: int = 0) -> LogisticFit:
    rows, wins = sample(n, seed)
    return fit_logistic(rows, wins, ["xg_form"])


def test_the_level_80_quantile_is_the_one_the_spec_names() -> None:
    assert Z80 == pytest.approx(1.2816, abs=1e-4)


def test_the_interval_matches_a_hand_worked_fit() -> None:
    """Elo only, scaled space, a diagonal covariance, so se is a short sum."""
    diagnostics = FitDiagnostics(
        iterations=1,
        objective=0.0,
        gradient_norm=0.0,
        condition=1.0,
        l2=1.0,
        means={"elo_logit": 0.5},
        scales={"elo_logit": 2.0},
        stop_reason="converged",
        covariance=((0.04, 0.0), (0.0, 0.09)),
    )
    fit = LogisticFit((), 0.2, 1.0, {}, diagnostics)
    interval = home_win_interval(fit, row(1.5), 0.8)
    # x = [1, (1.5 - 0.5) / 2] = [1, 0.5]; variance = 0.04 + 0.25 * 0.09 = 0.0625; se = 0.25.
    z = 0.2 + 1.0 * 1.5
    assert interval == pytest.approx((sigmoid(z - Z80 * 0.25), sigmoid(z + Z80 * 0.25)), abs=1e-12)


def test_a_fit_without_a_covariance_has_no_interval() -> None:
    assert home_win_interval(ELO_ONLY, row(0.3), 0.8) is None
    no_covariance = replace(_fitted(200), diagnostics=None)
    assert home_win_interval(no_covariance, row(0.3, xg_form=0.1), 0.8) is None


def test_the_stored_covariance_is_the_inverse_of_the_hessian_at_the_fit() -> None:
    """Build the Hessian again from the fit, apart from the code that made the covariance."""
    rows, wins = sample(300)
    fit = fit_logistic(rows, wins, ["xg_form"])
    diagnostics = fit.diagnostics
    assert diagnostics is not None and diagnostics.covariance is not None
    names = ("elo_logit", "xg_form")
    raw = [fit.elo_weight, fit.weights["xg_form"]]
    scaled_beta = [diagnostics.scales[n] * w for n, w in zip(names, raw, strict=True)]
    intercept = fit.intercept + sum(
        w * diagnostics.means[n] for n, w in zip(names, raw, strict=True)
    )
    beta = [intercept, *scaled_beta]
    size = len(beta)
    hessian = [[0.0] * size for _ in range(size)]
    for r in range(size):
        hessian[r][r] = 1.0 if r else 1e-6
    for item in rows:
        x = [
            1.0,
            (item.elo_logit - diagnostics.means["elo_logit"]) / diagnostics.scales["elo_logit"],
            (item.values["xg_form"] - diagnostics.means["xg_form"]) / diagnostics.scales["xg_form"],
        ]
        p = sigmoid(sum(b * v for b, v in zip(beta, x, strict=True)))
        for i in range(size):
            for j in range(size):
                hessian[i][j] += p * (1 - p) * x[i] * x[j]
    sigma = diagnostics.covariance
    for i in range(size):
        for j in range(size):
            product = sum(sigma[i][k] * hessian[k][j] for k in range(size))
            assert product == pytest.approx(1.0 if i == j else 0.0, abs=1e-6)


@settings(max_examples=40, deadline=None)
@given(
    elo=st.floats(min_value=-2.0, max_value=2.0),
    xg=st.floats(min_value=-0.5, max_value=0.5),
    level=st.floats(min_value=0.05, max_value=0.99),
)
def test_the_interval_contains_the_point_and_stays_inside_zero_and_one(
    elo: float, xg: float, level: float
) -> None:
    fit = _FIT_300
    low, high = _interval(fit, row(elo, xg_form=xg), level)
    point = sigmoid(linear_score(fit, row(elo, xg_form=xg)))
    assert 0.0 < low <= point <= high < 1.0


_FIT_300 = _fitted(300)


def _interval(fit: LogisticFit, item, level: float) -> tuple[float, float]:
    interval = home_win_interval(fit, item, level)
    assert interval is not None
    return interval


@settings(max_examples=30, deadline=None)
@given(low=st.floats(min_value=0.05, max_value=0.9), gap=st.floats(min_value=0.01, max_value=0.09))
def test_a_higher_level_gives_a_wider_interval(low: float, gap: float) -> None:
    item = row(0.2, xg_form=0.05)
    narrow = _interval(_FIT_300, item, low)
    wide = _interval(_FIT_300, item, low + gap)
    assert _width(wide) >= _width(narrow)


def test_the_width_falls_as_the_rows_grow() -> None:
    item = row(0.2, xg_form=0.05)
    widths = [_width(_interval(_fitted(n, seed=3), item, 0.8)) for n in (100, 400, 1600)]
    assert widths[0] > widths[1] > widths[2]


def test_a_refit_on_bootstrap_samples_agrees_with_the_width_in_order_of_size() -> None:
    """The spread of refits on resampled rows has the size of the delta-method se."""
    rows, wins = sample(400, seed=5)
    item = row(0.2, xg_form=0.05)
    fit = fit_logistic(rows, wins, ["xg_form"])
    low, high = _interval(fit, item, 0.8)
    se = (math.log(high / (1 - high)) - math.log(low / (1 - low))) / (2 * Z80)
    rng = random.Random(11)
    scores = []
    for _ in range(80):
        pick = [rng.randrange(len(rows)) for _ in rows]
        refit = fit_logistic([rows[i] for i in pick], [wins[i] for i in pick], ["xg_form"])
        scores.append(linear_score(refit, item))
    mean = sum(scores) / len(scores)
    spread = math.sqrt(sum((s - mean) ** 2 for s in scores) / (len(scores) - 1))
    assert 0.5 < spread / se < 2.0


def test_a_covariance_that_gives_a_negative_variance_is_refused() -> None:
    """A negative diagonal gives a negative quadratic form, which no real fit can write."""
    fit = _fitted(200)
    assert fit.diagnostics is not None
    indefinite = ((-1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, -1.0))
    bad = replace(fit, diagnostics=replace(fit.diagnostics, covariance=indefinite))
    with pytest.raises(ValueError, match="variance of .* below zero.*run `hockey-model fit` again"):
        home_win_interval(bad, row(1.0, xg_form=1.0), 0.8)
