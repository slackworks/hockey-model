"""Tests that a hook environment cannot point a test's git command at the real repository.

A git hook runs with variables such as ``GIT_DIR`` and ``GIT_INDEX_FILE`` set. A test that ran
``git add`` or ``git config`` in a temporary repository then changed the index and the config of
the real repository, and the pre-push hook failed on a tree that looked dirty (#323).
"""

import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from git_env import clear_git_env
from hypothesis import given
from hypothesis import strategies as st

TESTS_DIR = Path(__file__).resolve().parent


@pytest.mark.parametrize(
    "name",
    ["GIT_DIR", "GIT_INDEX_FILE", "GIT_CONFIG", "GIT_CONFIG_KEY_0", "GIT_AUTHOR_NAME", "GIT_X"],
)
def test_a_git_variable_is_cleared(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv(name, "/elsewhere")
    clear_git_env()
    assert name not in os.environ


@pytest.mark.parametrize("name", ["GITHUB_TOKEN", "GITLAB_CI", "HOME", "PATH"])
def test_a_variable_outside_the_git_prefix_stays(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "kept")
    clear_git_env()
    assert os.environ[name] == "kept"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def test_a_repository_in_a_temporary_folder_leaves_the_hook_repository_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook_repo = tmp_path / "hook"
    own_repo = tmp_path / "own"
    for repo in (hook_repo, own_repo):
        repo.mkdir()
    _git(hook_repo, "init", "-q")
    git_dir = hook_repo / ".git"
    monkeypatch.setenv("GIT_DIR", str(git_dir))
    monkeypatch.setenv("GIT_INDEX_FILE", str(git_dir / "index"))
    monkeypatch.setenv("GIT_CONFIG", str(git_dir / "config"))
    clear_git_env()

    _git(own_repo, "init", "-q")
    _git(own_repo, "config", "user.email", "own@example.com")
    (own_repo / "file.txt").write_text("x\n", encoding="utf-8")
    _git(own_repo, "add", "-A")

    assert "own@example.com" not in (git_dir / "config").read_text(encoding="utf-8")
    assert not (git_dir / "index").exists()


def test_a_pytest_run_under_a_hook_environment_starts_without_git_variables(
    tmp_path: Path,
) -> None:
    """Load tests/conftest.py as a plugin in a child run that a hook-like environment starts."""
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "import os\n\n\ndef test_clean():\n"
        "    assert [name for name in os.environ if name.startswith('GIT_')] == []\n",
        encoding="utf-8",
    )
    env = os.environ | {
        "GIT_DIR": "/elsewhere/.git",
        "GIT_INDEX_FILE": "/elsewhere/.git/index",
        "PYTHONPATH": str(TESTS_DIR),
    }
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "conftest"]
    result = subprocess.run(
        [*command, "--rootdir", str(tmp_path), str(probe)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


_NAMES = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126, exclude_characters="="),
    min_size=1,
    max_size=10,
)
_ENVIRONMENTS = st.dictionaries(
    st.one_of(_NAMES, _NAMES.map(lambda name: f"GIT_{name}")),
    st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=8),
    max_size=8,
)


@given(_ENVIRONMENTS)
def test_the_clear_keeps_exactly_the_names_without_the_prefix_and_is_idempotent(
    environment: dict[str, str],
) -> None:
    kept = {name: value for name, value in environment.items() if not name.startswith("GIT_")}
    with mock.patch.dict(os.environ, environment, clear=True):
        clear_git_env()
        once = dict(os.environ)
        clear_git_env()
        assert dict(os.environ) == once
    assert once == kept
