"""Tests for the rule that ties a confirmation status to the stored result (#312).

`experiments check` recomputes the status that a stored interval earns. A commit that
edits `rejected` to `promoted` and keeps the same result must fail the check.
"""

from pathlib import Path

import pytest
from experiment_support import make_record, register
from hypothesis import given
from hypothesis import strategies as st

from hockey_model.experiments.check import check_all
from hockey_model.experiments.models import (
    RESULT_STATUSES,
    STATUS_INCONCLUSIVE,
    STATUS_REGISTERED,
    HockeyModelError,
)
from hockey_model.experiments.status import (
    inconclusive_notes,
    status_for_interval,
    status_problem,
)

# An interval whose upper end is below zero promotes.
_PROMOTES = (-0.002, -0.0005)
# An interval whose lower end is above zero rejects.
_REJECTS = (0.0005, 0.002)
# An interval that straddles zero rejects.
_STRADDLES = (-0.001, 0.002)

_INTERVALS = st.tuples(
    st.floats(min_value=-1e6, max_value=1e6),
    st.floats(min_value=-1e6, max_value=1e6),
)
# An interval whose ends are ordered decides a status; one with no width or swapped ends does not.
_ORDERED = _INTERVALS.filter(lambda pair: pair[0] < pair[1])
_EFFECTS = st.none() | st.floats(min_value=-10.0, max_value=10.0)
_STATUSES = st.sampled_from([STATUS_REGISTERED, *RESULT_STATUSES])


def _result(interval: tuple[float, float]) -> dict:
    return {
        "games": 20,
        "difference": 0.001,
        "interval": list(interval),
        "csv_path": "experiments/results/0001-valid-games.csv",
        "csv_hash": "0" * 64,
    }


class TestTheStatusTheIntervalEarns:
    """Test the rule that both the run and the check use."""

    @pytest.mark.parametrize(
        ("interval", "expected"),
        [
            (_PROMOTES, "promoted"),
            (_REJECTS, "rejected"),
            (_STRADDLES, "rejected"),
            ((-0.001, 0.0), "rejected"),
        ],
    )
    def test_the_upper_end_decides(self, interval: tuple[float, float], expected: str):
        """A promotion needs the upper end strictly below zero."""
        record = make_record()
        assert status_for_interval(record, interval) == expected

    @pytest.mark.parametrize("interval", [(0.0, 0.0), (0.002, 0.001)])
    def test_an_interval_with_no_width_or_with_swapped_ends_is_refused(
        self, interval: tuple[float, float]
    ):
        """A swapped pair hides the power rule and promotes, so the rule refuses it."""
        record = make_record()
        with pytest.raises(HockeyModelError, match="lower end first"):
            status_for_interval(record, interval)

    def test_a_wide_interval_is_inconclusive(self):
        """The power rule turns a result into inconclusive when it is too wide."""
        record = make_record({"expected_effect": -0.001})
        assert status_for_interval(record, (-0.002, 0.002)) == "inconclusive"

    def test_a_narrow_enough_interval_is_not_inconclusive(self):
        """An interval narrower than the expected effect is narrow enough to decide."""
        record = make_record({"expected_effect": -0.001})
        assert status_for_interval(record, (-0.002, -0.0005)) == "promoted"

    def test_a_negative_expected_effect_uses_its_size(self):
        """The sign of the expected effect does not change the bar."""
        record = make_record({"expected_effect": 0.001})
        assert status_for_interval(record, (-0.002, 0.002)) == "inconclusive"


class TestTheStoredStatusMustMatchTheResult:
    """Test that the check recomputes the status of a record that holds a result."""

    def test_a_rejected_record_that_earns_promotion_fails(self, registry_dir: Path):
        """The cheapest way to fool the registry is a status edit, and it fails."""
        register(registry_dir, {"status": "rejected", "result": _result(_PROMOTES)})
        result = check_all(repo_root=registry_dir.parent)
        assert result["ok"] is False
        assert "'rejected'" in result["errors"][0]
        assert "'promoted'" in result["errors"][0]

    def test_a_promoted_record_that_earns_rejection_fails(self, registry_dir: Path):
        """A promotion needs the interval below zero, so an edit the other way fails too."""
        register(registry_dir, {"status": "promoted", "result": _result(_REJECTS)})
        result = check_all(repo_root=registry_dir.parent)
        assert result["ok"] is False
        assert "'promoted'" in result["errors"][0]
        assert "'rejected'" in result["errors"][0]

    def test_a_promoted_record_that_earns_inconclusive_fails(self, registry_dir: Path):
        """An underpowered result is inconclusive, so it cannot promote."""
        register(
            registry_dir,
            {
                "status": "promoted",
                "expected_effect": -0.001,
                "result": _result((-0.002, 0.002)),
            },
        )
        result = check_all(repo_root=registry_dir.parent)
        assert result["ok"] is False
        assert "'inconclusive'" in result["errors"][0]

    @pytest.mark.parametrize(
        ("status", "interval"),
        [("promoted", _PROMOTES), ("rejected", _REJECTS), ("rejected", _STRADDLES)],
    )
    def test_a_record_that_agrees_passes(
        self, registry_dir: Path, status: str, interval: tuple[float, float]
    ):
        """A record whose status is the one its result earns passes this rule."""
        register(registry_dir, {"status": status, "result": _result(interval)})
        errors = check_all(repo_root=registry_dir.parent)["errors"]
        assert not any("the stored status" in error for error in errors)

    def test_the_error_names_the_fix(self, registry_dir: Path):
        """The error says which status the result earns, so an author can correct it."""
        register(registry_dir, {"status": "rejected", "result": _result(_PROMOTES)})
        error = check_all(repo_root=registry_dir.parent)["errors"][0]
        assert "the stored status" in error
        assert "does not support" in error

    @pytest.mark.parametrize("status", ["promoted", "rejected", "inconclusive"])
    def test_an_exploration_record_cannot_claim_the_status_of_a_result(
        self, registry_dir: Path, status: str
    ):
        """Only a confirmation run decides a feature, so its exploration look earns no status.

        The result of an exploration run gates nothing, so a status beside it would read as a
        decision that no interval made.
        """
        register(
            registry_dir,
            {"cohort": "exploration", "status": status, "result": _result(_PROMOTES)},
        )
        errors = check_all(repo_root=registry_dir.parent)["errors"]
        assert any("exploration" in error for error in errors), errors


