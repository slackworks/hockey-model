"""Tests for the lookup that names the prepare window that holds the store group."""

import json
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from mutation_copy import require_path
from report_script_support import _calls, _env, _run

DAY = "2026-11-04"
_PREPARE = ".github/workflows/daily-prepare.yml"
_REPORT = ".github/workflows/daily-report.yml"
_STARTED = "2026-11-04T08:07:12Z"
# A queued run carries run_started_at, and the value is its creation time. The lookup must
# not read it, because that instant reads as a run that ends early.
_CREATED = "2026-11-04T07:55:00Z"
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
# The two jobs of the split prepare, with the timeouts that the workflow sets on each one.
_TIMEOUTS = ("--timeout", "skip=10", "--timeout", "prepare=180")


def _lookup(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the lookup with the job timeouts that the report gate passes."""
    return _run("prep-running.sh", env, *_TIMEOUTS)


def _end(started: str, minutes: int = 180) -> str:
    """Return the end of a job window that opens at ``started`` and lasts ``minutes``."""
    moment = datetime.fromisoformat(started.replace("Z", "+00:00")) + timedelta(minutes=minutes)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _printed_end(result: subprocess.CompletedProcess[str]) -> str:
    """Return the end of the window that the lookup printed."""
    assert _ISO.match(result.stdout), result.stdout
    return result.stdout


def _runs(*runs: tuple[str, str | None], status: str = "in_progress") -> str:
    """Return a workflow run list payload in the shape that the API returns for ``status``.

    A running run names its start in ``run_started_at``. A run that the group holds, or one
    that waits for a runner, holds its creation time there. No run object has ``started_at``.
    Each run carries an id, because the window of a running run comes from its job list.
    """
    rows = []
    for index, (path, started) in enumerate(runs):
        row = {"id": 1000 + index, "path": path, "started_at": None, "created_at": _CREATED}
        if status == "in_progress" and started is not None:
            row["run_started_at"] = started
        else:
            row["run_started_at"] = _CREATED
        rows.append(row)
    return json.dumps({"workflow_runs": rows})


def _jobs(started: str | None) -> str:
    """Return a job list payload whose one job started at ``started``, or has not started."""
    status = "in_progress" if started is not None else "queued"
    return json.dumps({"jobs": [{"name": "prepare", "status": status, "started_at": started}]})


def _env_for(
    fakes: Path, tmp_path: Path, status: str, *runs: tuple[str, str | None], **extra: str
) -> dict[str, str]:
    """Return an environment whose fake gh answers ``runs`` for one status only."""
    key = f"FAKE_RUNS_JSON_{status.upper()}"
    return _env(fakes, tmp_path, **{key: _runs(*runs, status=status)}, **extra)


def test_no_runs_means_no_prepare_holds_the_group(fakes: Path, tmp_path: Path) -> None:
    result = _lookup(_env(fakes, tmp_path))
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_a_running_prepare_names_the_end_of_its_job_window(fakes: Path, tmp_path: Path) -> None:
    """GitHub counts the job timeout from the job start, so the end of the window is the answer."""
    env = _env_for(
        fakes, tmp_path, "in_progress", (_PREPARE, _STARTED), FAKE_JOBS_JSON=_jobs(_STARTED)
    )
    result = _lookup(env)
    assert result.returncode == 0, result.stderr
    assert _printed_end(result) == _end(_STARTED)


def test_a_job_that_has_not_started_opens_its_window_now(fakes: Path, tmp_path: Path) -> None:
    """A run is in_progress while its job waits for a runner, and its window opens later.

    Reading the run start would predict an earlier end than the prepare can have, which is
    the late-publish case that the check exists to catch (#163).
    """
    env = _env_for(fakes, tmp_path, "in_progress", (_PREPARE, _STARTED), FAKE_JOBS_JSON=_jobs(None))
    result = _lookup(env)
    assert result.returncode == 0, result.stderr
    # Not the run start, and not the creation time: the job can still use its full window.
    end = _printed_end(result)
    assert end != _end(_STARTED)
    assert end != _end(_CREATED)
    assert _ISO.match(end), end


def test_a_gate_job_that_started_early_does_not_move_the_window(
    fakes: Path, tmp_path: Path
) -> None:
    """The gate job starts at once, and the prepare job waits for the group (#285)."""
    jobs = json.dumps(
        {
            "jobs": [
                {"name": "gate", "status": "in_progress", "started_at": "2026-11-04T06:00:00Z"},
                {"name": "prepare", "status": "in_progress", "started_at": _STARTED},
            ]
        }
    )
    env = _env_for(fakes, tmp_path, "in_progress", (_PREPARE, "2026-11-04T06:00:00Z"))
    result = _lookup({**env, "FAKE_JOBS_JSON": jobs})
    assert (result.returncode, _printed_end(result)) == (0, _end(_STARTED))


def test_a_prepare_job_that_waits_for_the_group_opens_its_window_now(
    fakes: Path, tmp_path: Path
) -> None:
    jobs = json.dumps(
        {
            "jobs": [
                {"name": "gate", "status": "in_progress", "started_at": "2026-11-04T06:00:00Z"},
                {"name": "prepare", "status": "queued", "started_at": None},
            ]
        }
    )
    env = _env_for(fakes, tmp_path, "in_progress", (_PREPARE, "2026-11-04T06:00:00Z"))
    result = _lookup({**env, "FAKE_JOBS_JSON": jobs})
    assert result.returncode == 0, result.stderr
    end = _printed_end(result)
    assert end != _end("2026-11-04T06:00:00Z")
    assert _ISO.match(end), end


@pytest.mark.parametrize("status", ["queued", "waiting", "pending", "requested"])
def test_a_waiting_prepare_job_opens_its_window_now_even_when_it_carries_a_start(
    fakes: Path, tmp_path: Path, status: str
) -> None:
    """GitHub fills started_at of a job that waits with its creation time."""
    created = "2026-11-04T06:00:00Z"
    jobs = json.dumps({"jobs": [{"name": "prepare", "status": status, "started_at": created}]})
    env = _env_for(fakes, tmp_path, "in_progress", (_PREPARE, created))
    result = _lookup({**env, "FAKE_JOBS_JSON": jobs})
    assert result.returncode == 0, result.stderr
    end = _printed_end(result)
    assert end != _end(created)
    assert _ISO.match(end), end


def test_a_prepare_job_that_the_gate_skipped_holds_no_group(fakes: Path, tmp_path: Path) -> None:
    skipped = json.dumps(
        {
            "jobs": [
                {
                    "name": "prepare",
                    "status": "completed",
                    "conclusion": "skipped",
                    "started_at": "2026-11-04T06:00:00Z",
                },
                # The skip job of the same run holds the group, and it is the holder.
                {"name": "skip", "status": "in_progress", "started_at": _CREATED},
            ]
        }
    )
    env = _env_for(fakes, tmp_path, "in_progress", (_PREPARE, "2026-11-04T06:00:00Z"))
    result = _lookup({**env, "FAKE_JOBS_JSON": skipped})
    assert result.returncode == 0, result.stderr
    assert _printed_end(result) == _end(_CREATED, 10)


def test_a_prepare_job_that_stopped_early_holds_no_group(fakes: Path, tmp_path: Path) -> None:
    """A run whose job died keeps its run status, and the job list is the record (#311).

    GitHub keeps a run `in_progress` after one job fails or is cancelled, because the jobs
    that need it keep running. A start from such a job is not a window that the report
    waits behind, and the run then reads as a holder for hours after it stopped.
    """
    stopped = "2026-11-04T05:00:00Z"
    jobs = json.dumps(
        {
            "jobs": [
                {
                    "name": "prepare",
                    "status": "completed",
                    "conclusion": "cancelled",
                    "started_at": stopped,
                },
                {"name": "gate", "status": "in_progress", "started_at": stopped},
            ]
        }
    )
    env = _env_for(fakes, tmp_path, "in_progress", (_PREPARE, "2026-11-04T06:00:00Z"))
    result = _lookup({**env, "FAKE_JOBS_JSON": jobs})
    assert (result.returncode, result.stdout) == (0, "")


def test_the_run_with_the_latest_end_names_the_wait(fakes: Path, tmp_path: Path) -> None:
    """The group frees only after every holder ends, so the latest end is the answer.

    The start of a run does not decide that: two runs can each hold their job at once, and
    the one that ends last is the one that the report waits for.
    """
    earlier = "2026-11-04T08:00:00Z"
    later = "2026-11-04T09:30:00Z"
    env = _env(
        fakes,
        tmp_path,
        FAKE_RUNS_JSON_IN_PROGRESS=_runs((_PREPARE, later), (_PREPARE, earlier)),
        FAKE_JOBS_JSON=json.dumps(
            {
                "jobs": [
                    {"name": "prepare", "status": "in_progress", "started_at": later},
                    {"name": "skip", "status": "in_progress", "started_at": earlier},
                ]
            }
        ),
    )
    # The fake gh answers the same job list for both runs, so the latest end of the run that
    # started later wins. A skip job of the later start still ends before the prepare job.
    result = _lookup(env)
    assert result.returncode == 0, result.stderr
    assert _printed_end(result) == _end(later)


def test_a_prepare_held_by_the_group_opens_its_window_now(fakes: Path, tmp_path: Path) -> None:
    """A prepare that waits for the group is pending, not queued. It has not started."""
    env = _env_for(fakes, tmp_path, "pending", (_PREPARE, None))
    result = _lookup(env)
    assert result.returncode == 0, result.stderr
    # Not the creation time: the run can still use its full window from now. The group can
    # admit the rebuild job first, so the long window is the answer (#311).
    end = _printed_end(result)
    assert end != _end(_CREATED, 180)
    assert _ISO.match(end), end


def test_a_queued_prepare_opens_its_window_now(fakes: Path, tmp_path: Path) -> None:
    env = _env_for(fakes, tmp_path, "queued", (_PREPARE, None))
    result = _lookup(env)
    assert result.returncode == 0, result.stderr
    assert _printed_end(result) != _end(_CREATED, 180)


@pytest.mark.parametrize("status", ["in_progress", "pending", "queued", "waiting", "requested"])
def test_every_state_that_holds_the_group_is_asked_for(
    fakes: Path, tmp_path: Path, status: str
) -> None:
    """A state that the script never asks for is a state it cannot see."""
    env = _env_for(fakes, tmp_path, status, (_PREPARE, None))
    result = _lookup(env)
    assert result.returncode == 0, result.stderr
    runs_calls = [call for call in _calls(tmp_path) if "actions/runs" in call]
    assert any(f"status={status}" in call for call in runs_calls), runs_calls


def test_a_report_run_alone_holds_no_store_group(fakes: Path, tmp_path: Path) -> None:
    env = _env_for(fakes, tmp_path, "in_progress", (_REPORT, "2026-11-04T11:44:00Z"))
    result = _lookup(env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_a_failed_lookup_is_not_an_empty_answer(fakes: Path, tmp_path: Path) -> None:
    """A broken lookup read as "no prepare" would let the report start behind a live run."""
    env = _env(fakes, tmp_path, FAKE_RUNS_FAIL="1")
    result = _lookup(env)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "cannot read the in_progress workflow runs" in result.stderr


def test_a_failed_job_read_is_not_an_empty_answer(fakes: Path, tmp_path: Path) -> None:
    """A broken job read must not read as "no prepare", for the same reason as the run list."""
    env = _env_for(fakes, tmp_path, "in_progress", (_PREPARE, _STARTED), FAKE_JOBS_FAIL="1")
    result = _lookup(env)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "cannot read the jobs of run" in result.stderr


def test_a_payload_that_jq_cannot_read_is_not_an_empty_answer(fakes: Path, tmp_path: Path) -> None:
    env = _env(fakes, tmp_path, FAKE_RUNS_JSON_IN_PROGRESS="{not json")
    result = _lookup(env)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "cannot read the workflow run list" in result.stderr


@pytest.mark.parametrize("started", ["not-a-time", "2026-13-45T99:99:99Z"])
def test_a_start_that_no_clock_read_can_use_names_its_run_and_its_job(
    fakes: Path, tmp_path: Path, started: str
) -> None:
    """A stop with no message sends the reader to look for a cause that is not named."""
    env = _env_for(
        fakes, tmp_path, "in_progress", (_PREPARE, _STARTED), FAKE_JOBS_JSON=_jobs(started)
    )
    result = _lookup(env)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "cannot add" in result.stderr
    assert started in result.stderr
    assert "1000" in result.stderr


def test_a_run_with_no_path_is_skipped_not_fatal(fakes: Path, tmp_path: Path) -> None:
    """One odd row must not stop the lookup, because a stop publishes no report."""
    payload = json.dumps(
        {
            "workflow_runs": [
                {"id": 1000, "path": None, "run_started_at": _STARTED},
                {"id": 1001, "path": _PREPARE, "run_started_at": _STARTED},
            ]
        }
    )
    env = _env(
        fakes,
        tmp_path,
        FAKE_RUNS_JSON_IN_PROGRESS=payload,
        FAKE_JOBS_JSON=_jobs(_STARTED),
    )
    result = _lookup(env)
    assert result.returncode == 0, result.stderr
    assert _printed_end(result) == _end(_STARTED)


def test_the_script_reads_the_job_that_the_workflow_names_prepare() -> None:
    """A renamed job would match nothing, and the lookup would read the group as free.

    The names come from the report workflow, so this test holds the two files together. A
    job rename that misses the workflow leaves a name that no job carries.
    """
    root = Path(__file__).resolve().parents[1]
    report = require_path(root / _REPORT).read_text(encoding="utf-8")
    assert "--timeout prepare=180" in report
    prepare = require_path(root / _PREPARE).read_text(encoding="utf-8")
    assert re.search(r"^  prepare:\n", prepare, re.MULTILINE)
    assert "    timeout-minutes: 180\n" in prepare


def test_an_unknown_argument_is_refused(fakes: Path, tmp_path: Path) -> None:
    result = _run("prep-running.sh", _env(fakes, tmp_path), "--nope")
    assert result.returncode == 2
    assert "unknown argument" in result.stderr


def test_help_names_the_output(fakes: Path, tmp_path: Path) -> None:
    result = _run("prep-running.sh", _env(fakes, tmp_path), "--help")
    assert result.returncode == 0
    assert "Prints an ISO 8601 UTC end" in result.stdout


@pytest.mark.parametrize("workflow", ["daily-report.yml"])
def test_the_report_workflow_passes_the_prepare_end(workflow: str) -> None:
    """The workflow is the only caller: it holds no lock, so it can read the running runs."""
    root = Path(__file__).resolve().parents[1]
    text = require_path(root / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
    assert "bash scripts/prep-running.sh" in text
    assert "--prep-ends" in text
    # The check must run before the gate decides, so the flag reaches report-gate.sh.
    assert text.index("prep-running.sh") < text.index("report-gate.sh")


def test_the_store_group_is_on_the_build_job_not_the_workflow() -> None:
    """A workflow-level group holds the gate back, and the gate then reads an empty group.

    The gate only sees a live prepare when it starts at the trigger. The group must stay on
    the build job, which is the only job that writes the store (#163).
    """
    root = Path(__file__).resolve().parents[1]
    lines = require_path(root / _REPORT).read_text(encoding="utf-8").splitlines()
    # A top-level key starts at column 0, and a key of the build job has four spaces.
    assert [line for line in lines if line.startswith("concurrency:")] == []
    build = lines[lines.index("  build:") + 1 :]
    assert "    concurrency:" in build
    assert build.index("    concurrency:") < build.index("    steps:")
