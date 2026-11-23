"""Tests that the mutation workflow agrees with scripts/report-mutation.sh.

The workflow is not run in a test. These tests read it as text and check the rules that a
text read can hold: which refs each job runs on, which job holds the issue token, and what
the report step passes to the script.
"""

import re
from pathlib import Path

import pytest
from mutation_copy import require_path

WORKFLOW = require_path(
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "mutation.yml"
)


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _job(name: str) -> str:
    """Return the text of one job, from its header to the next job header."""
    text = _text()
    start = text.index(f"\n  {name}:\n")
    rest = text[start + 1 :]
    following = re.search(r"\n  [a-z][a-z0-9_-]*:\n", rest[1:])
    return rest if following is None else rest[: following.start() + 1]


def test_no_group_holds_the_gate_back() -> None:
    """A group on the workflow holds the gate job, so no run ever stands aside (#296)."""
    assert not re.search(r"^concurrency:", _text(), re.MULTILINE)


def test_a_scheduled_run_stands_aside_while_a_manual_run_waits() -> None:
    """A newer scheduled run would replace a waiting manual run and cancel it (#296)."""
    gate = _job("gate")
    assert "concurrency:" not in gate
    assert '[[ "$EVENT" != "schedule" ]]' in gate
    assert "scripts/manual-prepare-pending.sh mutation.yml" in gate
    assert 'echo "go=false" >> "$GITHUB_OUTPUT"' in gate
    assert "needs: gate\n    if: needs.gate.outputs.go == 'true'" in _job("shard")


def test_only_the_file_job_joins_a_group() -> None:
    """The script lists the open reports and then creates one, so two file jobs can both
    create an issue. The group serializes them. Every other job stays out of a group, so
    the gate still stands aside for a waiting manual run (#296).
    """
    assert "concurrency:" in _job("file")
    assert "cancel-in-progress: false" in _job("file")
    for name in ("gate", "shard", "merge", "report"):
        assert "concurrency:" not in _job(name), name


def test_the_report_job_runs_on_every_ref() -> None:
    # A manual run on a branch must judge its own stats, so a broken stats file cannot pass.
    report = _job("report")
    assert "github.ref == 'refs/heads/main'" not in report


def test_only_the_file_job_runs_on_main() -> None:
    file_job = _job("file")
    assert "if: always() && needs.gate.outputs.go == 'true'" in file_job
    assert "github.ref == 'refs/heads/main'" in file_job


@pytest.mark.parametrize("name", ["merge", "report", "file"])
def test_a_gate_that_stood_aside_stops_the_jobs_after_it(name: str) -> None:
    """A stood-aside run did no work, so it must file no report and no issue (#296).

    A skipped job fails the job that needs it, and only an `if` with `always()` runs anyway.
    Every job after the gate therefore reads the gate output in that same `if`.
    """
    job = _job(name)
    assert "needs.gate.outputs.go == 'true'" in job
    assert job.index("always()") < job.index("needs.gate.outputs.go == 'true'")
    assert "needs: gate" in job or "[gate," in job


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


def _job_env(name: str) -> str:
    """Return the job-level env block, or an empty string when the job has none."""
    header = _job(name).split("\n    steps:\n")[0]
    if "\n    env:\n" not in header:
        return ""
    return header.split("\n    env:\n")[1].split("\n    [a-z]")[0]


@pytest.mark.parametrize("name", ["report", "file"])
def test_the_job_env_reads_no_step_context(name: str) -> None:
    # GitHub allows the steps context in a step's env, and in a job's outputs, but not in a
    # job's env. A job-level use stops the workflow before any job starts.
    assert "steps." not in _job_env(name)


def test_the_file_job_files_the_decision_of_the_report_job() -> None:
    # Two downloads and two decisions can disagree. The file job reads the status instead.
    file_job = _job("file")
    assert "needs.report.outputs.status" in file_job
    assert "--status" in file_job
    assert "--stats" not in file_job


def test_the_report_job_publishes_its_status_and_its_body() -> None:
    report = _job("report")
    assert "status: ${{ steps.decide.outputs.status }}" in report
    assert "mutation-report.md" in report


def test_the_timeout_list_has_its_own_step_that_cannot_fail_the_export() -> None:
    """The list is advisory, so its failure must not reach EXPORT_RESULT (#274)."""
    merge = _job("merge")
    export = merge[
        merge.index("- name: Export results") : merge.index("- name: List the timed-out")
    ]
    assert "mutation_timeouts.py" not in export
    step = merge[
        merge.index("- name: List the timed-out") : merge.index("- name: Note a failed list")
    ]
    assert "scripts/mutation_timeouts.py --out mutants/mutmut-timeouts.json" in step
    assert "continue-on-error: true" in step
    assert "if: always()" in step
    assert "id: timeouts" in step


def test_the_merge_job_uploads_the_timeout_list_beside_the_stats() -> None:
    upload = _job("merge")
    upload = upload[upload.index("actions/upload-artifact") :]
    assert "mutants/mutmut-timeouts.json" in upload
    assert "mutants/mutmut-cicd-stats.json" in upload
