"""Tests for scripts/ci-changes.sh and the job gates of ci.yml (#170)."""

import re
import subprocess
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mutation_copy import require_path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = require_path(ROOT / "scripts" / "ci-changes.sh")
CI = require_path(ROOT / ".github" / "workflows" / "ci.yml")


def decide(paths: list[str]) -> subprocess.CompletedProcess[str]:
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


@pytest.mark.parametrize("name", ["validation", "test"])
def test_the_code_jobs_wait_for_the_changes_job_and_its_answer(name: str) -> None:
    job = _job(name)
    assert re.search(r"needs: (changes|\[changes, )", job)
    assert "if: needs.changes.outputs.code == 'true'" in job


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
