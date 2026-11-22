"""Tests for the rule that ties a confirmation status to the stored result (#312).

`experiments check` recomputes the status that a stored interval earns. A commit that
edits `rejected` to `promoted` and keeps the same result must fail the check.
"""

from pathlib import Path

import pytest
from experiment_support import make_record, register

from hockey_model.experiments.check import check_all
from hockey_model.experiments.status import status_for_interval

# An interval whose upper end is below zero promotes.
_PROMOTES = (-0.002, -0.0005)
# An interval whose lower end is above zero rejects.
_REJECTS = (0.0005, 0.002)
# An interval that straddles zero rejects.
_STRADDLES = (-0.001, 0.002)


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
            ((0.0, 0.0), "rejected"),
        ],
    )
    def test_the_upper_end_decides(self, interval: tuple[float, float], expected: str):
        """A promotion needs the upper end strictly below zero."""
        record = make_record()
        assert status_for_interval(record, interval) == expected

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
