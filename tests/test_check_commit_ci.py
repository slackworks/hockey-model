"""Tests for scripts/check-commit-ci.sh and its use in release.yml (#299)."""

import json
import re
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from mutation_copy import require_path
from report_script_support import _calls, _env, _run

ROOT = Path(__file__).resolve().parents[1]
RELEASE = require_path(ROOT / ".github" / "workflows" / "release.yml")
SHA = "0123456789abcdef0123456789abcdef01234567"
JOBS = ("ci-result", "editorconfig", "smoke", "zizmor")


def run_json(name: str, conclusion: str | None, started: str = "2026-11-21T10:00:00Z") -> dict:
    status = "completed" if conclusion else "in_progress"
    return {"name": name, "status": status, "conclusion": conclusion, "started_at": started}


def runs(**conclusions: str | None) -> str:
    """Return a check-runs answer. Each keyword is a job name with its conclusion."""
    body = [run_json(name.replace("_", "-"), value) for name, value in conclusions.items()]
    return json.dumps({"total_count": len(body), "check_runs": body})


def check(fakes: Path, tmp_path: Path, answer: str | None, sha: str = SHA, **env: str):
    extra = dict(env)
    if answer is not None:
        extra["FAKE_CHECK_RUNS"] = answer
    return _run("check-commit-ci.sh", _env(fakes, tmp_path, **extra), "--sha", sha)


GOOD = runs(ci_result="success", editorconfig="success", smoke="success", zizmor="success")


def test_a_commit_whose_jobs_all_succeeded_passes(fakes: Path, tmp_path: Path) -> None:
    result = check(fakes, tmp_path, GOOD)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert f"commits/{SHA}/check-runs" in _calls(tmp_path)[0]


@pytest.mark.parametrize("job", JOBS)
@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped", "timed_out", None])
def test_a_required_job_that_did_not_succeed_fails_and_is_named(
    fakes: Path, tmp_path: Path, job: str, conclusion: str | None
) -> None:
    values: dict[str, str | None] = {name.replace("-", "_"): "success" for name in JOBS}
    values[job.replace("-", "_")] = conclusion
    result = check(fakes, tmp_path, runs(**values))
    assert result.returncode == 1
    shown = conclusion if conclusion else "in_progress"
    assert f"the ci job '{job}' is '{shown}'" in result.stderr


@pytest.mark.parametrize("job", JOBS)
def test_a_required_job_that_never_ran_is_missing(fakes: Path, tmp_path: Path, job: str) -> None:
    values = {name.replace("-", "_"): "success" for name in JOBS if name != job}
    result = check(fakes, tmp_path, runs(**values))
    assert result.returncode == 1
    assert f"the ci job '{job}' is 'missing'" in result.stderr


def test_a_commit_with_no_check_run_says_to_wait_or_push_to_main(
    fakes: Path, tmp_path: Path
) -> None:
    result = check(fakes, tmp_path, None)
    assert result.returncode == 1
    assert "no check run exists" in result.stderr


def test_the_newest_run_of_a_job_decides(fakes: Path, tmp_path: Path) -> None:
    old_red = run_json("smoke", "failure", "2026-11-21T09:00:00Z")
    new_green = run_json("smoke", "success", "2026-11-21T11:00:00Z")
    others = [run_json(name, "success") for name in JOBS if name != "smoke"]
    answer = json.dumps({"check_runs": [old_red, new_green, *others]})
    assert check(fakes, tmp_path, answer).returncode == 0
    answer = json.dumps(
        {
            "check_runs": [
                run_json("smoke", "success", "2026-11-21T09:00:00Z"),
                run_json("smoke", "failure", "2026-11-21T11:00:00Z"),
                *others,
            ]
        }
    )
    assert check(fakes, tmp_path, answer).returncode == 1


def test_a_run_of_another_job_name_does_not_stand_in(fakes: Path, tmp_path: Path) -> None:
    answer = runs(ci_result="success", editorconfig="success", smoke="success", zizmor2="success")
    result = check(fakes, tmp_path, answer)
    assert result.returncode == 1 and "'zizmor' is 'missing'" in result.stderr


def test_a_failed_lookup_fails_and_says_what_the_token_needs(fakes: Path, tmp_path: Path) -> None:
    result = check(fakes, tmp_path, GOOD, FAKE_CHECK_RUNS_FAIL="1")
    assert result.returncode == 1
    assert "checks: read" in result.stderr


@pytest.mark.parametrize("answer", ["not json", "{}", '{"check_runs": null}', "[]"])
def test_an_answer_with_no_check_run_list_fails(fakes: Path, tmp_path: Path, answer: str) -> None:
    result = check(fakes, tmp_path, answer)
    assert result.returncode == 1
    assert "holds no check_runs list" in result.stderr


@pytest.mark.parametrize("sha", ["", "main", "v0.1.0", SHA[:7], SHA.upper(), SHA + "0", "../x"])
def test_a_sha_that_is_not_a_full_commit_id_exits_2(fakes: Path, tmp_path: Path, sha: str) -> None:
    result = check(fakes, tmp_path, GOOD, sha=sha)
    assert result.returncode == 2
    assert "is not a 40-character commit ID" in result.stderr
    assert _calls(tmp_path) == []


def test_help_and_a_bad_argument(fakes: Path, tmp_path: Path) -> None:
    helped = _run("check-commit-ci.sh", _env(fakes, tmp_path), "--help")
    assert helped.returncode == 0 and "Usage: check-commit-ci.sh" in helped.stdout
    bad = _run("check-commit-ci.sh", _env(fakes, tmp_path), "--nope")
    assert bad.returncode == 2 and "unknown argument" in bad.stderr


@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    conclusions=st.fixed_dictionaries(
        {name: st.sampled_from(["success", "failure", "skipped", "cancelled"]) for name in JOBS}
    )
)
def test_the_verdict_is_success_exactly_when_every_required_job_succeeded(
    fakes: Path, tmp_path_factory: pytest.TempPathFactory, conclusions: dict[str, str]
) -> None:
    work = tmp_path_factory.mktemp("verdict")
    answer = json.dumps({"check_runs": [run_json(n, c) for n, c in conclusions.items()]})
    result = check(fakes, work, answer)
    every = all(value == "success" for value in conclusions.values())
    assert (result.returncode == 0) == every
    named = {job for job, value in conclusions.items() if value != "success"}
    assert {job for job in JOBS if f"the ci job '{job}'" in result.stderr} == named


def _job(name: str) -> str:
    text = RELEASE.read_text(encoding="utf-8")
    match = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z-]+:\n|\Z)", text, re.DOTALL | re.MULTILINE)
    assert match is not None
    return match.group(1)


def test_the_verify_job_runs_the_ci_check_with_a_token_that_can_only_read() -> None:
    job = _job("verify")
    assert "checks: read" in job and "contents: read" in job
    assert "contents: write" not in job
    assert 'run: bash scripts/check-commit-ci.sh --sha "$SHA"' in job
    assert "GH_TOKEN: ${{ github.token }}" in job
