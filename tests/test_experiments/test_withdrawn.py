"""Tests for the withdrawn status of an experiment record (#200).

A withdrawn record keeps its place in the history, spends no look, holds no result, and never
returns to another status.
"""

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from experiment_support import make_record, register

from hockey_model.experiments.check import check_all
from hockey_model.experiments.history import check_withdrawn_stays
from hockey_model.experiments.looks import look_problem
from hockey_model.experiments.models import (
    STATUS_REGISTERED,
    STATUS_WITHDRAWN,
    VALID_STATUSES,
    ExperimentRecord,
)
from hockey_model.experiments.schemas import EXPERIMENT_SCHEMA
from hockey_model.types import HockeyModelError

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
    _git(root, "init")
    _git(root, "config", "user.name", "Fixture")
    _git(root, "config", "user.email", "fixture@example.com")
    _git(root, "config", "commit.gpgsign", "false")


def _commit(root: Path, record: ExperimentRecord) -> None:
    record.save(root / "experiments" / f"{record.id}.json")
    _git(root, "add", "experiments")
    _git(root, "commit", "--allow-empty", "-m", f"state {record.status}")


def test_withdrawn_is_a_valid_status_in_the_code_and_in_the_schema() -> None:
    assert STATUS_WITHDRAWN in VALID_STATUSES
    assert sorted(EXPERIMENT_SCHEMA["properties"]["status"]["enum"]) == sorted(VALID_STATUSES)


def test_a_withdrawn_record_spends_no_look() -> None:
    withdrawn = make_record({"id": "0001-first", "status": "withdrawn"})
    second = make_record({"id": "0002-second", "look_number": 1})
    assert look_problem(second, earlier=[withdrawn]) is None
    wrong = make_record({"id": "0002-second", "look_number": 2})
    problem = look_problem(wrong, earlier=[withdrawn])
    assert problem is not None and "expected 1" in problem


def test_a_registered_record_still_spends_a_look() -> None:
    first = make_record({"id": "0001-first"})
    problem = look_problem(make_record({"id": "0002-second", "look_number": 1}), earlier=[first])
    assert problem is not None and "expected 2" in problem


def test_a_withdrawn_record_without_a_result_passes_the_check(registry_dir: Path) -> None:
    register(registry_dir, {"status": "withdrawn"})
    assert check_all(repo_root=registry_dir.parent)["ok"] is True


def test_a_withdrawn_record_with_a_result_fails_the_check(registry_dir: Path) -> None:
    register(registry_dir, {"status": "withdrawn", "result": RESULT})
    result = check_all(repo_root=registry_dir.parent)
    assert result["ok"] is False
    assert "'withdrawn' but the record holds a result" in result["errors"][0]


def test_a_withdrawn_record_cannot_return_to_another_status(tmp_path: Path) -> None:
    (tmp_path / "experiments").mkdir()
    _init_repo(tmp_path)
    registered = make_record()
    withdrawn = replace(registered, status=STATUS_WITHDRAWN)
    _commit(tmp_path, registered)
    _commit(tmp_path, withdrawn)
    check_withdrawn_stays(withdrawn, tmp_path)
    _commit(tmp_path, replace(registered, status=STATUS_REGISTERED))
    with pytest.raises(HockeyModelError, match="withdrawn.*cannot return"):
        check_withdrawn_stays(registered, tmp_path)


def test_a_record_that_was_never_withdrawn_passes_the_history_check(tmp_path: Path) -> None:
    (tmp_path / "experiments").mkdir()
    _init_repo(tmp_path)
    record = make_record()
    _commit(tmp_path, record)
    check_withdrawn_stays(record, tmp_path)


def test_the_check_reports_a_record_that_left_the_withdrawn_status(tmp_path: Path) -> None:
    root = tmp_path
    registry = root / "experiments"
    registry.mkdir()
    _init_repo(root)
    registered = make_record()
    _commit(root, registered)
    _commit(root, replace(registered, status=STATUS_WITHDRAWN))
    _commit(root, replace(registered, status="inconclusive"))
    result = check_all(repo_root=root)
    assert result["ok"] is False
    assert "cannot return" in result["errors"][0]
