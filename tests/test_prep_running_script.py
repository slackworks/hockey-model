"""Tests for the lookup that names the prepare run holding the store group."""

import json
import re
from pathlib import Path

import pytest
from report_script_support import _calls, _env, _run

DAY = "2026-11-04"
_PREPARE = ".github/workflows/daily-prepare.yml"
_REPORT = ".github/workflows/daily-report.yml"
_STARTED = "2026-11-04T08:07:12Z"
# A queued run carries run_started_at, and the value is its creation time. The lookup must
# not print it, because that instant reads as a run that ends early.
_CREATED = "2026-11-04T07:55:00Z"
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _runs(*runs: tuple[str, str | None], status: str = "in_progress") -> str:
    """Return a workflow run list payload in the shape that the API returns for ``status``.

    A running run names its start in ``run_started_at``. A run that the group holds, or one
    that waits for a runner, holds its creation time there. No run object has ``started_at``.
    """
    rows = []
    for path, started in runs:
        row = {"path": path, "started_at": None, "created_at": _CREATED}
        if status == "in_progress" and started is not None:
            row["run_started_at"] = started
        else:
            row["run_started_at"] = _CREATED
        rows.append(row)
    return json.dumps({"workflow_runs": rows})


def _env_for(
    fakes: Path, tmp_path: Path, status: str, *runs: tuple[str, str | None], **extra: str
) -> dict[str, str]:
    """Return an environment whose fake gh answers ``runs`` for one status only."""
    key = f"FAKE_RUNS_JSON_{status.upper()}"
    return _env(fakes, tmp_path, **{key: _runs(*runs, status=status)}, **extra)


