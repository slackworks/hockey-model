"""Tests that the mutation workflow agrees with scripts/report-mutation.sh.

The workflow is not run in a test. These tests read it as text and check the rules that a
text read can hold: which refs each job runs on, which job holds the issue token, and what
the report step passes to the script.
"""

import re
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "mutation.yml"


def _text() -> str:
    if "mutants" in Path(__file__).resolve().parts:
        pytest.skip("the mutation copy has no .github or scripts directory")
    return WORKFLOW.read_text(encoding="utf-8")


def _job(name: str) -> str:
    """Return the text of one job, from its header to the next job header."""
    text = _text()
    start = text.index(f"\n  {name}:\n")
    rest = text[start + 1 :]
    following = re.search(r"\n  [a-z][a-z0-9_-]*:\n", rest[1:])
    return rest if following is None else rest[: following.start() + 1]


def test_the_report_job_runs_on_every_ref() -> None:
    # A manual run on a branch must judge its own stats, so a broken stats file cannot pass.
    report = _job("report")
    assert "github.ref == 'refs/heads/main'" not in report


def test_only_the_file_job_runs_on_main() -> None:
    file_job = _job("file")
    assert "if: always() && github.ref == 'refs/heads/main'" in file_job


def test_only_the_file_job_holds_the_issue_token() -> None:
    # A branch run reaches no step that can write an issue.
    assert "issues: write" not in _job("report")
    assert "issues: write" in _job("file")


def test_the_report_step_names_every_job_result() -> None:
    # A failed shard, merge, or export must reach the body, not only the download.
    report = _job("report")
    for key in ("shard", "merge", "export", "download"):
        assert f"--{key}-result" in report


def test_the_report_step_shows_the_status_on_every_ref() -> None:
    # The decision runs on a branch, so the branch run must show its result somewhere.
    assert "GITHUB_STEP_SUMMARY" in _job("report")


@pytest.mark.parametrize("name", ["report", "file"])
def test_the_job_env_reads_no_step_context(name: str) -> None:
    # GitHub allows the steps context in a step's env only. A job-level use stops the
    # workflow before any job starts, and no lint step in this repo catches it.
    header = _job(name).split("\n    steps:\n")[0]
    assert "steps." not in header
