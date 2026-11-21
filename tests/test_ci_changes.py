"""Tests for scripts/ci-changes.sh and the job gates of ci.yml (#170)."""

import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mutation_copy import require_path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = require_path(ROOT / "scripts" / "ci-changes.sh")
CI = require_path(ROOT / ".github" / "workflows" / "ci.yml")


def decide(paths: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        input="".join(f"{p}\n" for p in paths),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "paths",
    [
        ["README.md"],
        ["docs/a.md"],
        ["docs/design/deep/nested/note.md", "CHANGELOG.md", "docs/superpowers/plans/p.md"],
    ],
)
def test_a_diff_of_markdown_only_skips_the_code_jobs(paths: list[str]) -> None:
    result = decide(paths)
    assert (result.returncode, result.stdout) == (0, "code=false\n")


@pytest.mark.parametrize(
    "path",
    [
        "src/x.py",
        "docs/x.py",
        "pyproject.toml",
        ".github/workflows/ci.yml",
        "docs/design/daily-prepare-tla.md",
        "README.md.bak",
        "notes.markdown",
    ],
)
def test_any_other_path_runs_the_code_jobs(path: str) -> None:
    result = decide(["README.md", path])
    assert (result.returncode, result.stdout) == (0, "code=true\n")
    assert path in result.stderr


@pytest.mark.parametrize("paths", [[], [""], ["", ""]])
def test_a_diff_that_lists_no_path_runs_the_code_jobs(paths: list[str]) -> None:
    result = decide(paths)
    assert (result.returncode, result.stdout) == (0, "code=true\n")
    assert "lists no path" in result.stderr


def test_a_last_path_with_no_newline_counts() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT)], input="README.md\nsrc/x.py", capture_output=True, text=True
    )
    assert result.stdout == "code=true\n"


_NAMES = st.text(alphabet="abcxyz_-", min_size=1, max_size=6)


@given(
    folders=st.lists(_NAMES, max_size=3),
    stem=_NAMES,
    suffix=st.sampled_from([".md", ".py", ".toml", ".yml", ".tla"]),
)
def test_a_path_is_docs_exactly_when_it_ends_in_md(
    folders: list[str], stem: str, suffix: str
) -> None:
    path = "/".join([*folders, stem + suffix])
    expected = "code=false\n" if suffix == ".md" else "code=true\n"
    assert decide([path]).stdout == expected


def test_a_bad_argument_exits_2_and_help_exits_0() -> None:
    bad = subprocess.run(["bash", str(SCRIPT), "--nope"], capture_output=True, text=True)
    assert bad.returncode == 2
    assert "unknown argument" in bad.stderr
    good = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
    assert good.returncode == 0
    assert "Usage:" in good.stdout


def _job(name: str) -> str:
    text = CI.read_text(encoding="utf-8")
    match = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z-]+:\n|\Z)", text, re.DOTALL | re.MULTILINE)
    assert match is not None, f"ci.yml has no job named {name}"
    return "".join(
        line
        for line in match.group(1).splitlines(keepends=True)
        if not line.lstrip().startswith("#")
    )


def test_validation_waits_for_the_changes_job_and_its_answer() -> None:
    job = _job("validation")
    assert "needs: changes\n" in job
    assert "if: needs.changes.outputs.code == 'true'" in job


def test_the_test_job_follows_validation_and_skips_with_it() -> None:
    """A skipped validation job skips the test job, so the test job needs no gate of its own."""
    job = _job("test")
    assert "needs: validation\n" in job
    assert "\n    if:" not in job


@pytest.mark.parametrize("name", ["editorconfig", "smoke", "zizmor"])
def test_the_other_jobs_always_run(name: str) -> None:
    job = _job(name)
    assert "needs:" not in job
    assert "\n    if:" not in job


def test_a_push_to_main_runs_every_code_job() -> None:
    job = _job("changes")
    assert '[ "$EVENT" = pull_request ]' in job
    assert 'echo "code=true" >> "$GITHUB_OUTPUT"' in job


def test_the_ci_workflow_has_no_path_filter() -> None:
    """A path filter leaves a required check pending, which blocks the merge."""
    assert not re.search(r"^\s*paths(-ignore)?:", CI.read_text(encoding="utf-8"), re.MULTILINE)


