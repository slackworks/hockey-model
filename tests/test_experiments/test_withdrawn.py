"""Tests for the withdrawn status of an experiment record (#200).

A withdrawn record keeps its place in the history, spends no look, holds no result, never runs,
and never returns to another status. Only a registered record can be withdrawn.
"""

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from experiment_support import make_record, register
from hypothesis import given
from hypothesis import strategies as st
from mutation_copy import require_path

from hockey_model.experiments.check import check_all, run_experiment
from hockey_model.experiments.history import check_one_shot
from hockey_model.experiments.looks import look_problem
from hockey_model.experiments.models import (
    STATUS_INCONCLUSIVE,
    STATUS_PROMOTED,
    STATUS_REGISTERED,
    STATUS_REJECTED,
    STATUS_WITHDRAWN,
    VALID_STATUSES,
    ExperimentRecord,
)
from hockey_model.experiments.registry import get_record
from hockey_model.experiments.withdrawn import check_withdrawn, status_problem
from hockey_model.types import HockeyModelError

COHORTS = ["confirmation", "exploration"]
RESULT = {
    "games": 20,
    "difference": -0.001,
    "interval": [-0.002, -0.0005],
    "csv_path": "experiments/results/0001-valid-games.csv",
    "csv_hash": "0" * 64,
}


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)


def _init_repo(root: Path) -> None:
    (root / "experiments").mkdir(exist_ok=True)
    _git(root, "init")
    _git(root, "config", "user.name", "Fixture")
    _git(root, "config", "user.email", "fixture@example.com")
    _git(root, "config", "commit.gpgsign", "false")


def _commit(root: Path, record: ExperimentRecord) -> None:
    record.save(root / "experiments" / f"{record.id}.json")
    _git(root, "add", "experiments")
    _git(root, "commit", "--allow-empty", "-m", f"state {record.status}")


def test_withdrawn_is_a_valid_status() -> None:
    assert STATUS_WITHDRAWN in VALID_STATUSES


def test_a_withdrawn_record_spends_no_look() -> None:
    withdrawn = make_record({"id": "0001-first", "status": "withdrawn"})
    second = make_record({"id": "0002-second", "look_number": 1})
    assert look_problem(second, earlier=[withdrawn]) is None
    wrong = make_record({"id": "0002-second", "look_number": 2})
    problem = look_problem(wrong, earlier=[withdrawn])
    assert problem is not None and "expected 1" in problem
    assert "Withdrawn records spend no look (0001-first)" in problem


def test_a_registered_record_still_spends_a_look() -> None:
    first = make_record({"id": "0001-first"})
    problem = look_problem(make_record({"id": "0002-second", "look_number": 1}), earlier=[first])
    assert problem is not None and "expected 2" in problem
    assert "Withdrawn records" not in problem


@given(
    st.lists(
        st.tuples(st.sampled_from(COHORTS), st.sampled_from(VALID_STATUSES)),
        max_size=6,
    ),
    st.integers(min_value=1, max_value=8),
)
def test_the_look_count_is_the_confirmation_records_that_are_not_withdrawn(
    earlier: list[tuple[str, str]], look: int
) -> None:
    records = [
        make_record({"id": f"{n:04d}-e", "cohort": cohort, "status": status})
        for n, (cohort, status) in enumerate(earlier, start=1)
    ]
    spent = [c == "confirmation" and s != STATUS_WITHDRAWN for c, s in earlier]
    expected = 1 + sum(spent)
    problem = look_problem(make_record({"id": "0099-now", "look_number": look}), records)
    assert (problem is None) == (look == expected)


_NEEDS_RESULT = {STATUS_PROMOTED, STATUS_REJECTED}
_FORBIDS_RESULT = {STATUS_REGISTERED, STATUS_WITHDRAWN}


@pytest.mark.parametrize("has_result", [False, True])
@pytest.mark.parametrize("status", VALID_STATUSES)
def test_the_status_and_the_result_agree_only_in_the_allowed_pairs(
    registry_dir: Path, status: str, has_result: bool
) -> None:
    register(registry_dir, {"status": status} | ({"result": RESULT} if has_result else {}))
    errors = check_all(repo_root=registry_dir.parent)["errors"]
    mismatch = (status in _FORBIDS_RESULT and has_result) or (
        status in _NEEDS_RESULT and not has_result
    )
    assert any("status is" in error for error in errors) == mismatch
    if status == STATUS_INCONCLUSIVE:
        assert not mismatch


def test_a_withdrawn_record_with_a_result_names_its_status(registry_dir: Path) -> None:
    register(registry_dir, {"status": "withdrawn", "result": RESULT})
    errors = check_all(repo_root=registry_dir.parent)["errors"]
    assert "status is 'withdrawn' but the record holds a result" in errors[0]


_ALLOWED_BEFORE_WITHDRAWAL = {STATUS_REGISTERED, STATUS_WITHDRAWN}


@given(st.lists(st.sampled_from(VALID_STATUSES), max_size=6), st.sampled_from(VALID_STATUSES))
def test_the_status_history_breaks_a_rule_exactly_when_the_rules_say(
    statuses: list[str], head: str
) -> None:
    history = [(f"commit{n}", status) for n, status in enumerate(statuses)]
    returned = head != STATUS_WITHDRAWN and STATUS_WITHDRAWN in statuses
    from_a_run = head == STATUS_WITHDRAWN and not set(statuses) <= _ALLOWED_BEFORE_WITHDRAWAL
    assert (status_problem(history, head) is not None) == (returned or from_a_run)


