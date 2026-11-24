"""Tests for the lookup that names the prepare window that holds the store group."""

import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
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


def test_a_run_with_only_its_gate_job_listed_opens_its_window_now(
    fakes: Path, tmp_path: Path
) -> None:
    """The holder jobs of a run appear one at a time, after its gate job (#311).

    A run that just started lists its gate job and no holder yet. The gate holds no group,
    so the run would read as no prepare, and the report gate would let a replace build start
    behind it. The holder can still use its whole window, so the window opens now.
    """
    _run_now(
        fakes,
        tmp_path,
        180,
        FAKE_RUNS_JSON_IN_PROGRESS=_one_run(_STARTED),
        FAKE_JOBS_JSON=_job_rows(_job_row("gate", "2026-11-04T06:00:00Z")),
    )


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


@pytest.mark.parametrize("warn", ["gh: a deprecation warning", '{"half": "a payload"}'])
def test_a_warning_from_gh_does_not_enter_the_answer(
    fakes: Path, tmp_path: Path, warn: str
) -> None:
    """gh writes a warning to stderr beside the JSON, so the read keeps the two apart.

    A merged stream stops jq, and a stop publishes no report. Only the payload is the answer.
    """
    env = _env_for(
        fakes,
        tmp_path,
        "in_progress",
        (_PREPARE, _STARTED),
        FAKE_JOBS_JSON=_jobs(_STARTED),
        FAKE_GH_WARN=warn,
    )
    result = _lookup(env)
    assert result.returncode == 0, result.stderr
    assert _printed_end(result) == _end(_STARTED)


def test_an_unknown_argument_is_refused(fakes: Path, tmp_path: Path) -> None:
    result = _run("prep-running.sh", _env(fakes, tmp_path), "--nope")
    assert result.returncode == 2
    assert "unknown argument" in result.stderr


@pytest.mark.parametrize("minutes", ["1", "10", "360"])
def test_a_whole_minute_count_is_accepted(fakes: Path, tmp_path: Path, minutes: str) -> None:
    """The ends of the accepted range are the ones a parser gets wrong."""
    result = _run("prep-running.sh", _env(fakes, tmp_path), "--timeout", f"skip={minutes}")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("minutes", ["0", "0.5", "361", "9999", "", "abc", "-1", " 10", "1 0"])
def test_a_minute_count_that_is_not_whole_and_inside_the_limit_is_refused(
    fakes: Path, tmp_path: Path, minutes: str
) -> None:
    result = _run("prep-running.sh", _env(fakes, tmp_path), "--timeout", f"skip={minutes}")
    assert result.returncode == 2
    assert result.stdout == ""
    assert "whole minutes" in result.stderr


@pytest.mark.parametrize("minutes", ["08", "008", "060"])
def test_a_leading_zero_is_refused_with_its_own_reason(
    fakes: Path, tmp_path: Path, minutes: str
) -> None:
    """A leading zero reads as base 8, so the message names the zero and not the range.

    The two failures read the same to a caller, and they need different fixes.
    """
    result = _run("prep-running.sh", _env(fakes, tmp_path), "--timeout", f"skip={minutes}")
    assert result.returncode == 2
    assert "leading zero" in result.stderr


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
    # The check must run before the gate decides, so the flag reaches report-gate.sh. The
    # lines compare, because a comment above the step can name either script.
    assert text.index("bash scripts/prep-running.sh") < text.index("bash scripts/report-gate.sh")


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


_OTHER_STARTED = "2026-11-04T07:20:00Z"


def _one_run(started: str | None) -> str:
    """Return a run list whose one prepare run carries ``started`` as its run start."""
    row = {"id": 1000, "path": _PREPARE, "run_started_at": started}
    return json.dumps({"workflow_runs": [row]})


def _job_rows(*rows: dict[str, object]) -> str:
    """Return a job list payload that holds ``rows``."""
    return json.dumps({"jobs": list(rows)})


def _job_row(name: str, started: str | None, status: str = "in_progress") -> dict[str, object]:
    """Return one job of a prepare run that holds the group."""
    return {"name": name, "status": status, "started_at": started}


