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


def run_json(conclusion: str | None, created: str = "2026-11-21T10:00:00Z") -> dict:
    status = "completed" if conclusion else "in_progress"
    return {"name": "ci", "status": status, "conclusion": conclusion, "created_at": created}


def answer(*runs: dict) -> str:
    return json.dumps({"total_count": len(runs), "workflow_runs": list(runs)})


def check(fakes: Path, tmp_path: Path, body: str | None, sha: str = SHA, **env: str):
    extra = dict(env)
    if body is not None:
        extra["FAKE_CI_RUNS"] = body
    return _run("check-commit-ci.sh", _env(fakes, tmp_path, **extra), "--sha", sha)


def test_a_commit_whose_push_run_succeeded_passes(fakes: Path, tmp_path: Path) -> None:
    result = check(fakes, tmp_path, answer(run_json("success")))
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    call = _calls(tmp_path)[0]
    assert "actions/workflows/ci.yml/runs?" in call
    assert f"head_sha={SHA}" in call and "event=push" in call


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped", "timed_out", None])
def test_a_run_that_did_not_succeed_fails_and_says_how(
    fakes: Path, tmp_path: Path, conclusion: str | None
) -> None:
    result = check(fakes, tmp_path, answer(run_json(conclusion)))
    assert result.returncode == 1
    shown = conclusion if conclusion else "in_progress"
    assert f"is '{shown}', and a release needs 'success'" in result.stderr


def test_a_commit_with_no_push_run_says_to_tag_a_commit_that_reached_main(
    fakes: Path, tmp_path: Path
) -> None:
    result = check(fakes, tmp_path, None)
    assert result.returncode == 1
    assert "no push run of ci.yml exists" in result.stderr
    assert "reached main" in result.stderr


def test_the_newest_run_decides(fakes: Path, tmp_path: Path) -> None:
    red_then_green = answer(
        run_json("failure", "2026-11-21T09:00:00Z"), run_json("success", "2026-11-21T11:00:00Z")
    )
    assert check(fakes, tmp_path, red_then_green).returncode == 0
    green_then_red = answer(
        run_json("success", "2026-11-21T09:00:00Z"), run_json("failure", "2026-11-21T11:00:00Z")
    )
    assert check(fakes, tmp_path, green_then_red).returncode == 1


def test_a_failed_lookup_fails_and_says_what_the_token_needs(fakes: Path, tmp_path: Path) -> None:
    result = check(fakes, tmp_path, answer(run_json("success")), FAKE_CI_RUNS_FAIL="1")
    assert result.returncode == 1
    assert "actions: read" in result.stderr


@pytest.mark.parametrize("body", ["not json", "{}", '{"workflow_runs": null}', "[]"])
def test_an_answer_with_no_run_list_fails(fakes: Path, tmp_path: Path, body: str) -> None:
    result = check(fakes, tmp_path, body)
    assert result.returncode == 1
    assert "holds no workflow_runs list" in result.stderr


@pytest.mark.parametrize("sha", ["", "main", "v0.1.0", SHA[:7], SHA.upper(), SHA + "0", "../x"])
def test_a_sha_that_is_not_a_full_commit_id_exits_2(fakes: Path, tmp_path: Path, sha: str) -> None:
    result = check(fakes, tmp_path, answer(run_json("success")), sha=sha)
    assert result.returncode == 2
    assert "is not a 40-character commit ID" in result.stderr
    assert _calls(tmp_path) == []


def test_help_and_a_bad_argument(fakes: Path, tmp_path: Path) -> None:
    helped = _run("check-commit-ci.sh", _env(fakes, tmp_path), "--help")
    assert helped.returncode == 0 and "Usage: check-commit-ci.sh" in helped.stdout
    bad = _run("check-commit-ci.sh", _env(fakes, tmp_path), "--nope")
    assert bad.returncode == 2 and "unknown argument" in bad.stderr


_RUN = st.tuples(
    st.sampled_from(["success", "failure", "cancelled", None]),
    st.integers(min_value=0, max_value=50),
)


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(runs=st.lists(_RUN, min_size=1, max_size=6, unique_by=lambda r: r[1]))
def test_the_verdict_is_the_conclusion_of_the_newest_run(
    fakes: Path, tmp_path_factory: pytest.TempPathFactory, runs: list[tuple[str | None, int]]
) -> None:
    work = tmp_path_factory.mktemp("newest")
    body = answer(*[run_json(c, f"2026-11-21T10:{minute:02d}:00Z") for c, minute in runs])
    newest = max(runs, key=lambda r: r[1])[0]
    result = check(fakes, work, body)
    assert (result.returncode == 0) == (newest == "success")


def _job(name: str) -> str:
    text = RELEASE.read_text(encoding="utf-8")
    match = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z-]+:\n|\Z)", text, re.DOTALL | re.MULTILINE)
    assert match is not None
    return match.group(1)


def test_the_verify_job_runs_the_ci_check_with_a_token_that_can_only_read() -> None:
    job = _job("verify")
    assert "actions: read" in job and "contents: read" in job
    assert "contents: write" not in job
    assert 'run: bash scripts/check-commit-ci.sh --sha "$SHA"' in job
    assert "GH_TOKEN: ${{ github.token }}" in job