def test_the_ci_test_job_runs_pytest_in_parallel() -> None:
    assert "- run: uv run pytest -n auto\n" in _job("test")


def test_the_pre_push_coverage_gate_runs_pytest_in_parallel() -> None:
    hooks = require_path(ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "rm -f coverage.json && uv run pytest -n auto && " in hooks


def test_the_default_pytest_options_stay_serial() -> None:
    """mutmut runs pytest by itself and needs one process, so addopts holds no -n flag."""
    addopts = re.search(r'addopts = """(.*?)"""', (ROOT / "pyproject.toml").read_text(), re.S)
    assert addopts is not None
    assert not re.search(r"(^|\s)(-n|--numprocesses)\b", addopts.group(1))


def test_the_workflow_lists_the_paths_of_a_rename_on_both_sides() -> None:
    assert "git diff --name-only --no-renames" in _job("changes")


def test_a_code_file_renamed_to_markdown_is_a_code_change(tmp_path: Path) -> None:
    """Without --no-renames, git lists only the new path, which reads as docs only."""

    def git(*args: str) -> str:
        done = subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout

    git("init", "-q")
    (tmp_path / "x.py").write_text("print('a long enough file body for rename detection')\n" * 20)
    git("add", "x.py")
    git("-c", "commit.gpgsign=false", "commit", "-q", "-m", "base")
    base = git("rev-parse", "HEAD").strip()
    git("mv", "x.py", "x.md")
    git("-c", "commit.gpgsign=false", "commit", "-q", "-m", "rename")
    default = git("diff", "--name-only", base, "HEAD")
    assert default == "x.md\n"
    assert decide(default.splitlines()).stdout == "code=false\n"
    fixed = git("diff", "--name-only", "--no-renames", base, "HEAD")
    assert sorted(fixed.splitlines()) == ["x.md", "x.py"]
    assert decide(fixed.splitlines()).stdout == "code=true\n"


GATE = require_path(ROOT / "scripts" / "ci-gate.sh")


def gate(changes: str, code: str, validation: str, test: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "CHANGES_RESULT": changes,
        "CODE": code,
        "VALIDATION_RESULT": validation,
        "TEST_RESULT": test,
    }
    return subprocess.run(["bash", str(GATE)], env=env, capture_output=True, text=True)


@pytest.mark.parametrize(
    ("changes", "code", "validation", "test", "exit_code"),
    [
        ("success", "true", "success", "success", 0),
        ("success", "false", "skipped", "skipped", 0),
        ("success", "true", "failure", "skipped", 1),
        ("success", "true", "success", "failure", 1),
        ("success", "true", "skipped", "skipped", 1),
        ("success", "true", "cancelled", "skipped", 1),
        ("success", "false", "success", "success", 1),
        ("success", "false", "skipped", "success", 1),
        ("failure", "", "skipped", "skipped", 1),
        ("failure", "true", "success", "success", 1),
        ("cancelled", "", "skipped", "skipped", 1),
        ("skipped", "", "skipped", "skipped", 1),
        ("success", "", "skipped", "skipped", 1),
        ("success", "maybe", "success", "success", 1),
    ],
)
def test_the_gate_passes_only_when_the_changes_job_and_the_code_jobs_agree(
    changes: str, code: str, validation: str, test: str, exit_code: int
) -> None:
    result = gate(changes, code, validation, test)
    assert result.returncode == exit_code, result.stderr
    assert result.stdout == ""
    assert (result.stderr != "") == (exit_code != 0)


def test_a_missing_result_exits_2_and_help_exits_0() -> None:
    missing = subprocess.run(
        ["bash", str(GATE)], env={"PATH": os.environ["PATH"]}, capture_output=True, text=True
    )
    assert missing.returncode == 2 and "CHANGES_RESULT is not set" in missing.stderr
    helped = subprocess.run(["bash", str(GATE), "--help"], capture_output=True, text=True)
    assert helped.returncode == 0 and "Usage:" in helped.stdout


def test_the_result_job_runs_after_a_failure_and_reads_every_code_job() -> None:
    job = _job("ci-result")
    assert "needs: [changes, validation, test]" in job
    assert "if: always()" in job
    for variable in ("CHANGES_RESULT", "CODE", "VALIDATION_RESULT", "TEST_RESULT"):
        assert f"{variable}: ${{{{ needs." in job
    assert "run: bash scripts/ci-gate.sh" in job
