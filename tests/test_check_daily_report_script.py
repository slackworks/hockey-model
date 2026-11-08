"""Tests for check-daily-report.sh, with fake `gh` and `uv` commands on PATH."""

import json
import os
import subprocess
from pathlib import Path

import pytest

# The mutation run copies src and tests only, so the scripts are not there to test.
if "mutants" in Path(__file__).resolve().parts:
    pytest.skip("the mutation copy has no scripts directory", allow_module_level=True)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check-daily-report.sh"
DAY = "2026-11-04"
TAG = f"reports-{DAY}"


@pytest.fixture
def fakes(tmp_path: Path) -> Path:
    """Write fake `gh` and `uv`. Both record their arguments in calls.log.

    `gh release view TAG` succeeds for a tag in FAKE_RELEASES. `gh release download` writes
    FAKE_REPORT_JSON to the --output path, or fails when FAKE_DOWNLOAD_FAIL is set.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'echo "gh $*" >> "$FAKE_CALLS"\n'
        'if [[ "$1 $2" == "release view" ]]; then\n'
        '  if grep -qxF "$3" <<<"${FAKE_RELEASES:-}"; then exit 0; fi\n'
        '  if [[ -n "${FAKE_VIEW_ERR:-}" ]]; then echo "$FAKE_VIEW_ERR" >&2; exit 1; fi\n'
        '  echo "release not found" >&2\n'
        "  exit 1\n"
        "fi\n"
        'if [[ "$1 $2" == "release download" ]]; then\n'
        '  if [[ -n "${FAKE_DOWNLOAD_FAIL:-}" ]]; then echo "download failed" >&2; exit 1; fi\n'
        '  out=""\n'
        "  while (($# > 0)); do\n"
        '    if [[ "$1" == "--output" ]]; then out="$2"; fi\n'
        "    shift\n"
        "  done\n"
        '  printf "%s" "$FAKE_REPORT_JSON" > "$out"\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    uv = bin_dir / "uv"
    uv.write_text(
        '#!/usr/bin/env bash\necho "uv $*" >> "$FAKE_CALLS"\n'
        'if [[ "$*" == "run hockey-model report-due"* ]]; then\n'
        '  if [[ -n "${FAKE_UV_FAIL:-}" ]]; then echo "uv boom" >&2; exit 3; fi\n'
        '  printf "%s" "$FAKE_DUE_JSON"\n'
        "fi\n",
        encoding="utf-8",
    )
    for command in (gh, uv):
        command.chmod(0o755)
    return bin_dir


def _env(fakes: Path, tmp_path: Path, **extra: str) -> dict[str, str]:
    calls = tmp_path / "calls.log"
    calls.touch()
    env = {
        **os.environ,
        "PATH": f"{fakes}{os.pathsep}{os.environ['PATH']}",
        "FAKE_CALLS": str(calls),
        "FAKE_DUE_JSON": json.dumps({"due": False, "tag": TAG, "date": DAY, "games": 3}),
        "FAKE_RELEASES": "",
        "FAKE_REPORT_JSON": json.dumps({"games": [{"made_after_start": False}]}),
    }
    env.update(extra)
    return env


def _run(tmp_path: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    # The script writes its download under .tmp/ in the working directory, so run it there.
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _calls(tmp_path: Path) -> str:
    return (tmp_path / "calls.log").read_text(encoding="utf-8")


def test_a_day_with_no_games_raises_no_alert(fakes: Path, tmp_path: Path) -> None:
    due = json.dumps({"due": False, "tag": TAG, "date": DAY, "games": 0})
    result = _run(tmp_path, _env(fakes, tmp_path, FAKE_DUE_JSON=due), "--date", DAY)
    assert result.returncode == 0, result.stderr
    assert "no games" in result.stdout
    assert "gh " not in _calls(tmp_path)


def test_a_day_with_games_and_no_release_fails(fakes: Path, tmp_path: Path) -> None:
    result = _run(tmp_path, _env(fakes, tmp_path), "--date", DAY)
    assert result.returncode == 1
    assert "::error::" in result.stderr
    assert f"no report release ({TAG})" in result.stderr


def test_a_release_lookup_failure_for_another_reason_stops_the_check(
    fakes: Path, tmp_path: Path
) -> None:
    env = _env(fakes, tmp_path, FAKE_VIEW_ERR="auth failed")
    result = _run(tmp_path, env, "--date", DAY)
    assert result.returncode == 1
    assert "failed: auth failed" in result.stderr
    assert "::error::" not in result.stderr


def test_a_report_with_a_pre_start_row_passes(fakes: Path, tmp_path: Path) -> None:
    report = {"games": [{"made_after_start": True}, {"made_after_start": False}]}
    env = _env(fakes, tmp_path, FAKE_RELEASES=f"{TAG}\n", FAKE_REPORT_JSON=json.dumps(report))
    result = _run(tmp_path, env, "--date", DAY)
    assert result.returncode == 0, result.stderr
    assert "at least one pre-start row" in result.stdout


def test_a_report_where_every_row_is_after_the_start_fails(fakes: Path, tmp_path: Path) -> None:
    report = {"games": [{"made_after_start": True}, {"made_after_start": True}]}
    env = _env(fakes, tmp_path, FAKE_RELEASES=f"{TAG}\n", FAKE_REPORT_JSON=json.dumps(report))
    result = _run(tmp_path, env, "--date", DAY)
    assert result.returncode == 1
    assert "made after its start" in result.stderr


def test_a_postponed_row_does_not_hide_a_late_report(fakes: Path, tmp_path: Path) -> None:
    report = {
        "games": [
            {"made_after_start": True, "state": "FINAL"},
            {"made_after_start": False, "state": "PPD"},
        ]
    }
    env = _env(fakes, tmp_path, FAKE_RELEASES=f"{TAG}\n", FAKE_REPORT_JSON=json.dumps(report))
    result = _run(tmp_path, env, "--date", DAY)
    assert result.returncode == 1
    assert "made after its start" in result.stderr


def test_a_report_with_no_rows_raises_no_alert(fakes: Path, tmp_path: Path) -> None:
    env = _env(fakes, tmp_path, FAKE_RELEASES=f"{TAG}\n", FAKE_REPORT_JSON='{"games": []}')
    result = _run(tmp_path, env, "--date", DAY)
    assert result.returncode == 0, result.stderr


def test_a_failed_download_fails_the_check(fakes: Path, tmp_path: Path) -> None:
    env = _env(fakes, tmp_path, FAKE_RELEASES=f"{TAG}\n", FAKE_DOWNLOAD_FAIL="1")
    result = _run(tmp_path, env, "--date", DAY)
    assert result.returncode == 1
    assert "could not download" in result.stderr


def test_a_report_that_is_not_json_fails_the_check(fakes: Path, tmp_path: Path) -> None:
    env = _env(fakes, tmp_path, FAKE_RELEASES=f"{TAG}\n", FAKE_REPORT_JSON="not json")
    result = _run(tmp_path, env, "--date", DAY)
    assert result.returncode == 1
    assert "not valid JSON" in result.stderr


@pytest.mark.parametrize(
    "report",
    [
        {"games": [{"start_utc": "2026-11-05T00:00:00Z"}]},
        {"games": [{"made_after_start": "true"}]},
        {"rows": [{"made_after_start": False}]},
    ],
)
def test_a_report_with_a_changed_shape_fails_instead_of_passing(
    fakes: Path, tmp_path: Path, report: dict[str, object]
) -> None:
    env = _env(fakes, tmp_path, FAKE_RELEASES=f"{TAG}\n", FAKE_REPORT_JSON=json.dumps(report))
    result = _run(tmp_path, env, "--date", DAY)
    assert result.returncode == 1
    assert "lacks a boolean .games[].made_after_start" in result.stderr
    assert "ok " not in result.stdout


def test_a_null_report_is_named_as_the_wrong_type(fakes: Path, tmp_path: Path) -> None:
    env = _env(fakes, tmp_path, FAKE_RELEASES=f"{TAG}\n", FAKE_REPORT_JSON="null")
    result = _run(tmp_path, env, "--date", DAY)
    assert result.returncode == 1
    assert "is not a JSON object" in result.stderr


def test_a_download_failure_carries_the_gh_error(fakes: Path, tmp_path: Path) -> None:
    env = _env(fakes, tmp_path, FAKE_RELEASES=f"{TAG}\n", FAKE_DOWNLOAD_FAIL="1")
    result = _run(tmp_path, env, "--date", DAY)
    assert result.returncode == 1
    assert "download failed" in result.stderr


def test_a_failed_report_due_call_fails_the_check(fakes: Path, tmp_path: Path) -> None:
    result = _run(tmp_path, _env(fakes, tmp_path, FAKE_UV_FAIL="1"), "--date", DAY)
    assert result.returncode == 1
    assert "report-due failed" in result.stderr


def test_a_missing_date_is_a_usage_error(fakes: Path, tmp_path: Path) -> None:
    result = _run(tmp_path, _env(fakes, tmp_path), "--date", "2026-11")
    assert result.returncode == 2
    assert "not a real date YYYY-MM-DD" in result.stderr


@pytest.mark.parametrize("bad", ["2026-02-30", "2025-02-29", "2026-13-01"])
def test_an_impossible_date_is_a_usage_error(fakes: Path, tmp_path: Path, bad: str) -> None:
    result = _run(tmp_path, _env(fakes, tmp_path), "--date", bad)
    assert result.returncode == 2
    assert "not a real date" in result.stderr
    assert _calls(tmp_path) == ""


def test_an_unknown_argument_is_a_usage_error(fakes: Path, tmp_path: Path) -> None:
    result = _run(tmp_path, _env(fakes, tmp_path), "--nope")
    assert result.returncode == 2
    assert "unknown argument" in result.stderr


def test_help_names_the_exit_codes(fakes: Path, tmp_path: Path) -> None:
    result = _run(tmp_path, _env(fakes, tmp_path), "--help")
    assert result.returncode == 0
    assert "Exits 1" in result.stdout
    assert "--date" in result.stdout
