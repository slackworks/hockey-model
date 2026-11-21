"""The imputation share of fit rows, and the refusal above the frozen limit."""

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hockey_model.game import DegradedInput, MissingKind
from hockey_model.game_model.features import FeatureRow
from hockey_model.game_model.imputation import (
    IMPUTATION_LIMIT,
    ImputationLimitError,
    check_imputation,
    feature_quality,
    validate_limit,
)


def _row(*imputed: DegradedInput) -> FeatureRow:
    return FeatureRow(
        game_id=1,
        elo_logit=0.0,
        values={},
        missing=(),
        uses_moneypuck=False,
        imputed=imputed,
    )


XG = DegradedInput("xg_form", MissingKind.XG_FORM, ("TOR",))
REST = DegradedInput("rest", MissingKind.REST_UNKNOWN, ("TOR",))


def test_a_row_with_two_reasons_for_one_feature_counts_once() -> None:
    rows = [_row(XG, DegradedInput("xg_form", MissingKind.XG_FORM, ("MTL",)))]
    (quality,) = feature_quality(rows, ["xg_form"])
    assert (quality.imputed, quality.total) == (1, 1)


def test_a_measured_row_is_not_counted() -> None:
    rows = [_row(), _row(XG)]
    (quality,) = feature_quality(rows, ["xg_form"])
    assert (quality.imputed, quality.total, quality.share) == (1, 2, 0.5)


def test_only_selected_features_are_reported() -> None:
    rows = [_row(XG, REST)]
    names = [q.feature for q in feature_quality(rows, ["rest"])]
    assert names == ["rest"]


def test_an_empty_denominator_is_an_error_not_a_zero_share() -> None:
    with pytest.raises(ValueError, match="no rows"):
        feature_quality([], ["xg_form"])


def test_a_name_that_is_not_a_feature_is_an_error() -> None:
    with pytest.raises(ValueError, match="not a known feature"):
        feature_quality([_row()], ["no_such_feature"])


def test_a_share_at_the_limit_passes_and_above_it_is_refused() -> None:
    limit = 0.5
    at_limit = [_row(XG), _row()]
    assert check_imputation(at_limit, ["xg_form"], limit)[0].share == limit
    above = [_row(XG), _row(XG), _row()]
    with pytest.raises(ImputationLimitError, match="xg_form"):
        check_imputation(above, ["xg_form"], limit)


@pytest.mark.parametrize("bad", [0.0, -0.1, 1.5, math.nan, math.inf])
def test_the_limit_must_be_a_share_above_zero_and_at_most_one(bad: float) -> None:
    with pytest.raises(ValueError, match="imputation limit"):
        validate_limit(bad)


def test_the_frozen_limit_is_a_valid_share() -> None:
    assert validate_limit(IMPUTATION_LIMIT) == IMPUTATION_LIMIT


@given(st.floats(allow_nan=True, allow_infinity=True))
def test_the_limit_is_valid_exactly_when_it_is_a_share_above_zero(limit: float) -> None:
    valid = math.isfinite(limit) and 0.0 < limit <= 1.0
    if valid:
        assert validate_limit(limit) == limit
    else:
        with pytest.raises(ValueError, match="imputation limit"):
            validate_limit(limit)


_ROW_IMPUTED = st.lists(st.sampled_from(["xg_form", "rest", "goalie_gsax"]), max_size=3)


@given(st.lists(_ROW_IMPUTED, min_size=1, max_size=12))
def test_counts_match_a_direct_count_and_never_exceed_the_total(
    rows_imputed: list[list[str]],
) -> None:
    rows = [
        _row(*(DegradedInput(f, MissingKind.XG_FORM, ("TOR",)) for f in names))
        for names in rows_imputed
    ]
    for quality in feature_quality(rows, ["xg_form", "rest", "goalie_gsax"]):
        direct = sum(quality.feature in names for names in rows_imputed)
        assert quality.imputed == direct
        assert 0 <= quality.imputed <= quality.total == len(rows)
        assert quality.share == pytest.approx(direct / len(rows))


@given(
    st.lists(_ROW_IMPUTED, min_size=1, max_size=12),
    st.floats(0.01, 1.0),
)
def test_a_fit_is_refused_exactly_when_some_share_is_above_the_limit(
    rows_imputed: list[list[str]], limit: float
) -> None:
    rows = [
        _row(*(DegradedInput(f, MissingKind.XG_FORM, ("TOR",)) for f in names))
        for names in rows_imputed
    ]
    names = ["xg_form", "rest", "goalie_gsax"]
    over = any(
        sum(name in imputed for imputed in rows_imputed) / len(rows) > limit for name in names
    )
    if over:
        with pytest.raises(ImputationLimitError):
            check_imputation(rows, names, limit)
    else:
        assert len(check_imputation(rows, names, limit)) == len(names)
