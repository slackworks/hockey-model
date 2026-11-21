"""The check output says when the deletion history was not checked (#292)."""

import argparse
import subprocess
from pathlib import Path

import pytest
from experiment_support import register

from hockey_model.experiments.check import check_all
from hockey_model.experiments.command import run_experiments_command
from hockey_model.experiments.history import history_unavailable_reason
from hockey_model.experiments.models import validate_against_schema
from hockey_model.experiments.schemas import CHECK_OUTPUT_SCHEMA
from hockey_model.types import HockeyModelError


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)


def _init(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Fixture")
    _git(root, "config", "user.email", "fixture@example.com")
    _git(root, "config", "commit.gpgsign", "false")


def test_a_plain_directory_says_that_the_deletion_history_was_not_checked(
    registry_dir: Path,
) -> None:
    register(registry_dir)
    output = check_all(repo_root=registry_dir.parent)
    assert output["ok"] is True
    assert output["notes"] == ["deletion history not checked: not a git work tree"]


def test_a_repository_with_no_commit_says_so(registry_dir: Path) -> None:
    _init(registry_dir.parent)
    register(registry_dir)
    assert check_all(repo_root=registry_dir.parent)["notes"] == [
        "deletion history not checked: the repository has no commit yet"
    ]


def test_a_repository_with_a_commit_has_no_note(registry_dir: Path) -> None:
    _init(registry_dir.parent)
    register(registry_dir)
    _git(registry_dir.parent, "add", "-A", "experiments")
    _git(registry_dir.parent, "commit", "-q", "-m", "register")
    output = check_all(repo_root=registry_dir.parent)
    assert output["notes"] == []
    assert history_unavailable_reason(registry_dir.parent) is None


def test_the_output_schema_needs_the_notes_list(registry_dir: Path) -> None:
    output = dict(check_all(repo_root=registry_dir.parent))
    validate_against_schema(output, CHECK_OUTPUT_SCHEMA, "CHECK_OUTPUT_SCHEMA")
    del output["notes"]
    with pytest.raises(HockeyModelError, match="notes"):
        validate_against_schema(output, CHECK_OUTPUT_SCHEMA, "CHECK_OUTPUT_SCHEMA")


def test_a_shallow_repository_says_so(registry_dir: Path) -> None:
    repo = registry_dir.parent
    _init(repo)
    register(registry_dir)
    _git(repo, "add", "-A", "experiments")
    _git(repo, "commit", "-q", "-m", "register")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    # A shallow repository lists its cut commits in this file.
    (repo / ".git" / "shallow").write_text(head + "\n", encoding="utf-8")
    assert history_unavailable_reason(repo) == "the repository is a shallow clone"


def test_the_command_prints_each_note_on_standard_error(
    registry_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    register(registry_dir)
    monkeypatch.chdir(registry_dir.parent)
    payload, code = run_experiments_command(argparse.Namespace(experiment_command="check"))
    assert code == 0
    assert isinstance(payload, dict)
    assert payload["notes"] == ["deletion history not checked: not a git work tree"]
    assert capsys.readouterr().err == (
        "experiments check: deletion history not checked: not a git work tree\n"
    )