def _env_with(fakes: Path, tmp_path: Path, *jobs: dict[str, object]) -> dict[str, str]:
    """Return an environment whose running prepare holds a group, with ``jobs`` as its jobs."""
    return _env(
        fakes,
        tmp_path,
        FAKE_RUNS_JSON_IN_PROGRESS=_one_run(_STARTED),
        FAKE_JOBS_JSON=_job_rows(*jobs),
    )


def _workflow_text(path: Path) -> str:
    return require_path(path).read_text(encoding="utf-8")


def _workflows() -> tuple[str, str]:
    """Return the daily-prepare text and the daily-report text."""
    root = Path(__file__).resolve().parents[1]
    return (
        _workflow_text(root / _PREPARE),
        _workflow_text(root / _REPORT),
    )


def test_a_skip_job_names_the_end_of_its_short_window(fakes: Path, tmp_path: Path) -> None:
    """The skip job is short, so a report can wait behind it until the puck drop (#311)."""
    env = _env_with(fakes, tmp_path, _job_row("skip", _STARTED))
    result = _run("prep-running.sh", env, *_TIMEOUTS)
    assert result.returncode == 0, result.stderr
    assert result.stdout == _end(_STARTED, 10)


def test_a_prepare_job_names_the_end_of_its_own_long_window(fakes: Path, tmp_path: Path) -> None:
    env = _env_with(fakes, tmp_path, _job_row("prepare", _STARTED))
    result = _run("prep-running.sh", env, *_TIMEOUTS)
    assert result.returncode == 0, result.stderr
    assert result.stdout == _end(_STARTED, 180)


def test_a_prepare_job_that_follows_a_finished_skip_job_names_the_prepare_job(
    fakes: Path, tmp_path: Path
) -> None:
    """The skip job ended, so the prepare job it handed the group to is the holder."""
    env = _env_with(
        fakes,
        tmp_path,
        _job_row("skip", _STARTED, status="completed"),
        _job_row("prepare", _OTHER_STARTED),
    )
    result = _run("prep-running.sh", env, *_TIMEOUTS)
    assert result.returncode == 0, result.stderr
    assert result.stdout == _end(_OTHER_STARTED, 180)


def test_the_split_holder_that_ends_last_names_the_wait(fakes: Path, tmp_path: Path) -> None:
    """The group frees only after every holder ends, so the latest end is the answer.

    A skip job that started at 08:07 ends at 08:17. A prepare job that started at 07:20 ends
    at 10:20. The report waits for the later one, whatever order the group runs them.
    """
    env = _env_with(
        fakes,
        tmp_path,
        _job_row("skip", _STARTED),
        _job_row("prepare", _OTHER_STARTED),
    )
    result = _run("prep-running.sh", env, *_TIMEOUTS)
    assert result.returncode == 0, result.stderr
    assert result.stdout == _end(_OTHER_STARTED, 180)


def test_a_short_job_that_ends_last_names_its_own_short_timeout(
    fakes: Path, tmp_path: Path
) -> None:
    """A late skip job ends after an early prepare job, and the short end is the answer."""
    env = _env_with(
        fakes,
        tmp_path,
        _job_row("prepare", "2026-11-04T07:00:00Z"),
        _job_row("skip", "2026-11-04T10:00:00Z"),
    )
    result = _run("prep-running.sh", env, *_TIMEOUTS)
    assert result.returncode == 0, result.stderr
    assert result.stdout == _end("2026-11-04T10:00:00Z", 10)


def _end_after(before: datetime, minutes: int) -> str:
    """Return the earliest end that a window which opened at ``before`` can have."""
    return (before + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _assert_window(
    result: subprocess.CompletedProcess[str], minutes: int, before: datetime
) -> None:
    """Assert that the end is the one that a window of ``minutes`` from ``before`` produces.

    A run that has not started opens its window at the current time, and the script reads
    that time somewhere between ``before`` and now. One second of slack covers a clock read
    that crosses a second boundary.
    """
    low = _end_after(before, minutes)
    high = _end_after(datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1), minutes)
    assert result.returncode == 0, result.stderr
    assert _ISO.match(result.stdout), result.stdout
    assert low <= result.stdout <= high, (result.stdout, low, high)


