"""Tests for the git-history helpers of the experiment checker.

Covers:
- git failures: a missing binary and an unexpected git error are errors, not "untracked"
- deleted_record_ids in a repository with no commits and outside any repository
"""

import subprocess
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hockey_model.experiments import history
from hockey_model.experiments.history import (
    check_immutable_fields,
    check_one_shot,
    deleted_record_ids,
    registration_commit,
)
from hockey_model.experiments.models import ExperimentRecord
from hockey_model.types import HockeyModelError


def test_a_missing_git_binary_is_an_error(monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
    """Without git the checker says so, instead of reporting the record as untracked."""

    def no_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(history.subprocess, "run", no_git)
    with pytest.raises(HockeyModelError, match="git is not installed"):
        registration_commit("0001-valid", repo_root)


def test_an_unexpected_git_failure_is_an_error(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path
) -> None:
    """A git error that is not "not a repository" stops the check, for example a bad owner."""

    def broken(args, **kwargs):
        return subprocess.CompletedProcess(args, 128, "", "fatal: detected dubious ownership")

    monkeypatch.setattr(history.subprocess, "run", broken)
    with pytest.raises(HockeyModelError, match="dubious ownership"):
        registration_commit("0001-valid", repo_root)


def test_a_directory_outside_a_repository_has_no_registration_commit(repo_root: Path) -> None:
    """A plain directory is not a work tree, so the record is simply untracked."""
    assert registration_commit("0001-valid", repo_root) is None


def test_deleted_record_ids_is_empty_outside_a_repository(repo_root: Path) -> None:
    """With no repository, no record counts as deleted."""
    assert deleted_record_ids([], repo_root) == []


def test_deleted_record_ids_is_empty_before_the_first_commit(repo_root: Path) -> None:
    """A repository with no commits has nothing tracked at HEAD."""
    subprocess.run(["git", "init"], cwd=repo_root, capture_output=True, check=True)
    assert deleted_record_ids([], repo_root) == []


_ID = "0001-valid"
_RESULTS = (None, "a", "b", "c")
_BASE = {
    "id": _ID,
    "title": "Valid record",
    "hypothesis": "Feature X improves log loss.",
    "cohort": "confirmation",
    "data_cutoff": "2026-10-31",
    "primary_metric": "mean_per_game_log_loss",
    "baseline": {"fit_id": 1, "commit": "abc123"},
    "selection_folds": None,
    "min_games": 10,
    "candidate_features": ["xg_form"],
    "look_number": 1,
    "expected_effect": None,
    "promotion_criterion": "95th percentile below zero",
    "failure_outcome": "ship Elo alone",
    "status": "registered",
    "result": None,
}


def _git(root: Path, *args: str) -> None:
    identity = ["-c", "user.name=Fixture", "-c", "user.email=f@example.com"]
    subprocess.run(
        ["git", *identity, "-c", "commit.gpgsign=false", *args],
        cwd=root,
        capture_output=True,
        check=True,
    )


def _record(result: str | None = None, **changes: object) -> ExperimentRecord:
    data = {**_BASE, **changes}
    if result is not None:
        data["status"] = "rejected"
        data["result"] = {
            "games": 20,
            "difference": 0.001,
            "interval": [0.0, 0.002],
            "csv_path": f"experiments/results/{result}.csv",
            "csv_hash": "0" * 64,
        }
    return ExperimentRecord.from_dict(data)


def _commit(root: Path, record: ExperimentRecord) -> None:
    path = root / "experiments" / f"{_ID}.json"
    record.save(path)
    _git(root, "add", "experiments")
    _git(root, "commit", "--allow-empty", "-m", "state")


@settings(max_examples=25, deadline=None)
@given(st.lists(st.sampled_from(_RESULTS), min_size=1, max_size=5))
def test_one_shot_fails_exactly_when_a_committed_result_differs_from_head(
    tmp_path_factory: pytest.TempPathFactory, results: list[str | None]
) -> None:
    root = tmp_path_factory.mktemp("repo")
    (root / "experiments").mkdir()
    _git(root, "init")
    for result in results:
        _commit(root, _record(result))
    head = _record(results[-1])
    committed = {r for r in results if r is not None}
    differs = any(r != results[-1] for r in committed)
    if differs:
        with pytest.raises(HockeyModelError, match="one-shot"):
            check_one_shot(head, root)
    else:
        check_one_shot(head, root)


@settings(max_examples=15, deadline=None)
@given(
    title=st.booleans(),
    min_games=st.booleans(),
    status=st.booleans(),
)
def test_immutable_fields_fail_exactly_when_a_frozen_field_changed(
    tmp_path_factory: pytest.TempPathFactory, title: bool, min_games: bool, status: bool
) -> None:
    root = tmp_path_factory.mktemp("repo")
    (root / "experiments").mkdir()
    _git(root, "init")
    _commit(root, _record())
    commit = registration_commit(_ID, root)
    assert commit is not None
    changes: dict[str, object] = {}
    if title:
        changes["title"] = "Another title"
    if min_games:
        changes["min_games"] = 11
    current = _record(None, **changes)
    if status:
        current = _record("a", **changes)
    if title or min_games:
        with pytest.raises(HockeyModelError, match="immutable"):
            check_immutable_fields(current, commit, root)
    else:
        check_immutable_fields(current, commit, root)
