"""Tests for the lookup that names the end of the prepare window that holds the store group."""

import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
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
    assert result.stdout == _end(_STARTED, 10)


def test_a_prepare_job_names_the_end_of_its_own_long_window(fakes: Path, tmp_path: Path) -> None:
    env = _env_with(fakes, tmp_path, _job("prepare", _STARTED))
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
        _job("skip", _STARTED, status="completed"),
        _job("prepare", _OTHER_STARTED),
    )
    result = _run("prep-running.sh", env, *_TIMEOUTS)
    assert result.returncode == 0, result.stderr
    assert result.stdout == _end(_OTHER_STARTED, 180)


def test_the_holder_that_ends_last_names_the_wait(fakes: Path, tmp_path: Path) -> None:
    """The group frees only after every holder ends, so the latest end is the answer.

    A skip job that started at 08:07 ends at 08:17. A prepare job that started at 07:20 ends
    at 10:20. The report waits for the later one, whatever order the group runs them.
    """
    env = _env_with(fakes, tmp_path, _job("skip", _STARTED), _job("prepare", _OTHER_STARTED))
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
        _job("prepare", "2026-11-04T07:00:00Z"),
        _job("skip", "2026-11-04T10:00:00Z"),
    )
    result = _run("prep-running.sh", env, *_TIMEOUTS)
    assert result.returncode == 0, result.stderr
    assert result.stdout == _end("2026-11-04T10:00:00Z", 10)


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


def _run_now(
    fakes: Path, tmp_path: Path, flags: tuple[str, ...], minutes: int, **extra: str
) -> None:
    """Run the lookup, and assert that the end is the one of a window from before the run."""
    before = datetime.now(UTC).replace(microsecond=0)
    result = _run("prep-running.sh", _env(fakes, tmp_path, **extra), *flags)
    _assert_window(result, minutes, before)


def test_a_job_that_has_not_started_opens_its_window_now(fakes: Path, tmp_path: Path) -> None:
    """A job that waits for the group can still use its whole window from now."""
    _run_now(
        fakes,
        tmp_path,
        _TIMEOUTS,
        10,
        FAKE_RUNS_JSON_IN_PROGRESS=_runs(_STARTED),
        FAKE_JOBS_JSON=_jobs(_job("skip", None, status="queued")),
    )


def _runs_queued() -> str:
    return json.dumps({"workflow_runs": [{"id": 1000, "path": _PREPARE}]})


def test_a_run_that_has_not_started_names_its_longest_window(fakes: Path, tmp_path: Path) -> None:
    """A queued run can start its prepare job first, so the long window is the one to plan for.

    A forced run skips the skip job and goes straight to the rebuild, and a run that the
    report build holds starts with whichever prepare job the group admits next. The short
    window would under-claim, and the report would then start behind the live rebuild (#311).
    """
    _run_now(
        fakes,
        tmp_path,
        _TIMEOUTS,
        180,
        FAKE_RUNS_JSON_QUEUED=_runs_queued(),
        FAKE_JOBS_JSON=_jobs(_job("skip", None, status="queued")),
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


def test_help_names_the_output(fakes: Path, tmp_path: Path) -> None:
    result = _run("prep-running.sh", _env(fakes, tmp_path), "--help")
    assert result.returncode == 0
    assert "Prints an ISO 8601 UTC end" in result.stdout


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
        jobs.append(_job(name, started))
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
        FAKE_RUNS_JSON_IN_PROGRESS=_runs(_STARTED),
        FAKE_JOBS_JSON=_jobs(*jobs),
    )
    result = _run("prep-running.sh", env, *flags)
    assert result.returncode == 0, result.stderr
    assert result.stdout == _latest_end(jobs, flags)


def _queued_env(fakes: Path, tmp_path: Path) -> dict[str, str]:
    return _env(fakes, tmp_path, FAKE_RUNS_JSON_QUEUED=_runs_queued())


@st.composite
def _longest_pair(draw: st.DrawFn) -> tuple[int, int]:
    """Return a short timeout and one that is longer, so the answer is never the short one.

    A rule that picks the shortest timeout prints an earlier end than the range below
    allows, so a wrong rule fails and the longest-timeout rule passes.
    """
    short = draw(st.integers(min_value=1, max_value=359))
    return short, draw(st.integers(min_value=short + 1, max_value=360))


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


def _as_flags(timeouts: list[str]) -> list[str]:
    """Return one --timeout flag for each JOB=MINUTES pair."""
    return [item for pair in timeouts for item in ("--timeout", pair)]


def _end_after(before: datetime, minutes: int) -> str:
    """Return the earliest end that a window which opened at ``before`` can have."""
    return (before + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


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