def _run_now(fakes: Path, tmp_path: Path, minutes: int, **extra: str) -> None:
    """Run the lookup, and assert that the end is the one of a window from before the run."""
    before = datetime.now(UTC).replace(microsecond=0)
    result = _run("prep-running.sh", _env(fakes, tmp_path, **extra), *_TIMEOUTS)
    _assert_window(result, minutes, before)


def test_a_split_job_that_has_not_started_opens_its_window_now(fakes: Path, tmp_path: Path) -> None:
    """A job that waits for the group can still use its whole window from now."""
    _run_now(
        fakes,
        tmp_path,
        10,
        FAKE_RUNS_JSON_IN_PROGRESS=_one_run(_STARTED),
        FAKE_JOBS_JSON=_job_rows(_job_row("skip", None, status="queued")),
    )


def _runs_queued() -> str:
    """Return a run list whose one prepare run waits for the group."""
    return json.dumps({"workflow_runs": [{"id": 1000, "path": _PREPARE}]})


def test_a_split_run_that_has_not_started_names_its_longest_window(
    fakes: Path, tmp_path: Path
) -> None:
    """A queued run can start its prepare job first, so the long window is the one to plan for.

    A forced run skips the skip job and goes straight to the rebuild, and a run that the
    report build holds starts with whichever prepare job the group admits next. The short
    window would under-claim, and the report would then start behind the live rebuild (#311).
    """
    _run_now(
        fakes,
        tmp_path,
        180,
        FAKE_RUNS_JSON_QUEUED=_runs_queued(),
        FAKE_JOBS_JSON=_job_rows(_job_row("skip", None, status="queued")),
    )


def test_a_timeout_flag_is_required(fakes: Path, tmp_path: Path) -> None:
    """The caller names the timeouts, so the script never guesses one from the workflow."""
    result = _run("prep-running.sh", _env(fakes, tmp_path))
    assert result.returncode == 2
    assert "--timeout" in result.stderr


def test_a_timeout_that_is_not_whole_minutes_is_refused(fakes: Path, tmp_path: Path) -> None:
    result = _run("prep-running.sh", _env(fakes, tmp_path), "--timeout", "skip=ten")
    assert result.returncode == 2
    assert "whole minutes" in result.stderr


def test_a_timeout_above_the_github_limit_is_refused(fakes: Path, tmp_path: Path) -> None:
    result = _run("prep-running.sh", _env(fakes, tmp_path), "--timeout", "skip=361")
    assert result.returncode == 2
    assert "360" in result.stderr


def test_a_zero_timeout_is_refused(fakes: Path, tmp_path: Path) -> None:
    result = _run("prep-running.sh", _env(fakes, tmp_path), "--timeout", "skip=0")
    assert result.returncode == 2
    assert "whole minutes" in result.stderr


def test_the_no_run_answer_names_no_window(fakes: Path, tmp_path: Path) -> None:
    """A caller reads an empty line as "no prepare", so it must not see a bare number."""
    result = _run("prep-running.sh", _env(fakes, tmp_path), *_TIMEOUTS)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


_JOB_NAMES = st.sampled_from(["skip", "prepare", "gate"])
_TIMEOUT_RANGE = st.integers(min_value=1, max_value=360)
_STARTS = st.datetimes(
    min_value=datetime(2026, 1, 1),
    max_value=datetime(2026, 12, 31, 23, 59, 59),
).map(lambda moment: moment.strftime("%Y-%m-%dT%H:%M:%SZ"))


@st.composite
def _holding_jobs(draw: st.DrawFn) -> tuple[list[dict[str, object]], list[str]]:
    """Return jobs that hold the group, and the --timeout flags that name their timeouts.

    Each job carries its own timeout, so the answer cannot come from one fixed pair. The
    names are unique because the flags build a map: a repeated name replaces its timeout.
    """
    count = draw(st.integers(min_value=1, max_value=3))
    names = draw(st.lists(_JOB_NAMES, min_size=count, max_size=count, unique=True))
    starts = draw(st.lists(_STARTS, min_size=count, max_size=count))
    flags: list[str] = []
    jobs: list[dict[str, object]] = []
    for name, started in zip(names, starts, strict=True):
        minutes = draw(_TIMEOUT_RANGE)
        jobs.append(_job_row(name, started))
        flags += ["--timeout", f"{name}={minutes}"]
    return jobs, flags


