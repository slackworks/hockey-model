"""Tests for the lookup that names the end of the prepare window that holds the store group."""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from mutation_copy import require_path
from report_script_support import _env, _run

_PREPARE = ".github/workflows/daily-prepare.yml"
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_STARTED = "2026-11-04T08:07:12Z"
_OTHER_STARTED = "2026-11-04T07:20:00Z"
# The two jobs of the split prepare, with the timeouts that the workflow sets on each one.
_TIMEOUTS = ("--timeout", "skip=10", "--timeout", "prepare=180")


def _end(started: str, minutes: int) -> str:
    """Return the end of a job window that opens at ``started`` and lasts ``minutes``."""
    moment = datetime.fromisoformat(started.replace("Z", "+00:00")) + timedelta(minutes=minutes)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _runs(started: str | None) -> str:
    row = {"id": 1000, "path": _PREPARE, "run_started_at": started}
    return json.dumps({"workflow_runs": [row]})


def _jobs(*rows: dict[str, object]) -> str:
    return json.dumps({"jobs": list(rows)})


def _job(name: str, started: str | None, status: str = "in_progress") -> dict[str, object]:
    return {"name": name, "status": status, "started_at": started}


def _env_with(fakes: Path, tmp_path: Path, *jobs: dict[str, object]) -> dict[str, str]:
    return _env(
        fakes,
        tmp_path,
        FAKE_RUNS_JSON_IN_PROGRESS=_runs(_STARTED),
        FAKE_JOBS_JSON=_jobs(*jobs),
    )


def _workflow_text(path: Path) -> str:
    return require_path(path).read_text(encoding="utf-8")


def _workflows() -> tuple[str, str]:
    """Return the daily-prepare text and the daily-report text."""
    root = Path(__file__).resolve().parents[1]
    return (
        _workflow_text(root / _PREPARE),
        _workflow_text(root / ".github" / "workflows" / "daily-report.yml"),
    )


def test_a_skip_job_names_the_end_of_its_short_window(fakes: Path, tmp_path: Path) -> None:
    """The skip job is short, so a report can wait behind it until the puck drop (#311)."""
    env = _env_with(fakes, tmp_path, _job("skip", _STARTED))
    result = _run("prep-running.sh", env, *_TIMEOUTS)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{_end(_STARTED, 10)} 10"


def test_a_prepare_job_names_the_end_of_its_own_long_window(fakes: Path, tmp_path: Path) -> None:
    env = _env_with(fakes, tmp_path, _job("prepare", _STARTED))
    result = _run("prep-running.sh", env, *_TIMEOUTS)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{_end(_STARTED, 180)} 180"


def test_a_prepare_job_that_follows_a_finished_skip_job_names_the_prepare_job(
    fakes: Path, tmp_path: Path
) -> None:
    """The skip job ended, so the prepare job it handed the group to is the holder."""
    env = _env_with(
        fakes,
        tmp_path,
        _job("skip", _STARTED, status="completed"),
        _job("prepare", _OTHER_STARTED),
    )
    result = _run("prep-running.sh", env, *_TIMEOUTS)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{_end(_OTHER_STARTED, 180)} 180"


def test_the_holder_that_ends_last_names_the_wait(fakes: Path, tmp_path: Path) -> None:
    """The group frees only after every holder ends, so the latest end is the answer.

    A skip job that started at 08:07 ends at 08:17. A prepare job that started at 07:20 ends
    at 10:20. The report waits for the later one, whatever order the group runs them.
    """
    env = _env_with(fakes, tmp_path, _job("skip", _STARTED), _job("prepare", _OTHER_STARTED))
    result = _run("prep-running.sh", env, *_TIMEOUTS)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{_end(_OTHER_STARTED, 180)} 180"


def test_a_short_job_that_ends_last_names_its_own_short_timeout(
    fakes: Path, tmp_path: Path
) -> None:
    """A late skip job ends after an early prepare job, and the short end is the answer."""
    env = _env_with(
        fakes,
        tmp_path,
        _job("prepare", "2026-11-04T07:00:00Z"),
        _job("skip", "2026-11-04T10:00:00Z"),
    )
    result = _run("prep-running.sh", env, *_TIMEOUTS)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{_end('2026-11-04T10:00:00Z', 10)} 10"


def test_a_job_that_has_not_started_opens_its_window_now(fakes: Path, tmp_path: Path) -> None:
    """A job that waits for the group can still use its whole window from now."""
    env = _env_with(fakes, tmp_path, _job("skip", None, status="queued"))
    result = _run("prep-running.sh", env, *_TIMEOUTS)
    assert result.returncode == 0, result.stderr
    end, minutes = result.stdout.split()
    assert _ISO.match(end), end
    assert end != _end(_STARTED, 10)
    assert minutes == "10"


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


def test_help_names_the_output(fakes: Path, tmp_path: Path) -> None:
    result = _run("prep-running.sh", _env(fakes, tmp_path), "--help")
    assert result.returncode == 0
    assert 'Prints "<ISO 8601 UTC end> <MINUTES>"' in result.stdout


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
