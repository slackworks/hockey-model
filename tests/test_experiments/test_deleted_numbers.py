"""A deleted confirmation record that never ran must not free its number (#291)."""

import subprocess
from pathlib import Path

import pytest
from experiment_support import register

from hockey_model.experiments.check import check_all
from hockey_model.experiments.history import deleted_record_ids

_SPENT = {
    "status": "rejected",
    "result": {
        "games": 20,
        "difference": 0.001,
        "interval": [0.0005, 0.002],
        "csv_path": "experiments/results/0002-ran-games.csv",
        "csv_hash": "0" * 64,
    },
}


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path, registry_dir: Path) -> Path:
    """Return a repository with an identity and no signing, so commits work anywhere."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Fixture")
    _git(tmp_path, "config", "user.email", "fixture@example.com")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    return tmp_path


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A", "experiments")
    _git(repo, "commit", "-q", "-m", message)


def _remove(repo: Path, name: str) -> None:
    _git(repo, "rm", "-q", f"experiments/{name}.json")
    _git(repo, "commit", "-q", "-m", f"remove {name}")


def _register_two(repo: Path, registry_dir: Path) -> None:
    """Register 0001, which never runs, and 0002, which holds a result."""
    register(registry_dir, {"id": "0001-unrun", "look_number": 1})
    register(registry_dir, _SPENT | {"id": "0002-ran", "look_number": 2})
    _commit(repo, "register two records")


def test_the_scenario_of_the_issue_stays_red_after_the_number_is_reused(
    repo: Path, registry_dir: Path
) -> None:
    _register_two(repo, registry_dir)
    _remove(repo, "0001-unrun")
    register(registry_dir, {"id": "0001-other", "look_number": 1})
    result = check_all(repo_root=repo)
    assert result["ok"] is False
    assert any(error.startswith("0001-unrun: tracked") for error in result["errors"])


def test_an_unrun_record_below_a_higher_confirmation_record_cannot_be_deleted(
    repo: Path, registry_dir: Path
) -> None:
    _register_two(repo, registry_dir)
    _remove(repo, "0001-unrun")
    assert deleted_record_ids(["0002-ran"], repo) == ["0001-unrun"]


def test_the_error_names_the_withdrawn_status_as_the_way_to_drop_a_record(
    repo: Path, registry_dir: Path
) -> None:
    _register_two(repo, registry_dir)
    _remove(repo, "0001-unrun")
    [error] = [e for e in check_all(repo_root=repo)["errors"] if e.startswith("0001-unrun")]
    assert "withdrawn" in error


def test_a_deletion_before_any_higher_record_existed_stays_allowed(
    repo: Path, registry_dir: Path
) -> None:
    """The real history holds this: 0001-test was deleted, and a higher record came later."""
    register(registry_dir, {"id": "0001-test", "look_number": 1})
    _commit(repo, "register the first record")
    _remove(repo, "0001-test")
    register(registry_dir, {"id": "0001-v1", "look_number": 1})
    register(registry_dir, _SPENT | {"id": "0002-ran", "look_number": 2})
    _commit(repo, "register later records")
    assert deleted_record_ids(["0001-v1", "0002-ran"], repo) == []


def test_the_highest_unrun_record_can_be_deleted(repo: Path, registry_dir: Path) -> None:
    register(registry_dir, {"id": "0001-first", "look_number": 1})
    register(registry_dir, {"id": "0002-draft", "look_number": 2})
    _commit(repo, "register two records")
    _remove(repo, "0002-draft")
    assert deleted_record_ids(["0001-first"], repo) == []


def test_an_exploration_record_does_not_hold_a_number(repo: Path, registry_dir: Path) -> None:
    register(registry_dir, {"id": "0001-unrun", "look_number": 1})
    register(registry_dir, {"id": "0002-explore", "cohort": "exploration", "look_number": None})
    _commit(repo, "register a confirmation and an exploration record")
    _remove(repo, "0001-unrun")
    assert deleted_record_ids(["0002-explore"], repo) == []


def test_a_deleted_exploration_record_is_no_loss(repo: Path, registry_dir: Path) -> None:
    register(registry_dir, {"id": "0001-explore", "cohort": "exploration", "look_number": None})
    register(registry_dir, {"id": "0002-ran", "look_number": 1} | _SPENT)
    _commit(repo, "register two records")
    _remove(repo, "0001-explore")
    assert deleted_record_ids(["0002-ran"], repo) == []


def test_a_name_that_is_not_a_record_id_is_not_read(repo: Path, registry_dir: Path) -> None:
    (registry_dir / "notes.json").write_text("{}\n", encoding="utf-8")
    register(registry_dir, {"id": "0002-ran", "look_number": 1} | _SPENT)
    _commit(repo, "add a stray file")
    _remove(repo, "notes")
    assert deleted_record_ids(["0002-ran"], repo) == []


def test_the_registry_of_this_repository_passes() -> None:
    root = Path(__file__).resolve().parents[2]
    if not (root / ".git").exists():
        pytest.skip("the mutation copy has no git history")
    assert check_all(repo_root=root)["ok"] is True
