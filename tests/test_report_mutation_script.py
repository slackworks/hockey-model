"""Tests for scripts/report-mutation.sh: the issue calls and the status it prints.

The script runs against fake `gh` and `uv` commands, so no network call and no issue is made.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
from mutation_copy import require_path
from report_script_support import REAL_UV, _calls

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = require_path(ROOT / "scripts" / "report-mutation.sh")
URL = "https://example.test/run/9"
STATS = {"killed": 400, "survived": 10, "timeout": 0, "no_tests": 0, "total": 410}
GOOD_RESULTS = [
    "--shard-result",
    "success",
    "--merge-result",
    "success",
    "--export-result",
    "success",
    "--download-result",
    "success",
]

if not REAL_UV:
    pytest.skip("no uv on PATH, so the script cannot run", allow_module_level=True)


def _run(
    fakes: Path, tmp_path: Path, *args: str, issues: list[dict] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{fakes}{os.pathsep}{os.environ['PATH']}",
        "FAKE_CALLS": str(tmp_path / "calls.log"),
        "FAKE_ISSUES": json.dumps(issues if issues is not None else []),
        # The helper runs under the real uv, so the status is the real decision.
        "FAKE_REAL_UV": REAL_UV,
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        # A fake that resolves to itself loops for ever. A timeout fails the test instead.
        timeout=120,
    )


def _stats(tmp_path: Path, **changes: int) -> Path:
    path = tmp_path / "stats.json"
    path.write_text(json.dumps({**STATS, **changes}), encoding="utf-8")
    return path


def _body() -> str:
    return (ROOT / ".tmp" / "mutation-report.md").read_text(encoding="utf-8")


def test_without_file_issue_the_script_makes_no_gh_call(fakes: Path, tmp_path: Path) -> None:
    # A run on another ref still judges its own stats, and writes no issue.
    result = _run(
        fakes, tmp_path, "--stats", str(_stats(tmp_path)), "--run-url", URL, *GOOD_RESULTS
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "clean"
    # A branch run judges its stats and stops. It makes no gh call, not even for the title.
    gh_calls = [call for call in _calls(tmp_path) if call.startswith("gh ")]
    assert gh_calls == []
    assert not any("mutation_report.py title" in call for call in _calls(tmp_path))


def test_a_run_without_the_flag_that_files_no_issue_says_so(fakes: Path, tmp_path: Path) -> None:
    # The flag is opt-in, so a caller that forgets it must not read as a pass.
    result = _run(
        fakes, tmp_path, "--stats", str(tmp_path / "absent.json"), "--run-url", URL, *GOOD_RESULTS
    )
    assert "this step files no issue" in result.stderr


def test_a_clean_run_without_the_flag_says_nothing_extra(fakes: Path, tmp_path: Path) -> None:
    result = _run(
        fakes, tmp_path, "--stats", str(_stats(tmp_path)), "--run-url", URL, *GOOD_RESULTS
    )
    assert "this step files no issue" not in result.stderr


def test_a_bad_stats_file_fails_a_run_that_files_no_issue(fakes: Path, tmp_path: Path) -> None:
    # A manual run on a branch must not stay green on a missing stats file.
    result = _run(
        fakes, tmp_path, "--stats", str(tmp_path / "absent.json"), "--run-url", URL, *GOOD_RESULTS
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "execution_failed"


def test_a_failed_shard_fails_the_run(fakes: Path, tmp_path: Path) -> None:
    results = list(GOOD_RESULTS)
    results[1] = "failure"
    result = _run(fakes, tmp_path, "--stats", str(_stats(tmp_path)), "--run-url", URL, *results)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "execution_failed"
    assert "the shard job did not finish (failure)" in _body()


def test_a_skipped_download_is_a_cause(fakes: Path, tmp_path: Path) -> None:
    # A download that never ran is not a download that worked.
    results = list(GOOD_RESULTS)
    results[7] = "skipped"
    result = _run(fakes, tmp_path, "--stats", str(_stats(tmp_path)), "--run-url", URL, *results)
    assert result.returncode == 0, result.stderr
    assert "the download step did not finish (skipped)" in _body()


def test_an_unknown_download_outcome_is_a_cause(fakes: Path, tmp_path: Path) -> None:
    # An unresolved workflow expression arrives as an empty string, which the script names.
    results = list(GOOD_RESULTS)
    results[7] = "unknown"
    result = _run(fakes, tmp_path, "--stats", str(_stats(tmp_path)), "--run-url", URL, *results)
    assert result.returncode == 0, result.stderr
    assert "the download step did not finish (unknown)" in _body()


def test_the_script_rejects_a_bad_result_value(fakes: Path, tmp_path: Path) -> None:
    results = list(GOOD_RESULTS)
    results[1] = "failur"
    result = _run(fakes, tmp_path, "--stats", str(_stats(tmp_path)), "--run-url", URL, *results)
    assert result.returncode == 2
    assert "must be one of" in result.stderr


def test_the_script_rejects_a_bad_execution_value(fakes: Path, tmp_path: Path) -> None:
    result = _run(
        fakes, tmp_path, "--stats", str(_stats(tmp_path)), "--run-url", URL, "--execution", "failur"
    )
    assert result.returncode == 2
    assert "success or failure" in result.stderr


def test_file_issue_creates_the_report_when_none_is_open(fakes: Path, tmp_path: Path) -> None:
    results = list(GOOD_RESULTS)
    results[1] = "failure"
    result = _run(
        fakes,
        tmp_path,
        "--stats",
        str(_stats(tmp_path)),
        "--run-url",
        URL,
        "--file-issue",
        *results,
    )
    assert result.returncode == 0, result.stderr
    assert any(call.startswith("gh issue create") for call in _calls(tmp_path))


def test_a_failed_download_names_itself_in_the_report(fakes: Path, tmp_path: Path) -> None:
    # A failed download and a shard that wrote no stats file must not read the same.
    results = list(GOOD_RESULTS)
    results[7] = "failure"
    result = _run(
        fakes,
        tmp_path,
        "--stats",
        str(_stats(tmp_path)),
        "--run-url",
        URL,
        "--file-issue",
        *results,
    )
    assert result.returncode == 0, result.stderr
    assert "the download step did not finish (failure)" in _body()


def test_the_issue_search_fails_at_the_list_cap(fakes: Path, tmp_path: Path) -> None:
    # At the cap, an open report can be missing from the list, so a second one would open.
    issues = [{"number": n, "body": None, "author": {"is_bot": True}} for n in range(1000)]
    results = list(GOOD_RESULTS)
    results[1] = "failure"
    result = _run(
        fakes,
        tmp_path,
        "--stats",
        str(_stats(tmp_path)),
        "--run-url",
        URL,
        "--file-issue",
        *results,
        issues=issues,
    )
    assert result.returncode == 1
    assert "limit" in result.stderr
    assert not any(call.startswith("gh issue create") for call in _calls(tmp_path))


def test_one_issue_below_the_cap_still_files(fakes: Path, tmp_path: Path) -> None:
    issues = [{"number": n, "body": None, "author": {"is_bot": True}} for n in range(999)]
    results = list(GOOD_RESULTS)
    results[1] = "failure"
    result = _run(
        fakes,
        tmp_path,
        "--stats",
        str(_stats(tmp_path)),
        "--run-url",
        URL,
        "--file-issue",
        *results,
        issues=issues,
    )
    assert result.returncode == 0, result.stderr
    assert any(call.startswith("gh issue create") for call in _calls(tmp_path))


def test_help_prints_the_usage_without_the_set_line(fakes: Path, tmp_path: Path) -> None:
    result = _run(fakes, tmp_path, "--help")
    assert result.returncode == 0, result.stderr
    assert "Usage: scripts/report-mutation.sh" in result.stdout
    assert "set -euo pipefail" not in result.stdout