# The status rule is a comparison, so these hold for every interval and every status. Each
# is a module-level function, because Hypothesis refuses one method on two class instances
# and mutmut runs the same test twice in one process (stats, then clean).
@given(interval=_ORDERED, expected_effect=_EFFECTS, status=_STATUSES)
def test_a_confirmation_record_is_clean_exactly_when_it_agrees(
    interval: tuple[float, float],
    expected_effect: float | None,
    status: str,
) -> None:
    """The check is silent for a status the interval earns, and names it otherwise."""
    record = make_record(
        {"status": status, "expected_effect": expected_effect, "result": _result(interval)}
    )
    earned = status_for_interval(record, interval)
    problem = status_problem(record)
    assert (problem is None) is (status == earned), problem
    if problem is not None:
        assert repr(earned) in problem


@given(interval=_INTERVALS, status=_STATUSES)
def test_a_record_whose_interval_cannot_decide_is_never_clean(
    interval: tuple[float, float], status: str
) -> None:
    """A swapped or zero-width interval earns no status, so no stored status agrees."""
    if interval[0] < interval[1]:
        return
    record = make_record({"status": status, "result": _result(interval)})
    problem = status_problem(record)
    assert problem is not None
    assert "cannot decide a status" in problem


def test_a_confirmation_record_with_no_result_is_clean() -> None:
    """A record that has not run yet holds no result, so no interval contradicts it."""
    record = make_record({"status": "registered"})
    assert status_problem(record) is None


@given(status=_STATUSES)
def test_an_exploration_record_may_hold_no_status_that_a_result_earns(status: str) -> None:
    """Only a result status fails, and the note names the status to use instead."""
    record = make_record({"cohort": "exploration", "status": status, "result": _result(_PROMOTES)})
    problem = status_problem(record)
    assert (problem is None) is (status not in RESULT_STATUSES), problem
    if problem is not None:
        assert repr(status) in problem
        assert repr(STATUS_REGISTERED) in problem


# The note states the two numbers that made the run underpowered.
@given(interval=_ORDERED, expected_effect=_EFFECTS, status=_STATUSES)
def test_the_note_appears_only_for_an_inconclusive_result_with_an_effect(
    interval: tuple[float, float],
    expected_effect: float | None,
    status: str,
) -> None:
    record = make_record(
        {"status": status, "expected_effect": expected_effect, "result": _result(interval)}
    )
    notes = inconclusive_notes(record)
    quiet = status != STATUS_INCONCLUSIVE or expected_effect is None
    assert (notes == ()) is quiet, notes
    if quiet:
        return
    low, high = interval
    assert len(notes) == 1
    assert f"{(high - low) / 2:.4f}" in notes[0]
    assert expected_effect is not None
    assert f"{abs(expected_effect):.4f}" in notes[0]


# The promotion rule stated alone: the upper end decides. These do not call the function to
# learn the rule, so a change that breaks the rule fails even if the function agrees with itself.
@given(interval=_ORDERED, expected_effect=_EFFECTS)
def test_only_an_upper_end_below_zero_promotes(
    interval: tuple[float, float], expected_effect: float | None
) -> None:
    """A promotion means the interval lies below zero, so a high end at zero or above never does."""
    record = make_record({"expected_effect": expected_effect})
    status = status_for_interval(record, interval)
    if status == "promoted":
        assert interval[1] < 0.0, interval


@given(interval=_ORDERED, expected_effect=_EFFECTS, above=st.floats(min_value=0.0, max_value=1.0))
def test_a_decided_interval_keeps_its_status_when_its_low_end_moves_up(
    interval: tuple[float, float], expected_effect: float | None, above: float
) -> None:
    """Narrowing the interval from the low side cannot widen it, so the decision stands.

    The upper end is unchanged, and a narrower interval is never more than the bar that made
    it inconclusive, so a decided interval stays decided.
    """
    record = make_record({"expected_effect": expected_effect})
    low, high = interval
    narrow = (low + (high - low) * above / 2, high)
    decided = status_for_interval(record, interval)
    if decided == STATUS_INCONCLUSIVE:
        return
    assert status_for_interval(record, narrow) == decided


@given(expected_effect=_EFFECTS)
def test_a_record_with_no_result_gets_no_note(expected_effect: float | None) -> None:
    """A status alone cannot say the interval was wide, because there is no interval."""
    record = make_record({"status": STATUS_INCONCLUSIVE, "expected_effect": expected_effect})
    assert inconclusive_notes(record) == ()