def test_a_withdrawn_record_cannot_return_to_another_status(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    registered = make_record()
    withdrawn = replace(registered, status=STATUS_WITHDRAWN)
    _commit(tmp_path, registered)
    _commit(tmp_path, withdrawn)
    check_withdrawn(withdrawn, tmp_path)
    _commit(tmp_path, registered)
    with pytest.raises(HockeyModelError, match="withdrawn.*cannot return"):
        check_withdrawn(registered, tmp_path)


def test_a_record_that_ran_without_a_result_cannot_be_withdrawn(tmp_path: Path) -> None:
    """An inconclusive record with no result still ran, so its look is spent."""
    _init_repo(tmp_path)
    registered = make_record()
    _commit(tmp_path, registered)
    _commit(tmp_path, replace(registered, status=STATUS_INCONCLUSIVE))
    withdrawn = replace(registered, status=STATUS_WITHDRAWN)
    with pytest.raises(HockeyModelError, match="inconclusive.*only a registered record"):
        check_withdrawn(withdrawn, tmp_path)


def test_a_registered_record_can_be_withdrawn(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    registered = make_record()
    _commit(tmp_path, registered)
    check_withdrawn(replace(registered, status=STATUS_WITHDRAWN), tmp_path)


def test_a_record_that_was_never_withdrawn_passes_the_history_check(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    record = make_record()
    _commit(tmp_path, record)
    check_withdrawn(record, tmp_path)


def test_the_check_reports_a_record_that_left_the_withdrawn_status(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    registered = make_record()
    _commit(tmp_path, registered)
    _commit(tmp_path, replace(registered, status=STATUS_WITHDRAWN))
    _commit(tmp_path, replace(registered, status=STATUS_INCONCLUSIVE))
    result = check_all(repo_root=tmp_path)
    assert result["ok"] is False
    assert "cannot return" in result["errors"][0]


@pytest.mark.parametrize("committed", [False, True])
def test_a_withdrawn_record_with_a_results_file_is_refused(tmp_path: Path, committed: bool) -> None:
    """A run writes the results file, so the file shows that the games were seen."""
    _init_repo(tmp_path)
    registered = make_record()
    _commit(tmp_path, registered)
    results = tmp_path / "experiments" / "results"
    results.mkdir()
    csv = results / f"{registered.id}-games.csv"
    csv.write_text("game_id\n", encoding="utf-8")
    if committed:
        _git(tmp_path, "add", "experiments")
        _git(tmp_path, "commit", "-m", "results")
        csv.unlink()
    with pytest.raises(
        HockeyModelError, match=r"results/0001-valid-games\.csv exists or did exist"
    ):
        check_withdrawn(replace(registered, status=STATUS_WITHDRAWN), tmp_path)


def test_a_record_deleted_and_added_again_does_not_crash_the_history_checks(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    record = make_record()
    _commit(tmp_path, record)
    _git(tmp_path, "rm", "-q", f"experiments/{record.id}.json")
    _git(tmp_path, "commit", "-m", "delete")
    _commit(tmp_path, record)
    check_withdrawn(record, tmp_path)
    check_one_shot(record, tmp_path)


@pytest.mark.parametrize("cohort", ["confirmation", "exploration"])
def test_a_withdrawn_record_never_runs(registry_dir: Path, store, cohort: str) -> None:
    register(registry_dir, {"status": "withdrawn", "cohort": cohort})
    with pytest.raises(HockeyModelError, match="a withdrawn record never runs"):
        run_experiment("0001-valid", store, repo_root=registry_dir.parent)


def test_withdrawing_an_earlier_record_breaks_a_later_look_number(registry_dir: Path) -> None:
    """The later record counted the earlier one, so the check names the withdrawn record."""
    register(registry_dir, {"id": "0001-first"})
    register(registry_dir, {"id": "0002-second", "look_number": 2})
    register(registry_dir, {"id": "0001-first", "status": "withdrawn"})
    result = check_all(repo_root=registry_dir.parent)
    assert result["ok"] is False
    assert any("Withdrawn records spend no look (0001-first)" in e for e in result["errors"])


@pytest.mark.parametrize("name", ["0002_x.json", "0004-Forward.json", "0007.json", "x.json"])
def test_a_json_file_with_a_non_record_name_is_an_error(registry_dir: Path, name: str) -> None:
    """The look count skips such a file, so a renamed record would leave the count."""
    register(registry_dir)
    (registry_dir / name).write_text("{}", encoding="utf-8")
    result = check_all(repo_root=registry_dir.parent)
    assert result["ok"] is False
    assert any(error.startswith(f"{name}: ") for error in result["errors"])


def test_a_file_that_is_not_json_is_not_a_record_name_error(registry_dir: Path) -> None:
    register(registry_dir)
    (registry_dir / "notes.txt").write_text("x", encoding="utf-8")
    (registry_dir / "0001-valid.json.bak").write_text("{}", encoding="utf-8")
    assert check_all(repo_root=registry_dir.parent)["ok"] is True


def test_the_real_registry_replaces_0001_with_0002_as_look_one() -> None:
    root = require_path(Path(__file__).resolve().parents[2] / "experiments").parent
    first = get_record("0001-v1-forward-test", root)
    second = get_record("0002-v1-forward-test", root)
    assert first.status == STATUS_WITHDRAWN and first.result is None
    assert second.status == STATUS_REGISTERED and second.look_number == 1
    assert look_problem(second, earlier=[first]) is None
    assert "97.5th percentile" in second.promotion_criterion