def _latest_end(jobs: list[dict[str, object]], flags: list[str]) -> str:
    """Return the end of the job of the run that ends last."""
    pairs = re.findall(r"([a-z-]+)=(\d+)", " ".join(flags))
    timeouts = {name: int(minutes) for name, minutes in pairs}
    ends = [_end(str(job["started_at"]), timeouts[str(job["name"])]) for job in jobs]
    return max(ends)


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(holders=_holding_jobs())
def test_the_latest_end_of_any_set_of_holders_is_the_answer(
    fakes: Path, tmp_path: Path, holders: tuple[list[dict[str, object]], list[str]]
) -> None:
    """The group frees after every holder ends, so the answer is the latest end of the run.

    A fixed pair of jobs only ever checks one ordering. The rule is a maximum, so it must
    hold for any set of job starts and timeouts, including a short job that started late.
    """
    jobs, flags = holders
    env = _env(
        fakes,
        tmp_path,
        FAKE_RUNS_JSON_IN_PROGRESS=_one_run(_STARTED),
        FAKE_JOBS_JSON=_job_rows(*jobs),
    )
    result = _run("prep-running.sh", env, *flags)
    assert result.returncode == 0, result.stderr
    assert result.stdout == _latest_end(jobs, flags)


def _queued_env(fakes: Path, tmp_path: Path) -> dict[str, str]:
    """Return an environment whose only prepare run waits for the group."""
    return _env(fakes, tmp_path, FAKE_RUNS_JSON_QUEUED=_runs_queued())


@st.composite
def _longest_pair(draw: st.DrawFn) -> tuple[int, int]:
    """Return a short timeout and one that is longer, so the answer is never the short one.

    A rule that picks the shortest timeout prints an earlier end than the range below
    allows, so a wrong rule fails and the longest-timeout rule passes.
    """
    short = draw(st.integers(min_value=1, max_value=359))
    return short, draw(st.integers(min_value=short + 1, max_value=360))


def _as_flags(timeouts: list[str]) -> list[str]:
    """Return one --timeout flag for each JOB=MINUTES pair."""
    return [item for pair in timeouts for item in ("--timeout", pair)]


@settings(
    max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(pair=_longest_pair())
def test_a_run_that_has_not_started_names_its_longest_timeout(
    fakes: Path, tmp_path: Path, pair: tuple[int, int]
) -> None:
    """Any job of the run can take the group first, so the longest window is the answer.

    The run has no job start to read, so the window opens at the current time. The end is the
    one that the longest timeout produces, and the order of the flags does not change it.
    """
    short, longest = pair
    timeouts = [f"skip={short}", f"prepare={longest}"]
    before = datetime.now(UTC).replace(microsecond=0)
    result = _run("prep-running.sh", _queued_env(fakes, tmp_path), *_as_flags(timeouts))
    _assert_window(result, longest, before)


def test_the_job_timeouts_that_the_report_workflow_passes_are_the_real_ones() -> None:
    """A --timeout name that no job carries matches nothing, and reads as no prepare.

    The lookup cannot check the names at run time: the two jobs of the split prepare appear
    one at a time, so a running skip job comes with a job list that holds no `prepare` job.
    This test compares the flags with daily-prepare.yml instead (#311).
    """
    prepare, report = _workflows()
    passed = dict(re.findall(r"--timeout ([a-z-]+)=(\d+)", report))
    assert passed, "the report workflow passes no job timeout"
    for name, minutes in passed.items():
        job = re.search(
            rf"^  {re.escape(name)}:\n(.*?)(?=^  [a-z-]+:\n|\Z)", prepare, re.DOTALL | re.MULTILINE
        )
        assert job is not None, f"daily-prepare.yml has no job named {name}"
        assert f"    timeout-minutes: {minutes}\n" in job.group(1)
