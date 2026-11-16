"""Tests that the mutation workflow agrees with scripts/report-mutation.sh.

The workflow is not run in a test. These tests read it as text and check the two rules
that a text read can hold: which refs the decision runs on, and what the report step
passes to the script.
"""

import re
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "mutation.yml"
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "report-mutation.sh"


def _skip_in_mutation_copy() -> None:
    """Skip when the mutation run copied this test without the files it reads."""
    if "mutants" in Path(__file__).resolve().parts:
        pytest.skip("the mutation copy has no .github or scripts directory")


def _text() -> str:
    _skip_in_mutation_copy()
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
    _skip_in_mutation_copy()
    report = _job("report")
    assert "github.ref == 'refs/heads/main'" not in report


def test_only_the_issue_calls_skip_a_non_main_ref() -> None:
    _skip_in_mutation_copy()
    report = _job("report")
    # The script makes the issue calls. A branch run passes no flag, so it writes no issue.
    assert "--file-issue" in report
    assert '[[ "$GITHUB_REF" == refs/heads/main ]]' in report


def test_a_failed_download_reaches_the_report() -> None:
    # Without this, a failed download and a shard that wrote no file read the same.
    _skip_in_mutation_copy()
    report = _job("report")
    assert "id: download" in report
    assert "--download-outcome" in report


def test_the_job_env_reads_no_step_context() -> None:
    # GitHub allows the steps context in a step's env only. A job-level use fails actionlint,
    # and the workflow never starts.
    _skip_in_mutation_copy()
    header = _job("report").split("\n    steps:\n")[0]
    assert "steps." not in header
