"""Tests for the publish-daily-report.sh script, with fake `gh` on PATH."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from report_script_support import DAY, GENERATED, SHA, TAG, _calls, _env, _run

from hockey_model.store.timestamps import iso_utc

# The mutation run copies src and tests only, so the scripts are not there to test.
if "mutants" in Path(__file__).resolve().parts:
    pytest.skip("the mutation copy has no scripts directory", allow_module_level=True)


def _write_report(report_dir: Path, day: str = DAY) -> None:
    report_dir.mkdir()
    (report_dir / f"report-{day}.md").write_text("# report\n", encoding="utf-8")
    (report_dir / f"report-{day}.json").write_text(
        json.dumps({"generated_at": GENERATED}) + "\n", encoding="utf-8"
    )
    (report_dir / f"backtest-{day}.json").write_text("{}\n", encoding="utf-8")
    (report_dir / f"backtest-games-{day}.csv").write_text("game_id,elo\n1,1\n", encoding="utf-8")


def _publish(fakes: Path, tmp_path: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    args = ("--date", DAY, "--report-dir", str(tmp_path / "report"))
    return _run("publish-daily-report.sh", _env(fakes, tmp_path, **extra), *args)


def test_publish_creates_the_release_at_the_build_commit(fakes: Path, tmp_path: Path) -> None:
    _write_report(tmp_path / "report")
    result = _publish(fakes, tmp_path)
    assert result.returncode == 0, result.stderr
    create = [line for line in _calls(tmp_path) if line.startswith("gh release create")]
    assert create and f" {TAG} " in create[0]
    assert f"--target {SHA}" in create[0]
    assert create[0].count("backtest") == 2


def test_publish_accepts_a_leap_day(fakes: Path, tmp_path: Path) -> None:
    day = "2024-02-29"
    report_dir = tmp_path / "report"
    _write_report(report_dir, day)
    result = _run(
        "publish-daily-report.sh",
        _env(fakes, tmp_path),
        "--date",
        day,
        "--report-dir",
        str(report_dir),
    )
    assert result.returncode == 0, result.stderr
    assert any(f"gh release create reports-{day} " in call for call in _calls(tmp_path))


def test_publish_replaces_the_files_of_an_existing_release(fakes: Path, tmp_path: Path) -> None:
    _write_report(tmp_path / "report")
    result = _publish(fakes, tmp_path, FAKE_RELEASES=f"{TAG}\n")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    upload = [line for line in calls if line.startswith("gh release upload")]
    # The alias and the backtest files move to this build with --clobber. The version does not.
    alias = [line for line in upload if "--clobber" in line]
    assert alias and f" {TAG} " in alias[0] and f"/report-{DAY}.json" in alias[0]
    assert any(line.startswith("gh release edit") for line in calls)
    assert not any(line.startswith("gh release create") for line in calls)


def test_publish_notes_say_every_build_is_kept(fakes: Path, tmp_path: Path) -> None:
    _write_report(tmp_path / "report")
    result = _publish(fakes, tmp_path)
    assert result.returncode == 0, result.stderr
    notes = (tmp_path / "report" / f"release-notes-{DAY}.md").read_text(encoding="utf-8")
    assert "Every build's report is kept" in notes
    assert "report-" + DAY + "-HHMMSSZ" in notes
    assert f"commit {SHA}" in notes


def test_publish_needs_the_build_commit(fakes: Path, tmp_path: Path) -> None:
    _write_report(tmp_path / "report")
    env = _env(fakes, tmp_path)
    del env["GITHUB_SHA"]
    args = ("--date", DAY, "--report-dir", str(tmp_path / "report"))
    result = _run("publish-daily-report.sh", env, *args)
    assert result.returncode == 2
    assert "GITHUB_SHA" in result.stderr


@pytest.mark.parametrize(
    "bad", ["../2026-11-04", "2026-11-04 extra", "2026-1-4", "tomorrow", "2026-02-30", "0000-01-01"]
)
def test_publish_refuses_a_malformed_date(fakes: Path, tmp_path: Path, bad: str) -> None:
    _write_report(tmp_path / "report")
    args = ("--date", bad, "--report-dir", str(tmp_path / "report"))
    result = _run("publish-daily-report.sh", _env(fakes, tmp_path), *args)
    assert result.returncode == 2
    assert "not a real date YYYY-MM-DD" in result.stderr
    assert not any(line.startswith("gh release create") for line in _calls(tmp_path))


def test_publish_refuses_a_missing_report(fakes: Path, tmp_path: Path) -> None:
    (tmp_path / "report").mkdir()
    result = _publish(fakes, tmp_path)
    assert result.returncode == 1
    assert "missing or empty" in result.stderr
    assert not any(line.startswith("gh release create") for line in _calls(tmp_path))


def test_publish_refuses_an_empty_report(fakes: Path, tmp_path: Path) -> None:
    _write_report(tmp_path / "report")
    (tmp_path / "report" / f"report-{DAY}.json").write_text("", encoding="utf-8")
    result = _publish(fakes, tmp_path)
    assert result.returncode == 1
    assert "missing or empty" in result.stderr


def test_publish_refuses_invalid_json(fakes: Path, tmp_path: Path) -> None:
    _write_report(tmp_path / "report")
    (tmp_path / "report" / f"backtest-{DAY}.json").write_text("<html>", encoding="utf-8")
    result = _publish(fakes, tmp_path)
    assert result.returncode == 1
    assert "is not valid JSON" in result.stderr
    assert not any(line.startswith("gh release create") for line in _calls(tmp_path))


def test_publish_refuses_a_csv_without_a_header(fakes: Path, tmp_path: Path) -> None:
    _write_report(tmp_path / "report")
    (tmp_path / "report" / f"backtest-games-{DAY}.csv").write_text("Not Found\n", encoding="utf-8")
    result = _publish(fakes, tmp_path)
    assert result.returncode == 1
    assert "no CSV header" in result.stderr


def test_publish_help_states_that_a_version_is_never_replaced(fakes: Path, tmp_path: Path) -> None:
    result = _run("publish-daily-report.sh", _env(fakes, tmp_path), "--help")
    assert result.returncode == 0
    assert "is never replaced" in result.stdout
    assert "--report-dir" in result.stdout


def _calls_with(tmp_path: Path, prefix: str, text: str) -> list[str]:
    return [line for line in _calls(tmp_path) if line.startswith(prefix) and text in line]


def test_publish_keeps_each_build_under_its_generated_seconds(fakes: Path, tmp_path: Path) -> None:
    _write_report(tmp_path / "report")
    result = _publish(fakes, tmp_path, FAKE_RELEASES=f"{TAG}\n")
    assert result.returncode == 0, result.stderr
    for ext in ("md", "json"):
        version = _calls_with(tmp_path, "gh release upload", f"report-{DAY}-202013Z.{ext}")
        # A published version is never replaced, so its upload has no --clobber.
        assert version and "--clobber" not in version[0]


def test_publish_creates_the_release_with_versions_and_the_alias(
    fakes: Path, tmp_path: Path
) -> None:
    _write_report(tmp_path / "report")
    result = _publish(fakes, tmp_path)
    assert result.returncode == 0, result.stderr
    create = _calls_with(tmp_path, "gh release create", f" {TAG} ")
    assert create
    assert f"report-{DAY}-202013Z.json" in create[0]
    assert f"report-{DAY}-202013Z.md" in create[0]
    assert f"/report-{DAY}.json" in create[0]


@pytest.mark.parametrize("generated", ["", "2026-11-04 20:20:13", "2026-11-04T20:20:13Z"])
def test_publish_refuses_a_report_without_the_generated_time(
    fakes: Path, tmp_path: Path, generated: str
) -> None:
    _write_report(tmp_path / "report")
    (tmp_path / "report" / f"report-{DAY}.json").write_text(
        json.dumps({"generated_at": generated}) + "\n", encoding="utf-8"
    )
    result = _publish(fakes, tmp_path)
    assert result.returncode == 1
    assert "generated_at" in result.stderr
    assert not any(line.startswith("gh release") for line in _calls(tmp_path))


def _remote_versions(tmp_path: Path, body: str) -> Path:
    remote = tmp_path / "remote"
    remote.mkdir()
    for ext in ("md", "json"):
        (remote / f"report-{DAY}-202013Z.{ext}").write_text(body, encoding="utf-8")
    return remote


def test_publish_retry_with_the_same_bytes_skips_the_version(fakes: Path, tmp_path: Path) -> None:
    # A retry after a failed alias upload finds its version already on the release.
    _write_report(tmp_path / "report")
    report = tmp_path / "report"
    remote = tmp_path / "remote"
    remote.mkdir()
    for ext in ("md", "json"):
        # The version is a copy of the build, so the same build has the same bytes.
        build = report / f"report-{DAY}.{ext}"
        (remote / f"report-{DAY}-202013Z.{ext}").write_bytes(build.read_bytes())
    result = _publish(
        fakes,
        tmp_path,
        FAKE_RELEASES=f"{TAG}\n",
        FAKE_ASSETS=f"report-{DAY}-202013Z.md\nreport-{DAY}-202013Z.json",
        FAKE_REMOTE_DIR=str(remote),
    )
    assert result.returncode == 0, result.stderr
    assert "already on" in result.stderr and "same bytes" in result.stderr
    assert not _calls_with(tmp_path, "gh release upload", f"report-{DAY}-202013Z")
    assert _calls_with(tmp_path, "gh release upload", "--clobber")


def test_publish_same_name_with_other_bytes_stops_before_the_alias(
    fakes: Path, tmp_path: Path
) -> None:
    _write_report(tmp_path / "report")
    remote = _remote_versions(tmp_path, "a different build\n")
    result = _publish(
        fakes,
        tmp_path,
        FAKE_RELEASES=f"{TAG}\n",
        FAKE_ASSETS=f"report-{DAY}-202013Z.md\nreport-{DAY}-202013Z.json",
        FAKE_REMOTE_DIR=str(remote),
    )
    assert result.returncode == 1
    assert "other bytes" in result.stderr
    assert not _calls_with(tmp_path, "gh release upload", "--clobber")


def _write_build(report_dir: Path, generated: str) -> None:
    report_dir.mkdir(exist_ok=True)
    (report_dir / f"report-{DAY}.md").write_text("# report\n", encoding="utf-8")
    (report_dir / f"report-{DAY}.json").write_text(
        json.dumps({"generated_at": generated}) + "\n", encoding="utf-8"
    )
    (report_dir / f"backtest-{DAY}.json").write_text("{}\n", encoding="utf-8")
    (report_dir / f"backtest-games-{DAY}.csv").write_text("game_id,elo\n1,1\n", encoding="utf-8")


@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    moment=st.datetimes(
        min_value=datetime(2000, 1, 1), max_value=datetime(2099, 12, 31), timezones=st.just(UTC)
    )
)
def test_publish_names_each_version_by_the_hhmmss_of_its_generated_time(
    fakes: Path, tmp_path: Path, moment: datetime
) -> None:
    # Every time iso_utc can write must give a version name with its own HHMMSS.
    _write_build(tmp_path / "report", iso_utc(moment))
    result = _publish(fakes, tmp_path)
    assert result.returncode == 0, result.stderr
    created = [line for line in _calls(tmp_path) if line.startswith("gh release create")][-1]
    assert f"report-{DAY}-{moment:%H%M%S}Z.json" in created