def test_no_runs_means_no_prepare_holds_the_group(fakes: Path, tmp_path: Path) -> None:
    result = _run("prep-running.sh", _env(fakes, tmp_path))
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_a_running_prepare_names_its_start(fakes: Path, tmp_path: Path) -> None:
    env = _env_for(fakes, tmp_path, "in_progress", (_PREPARE, _STARTED))
    result = _run("prep-running.sh", env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == _STARTED


def test_a_run_with_no_started_at_field_still_names_its_start(fakes: Path, tmp_path: Path) -> None:
    """The run object has no started_at. A script that reads it prints the wall clock."""
    payload = json.dumps(
        {"workflow_runs": [{"path": _PREPARE, "run_started_at": _STARTED}]},
    )
    env = _env(fakes, tmp_path, FAKE_RUNS_JSON_IN_PROGRESS=payload)
    result = _run("prep-running.sh", env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == _STARTED


def test_the_earliest_of_two_prepares_wins(fakes: Path, tmp_path: Path) -> None:
    # The earlier start holds the group first, so it is the one that the report waits behind.
    earlier = "2026-11-04T08:00:00Z"
    env = _env_for(
        fakes, tmp_path, "in_progress", (_PREPARE, "2026-11-04T09:30:00Z"), (_PREPARE, earlier)
    )
    result = _run("prep-running.sh", env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == earlier


def test_a_prepare_held_by_the_group_names_now_as_its_earliest_start(
    fakes: Path, tmp_path: Path
) -> None:
    """A prepare that waits for the group is pending, not queued. It has not started."""
    env = _env_for(fakes, tmp_path, "pending", (_PREPARE, None))
    result = _run("prep-running.sh", env)
    assert result.returncode == 0, result.stderr
    # Not the creation time: the run can still use its full window from now.
    assert result.stdout != _CREATED
    assert _ISO.match(result.stdout), result.stdout


def test_a_queued_prepare_names_now_as_its_earliest_start(fakes: Path, tmp_path: Path) -> None:
    env = _env_for(fakes, tmp_path, "queued", (_PREPARE, None))
    result = _run("prep-running.sh", env)
    assert result.returncode == 0, result.stderr
    assert result.stdout != _CREATED
    assert _ISO.match(result.stdout), result.stdout


@pytest.mark.parametrize("status", ["in_progress", "pending", "queued"])
def test_every_state_that_holds_the_group_is_asked_for(
    fakes: Path, tmp_path: Path, status: str
) -> None:
    """A state that the script never asks for is a state it cannot see."""
    env = _env_for(fakes, tmp_path, status, (_PREPARE, None))
    result = _run("prep-running.sh", env)
    assert result.returncode == 0, result.stderr
    runs_calls = [call for call in _calls(tmp_path) if "actions/runs" in call]
    assert any(f"status={status}" in call for call in runs_calls), runs_calls


def test_a_report_run_alone_holds_no_store_group(fakes: Path, tmp_path: Path) -> None:
    env = _env_for(fakes, tmp_path, "in_progress", (_REPORT, "2026-11-04T11:44:00Z"))
    result = _run("prep-running.sh", env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_a_failed_lookup_is_not_an_empty_answer(fakes: Path, tmp_path: Path) -> None:
    """A broken lookup read as "no prepare" would let the report start behind a live run."""
    env = _env(fakes, tmp_path, FAKE_RUNS_FAIL="1")
    result = _run("prep-running.sh", env)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "cannot read the in_progress workflow runs" in result.stderr


def test_a_payload_that_jq_cannot_read_is_not_an_empty_answer(fakes: Path, tmp_path: Path) -> None:
    env = _env(fakes, tmp_path, FAKE_RUNS_JSON_IN_PROGRESS="{not json")
    result = _run("prep-running.sh", env)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "cannot read the workflow run list" in result.stderr


def test_a_run_with_no_path_is_skipped_not_fatal(fakes: Path, tmp_path: Path) -> None:
    """One odd row must not stop the lookup, because a stop publishes no report."""
    payload = json.dumps(
        {
            "workflow_runs": [
                {"path": None, "run_started_at": _STARTED},
                {"path": _PREPARE, "run_started_at": _STARTED},
            ]
        }
    )
    env = _env(fakes, tmp_path, FAKE_RUNS_JSON_IN_PROGRESS=payload)
    result = _run("prep-running.sh", env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == _STARTED


def test_an_unknown_argument_is_refused(fakes: Path, tmp_path: Path) -> None:
    result = _run("prep-running.sh", _env(fakes, tmp_path), "--nope")
    assert result.returncode == 2
    assert "unknown argument" in result.stderr


def test_help_names_the_output(fakes: Path, tmp_path: Path) -> None:
    result = _run("prep-running.sh", _env(fakes, tmp_path), "--help")
    assert result.returncode == 0
    assert "ISO 8601 UTC instant" in result.stdout


@pytest.mark.parametrize("workflow", ["daily-report.yml"])
def test_the_report_workflow_passes_the_prepare_start(workflow: str) -> None:
    """The workflow is the only caller: it holds no lock, so it can read the running runs."""
    from mutation_copy import require_path

    root = Path(__file__).resolve().parents[1]
    text = require_path(root / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
    assert "bash scripts/prep-running.sh" in text
    assert "--prep-started" in text
    # The check must run before the gate decides, so the flag reaches report-gate.sh.
    assert text.index("prep-running.sh") < text.index("report-gate.sh")


def test_the_store_group_is_on_the_build_job_not_the_workflow() -> None:
    """A workflow-level group holds the gate back, and the gate then reads an empty group.

    The gate only sees a live prepare when it starts at the trigger. The group must stay on
    the build job, which is the only job that writes the store (#163).
    """
    from mutation_copy import require_path

    root = Path(__file__).resolve().parents[1]
    lines = require_path(root / _REPORT).read_text(encoding="utf-8").splitlines()
    # A top-level key starts at column 0, and a key of the build job has four spaces.
    assert [line for line in lines if line.startswith("concurrency:")] == []
    build = lines[lines.index("  build:") + 1 :]
    assert "    concurrency:" in build
    assert build.index("    concurrency:") < build.index("    steps:")
