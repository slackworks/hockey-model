"""Tests that a hook environment cannot point a test's git command at the real repository.

A git hook runs with variables such as ``GIT_DIR`` and ``GIT_INDEX_FILE`` set. A test that runs
``git add`` or ``git rm`` in a temporary repository then changed the index of the real
repository, and the pre-push hook failed on a tree that looked dirty (#323).
"""

import os
import subprocess

import conftest
import pytest
from git_env import CONFIG_VARS, REPO_VARS, clear_repo_env


def test_every_variable_that_names_a_repository_is_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in REPO_VARS:
        monkeypatch.setenv(name, "/elsewhere")
    clear_repo_env()
    assert [name for name in REPO_VARS if name in os.environ] == []


def test_a_variable_that_does_not_name_a_repository_stays(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test Author")
    monkeypatch.setenv("GIT_DIR", "/elsewhere")
    clear_repo_env()
    assert os.environ["GIT_AUTHOR_NAME"] == "Test Author"


def test_the_pytest_start_clears_the_repository_variables(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    monkeypatch.setenv("GIT_INDEX_FILE", "/elsewhere/index")
    conftest.pytest_configure(request.config)
    assert "GIT_INDEX_FILE" not in os.environ


def test_the_lists_cover_every_local_variable_that_git_reports() -> None:
    listing = subprocess.run(
        ["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True, check=True
    ).stdout
    assert set(listing.split()) <= set(REPO_VARS) | set(CONFIG_VARS), (
        "git reports a local variable that tests/git_env.py names in neither list. "
        "Add it to REPO_VARS, or to CONFIG_VARS when it holds configuration only."
    )
