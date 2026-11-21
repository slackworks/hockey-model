"""Tests that daily-report.sh --skip-unchanged stops a build whose inputs match the release."""

import json
from pathlib import Path

import pytest
from report_script_support import DAY, TAG, _calls, _env, _run, store_path

HASH_A = "a" * 64
HASH_B = "b" * 64


def _prepared_store(tmp_path: Path) -> Path:
    """Leave the marker and the backtest files that the prepare run leaves for the report."""
    store = store_path(tmp_path)
    store.parent.mkdir(parents=True, exist_ok=True)
    (store.parent / f"prepared-{DAY}").touch()
    (store.parent / f"backtest-{DAY}.json").write_text("{}", encoding="utf-8")
    (store.parent / f"backtest-games-{DAY}.csv").write_text("game_id\n", encoding="utf-8")
    return store


def _published(tmp_path: Path, payload: object) -> Path:
    """Put the alias of the newest published build where the fake release download finds it."""
    remote = tmp_path / "remote"
    remote.mkdir(exist_ok=True)
    (remote / f"report-{DAY}.json").write_text(json.dumps(payload), encoding="utf-8")
    return remote


def _env_for(fakes: Path, tmp_path: Path, built: object, **extra: str) -> dict[str, str]:
    store = _prepared_store(tmp_path)
    return _env(
        fakes,
        tmp_path,
        HOCKEY_MODEL_DB=str(store),
        FAKE_BUILT_REPORT=json.dumps(built),
        FAKE_REMOTE_DIR=str(tmp_path / "remote"),
        **extra,
    )


def _run_report(env: dict[str, str], tmp_path: Path, *flags: str) -> tuple[int, str, Path]:
    out = tmp_path / "out"
    result = _run("daily-report.sh", env, "--date", DAY, "--out-dir", str(out), *flags)
    return result.returncode, result.stderr, out


def test_unchanged_inputs_stop_the_build_before_the_complete_marker(
    fakes: Path, tmp_path: Path
) -> None:
    _published(tmp_path, {"inputs_sha256": HASH_A})
    env = _env_for(fakes, tmp_path, {"inputs_sha256": HASH_A}, FAKE_RELEASES=TAG)
    code, stderr, out = _run_report(env, tmp_path, "--skip-unchanged")
    assert code == 0, stderr
    assert "::notice::" in stderr
    assert "unchanged" in stderr
    assert not (out / "complete").exists()


def test_the_skip_adds_its_notice_to_the_step_summary(fakes: Path, tmp_path: Path) -> None:
    _published(tmp_path, {"inputs_sha256": HASH_A})
    summary = tmp_path / "summary.md"
    env = _env_for(
        fakes,
        tmp_path,
        {"inputs_sha256": HASH_A},
        FAKE_RELEASES=TAG,
        GITHUB_STEP_SUMMARY=str(summary),
    )
    _run_report(env, tmp_path, "--skip-unchanged")
    assert "Nothing publishes" in summary.read_text(encoding="utf-8")


def test_changed_inputs_complete_the_build(fakes: Path, tmp_path: Path) -> None:
    _published(tmp_path, {"inputs_sha256": HASH_B})
    env = _env_for(fakes, tmp_path, {"inputs_sha256": HASH_A}, FAKE_RELEASES=TAG)
    code, stderr, out = _run_report(env, tmp_path, "--skip-unchanged")
    assert code == 0, stderr
    assert (out / "complete").exists()


def test_a_published_report_without_a_hash_builds_once(fakes: Path, tmp_path: Path) -> None:
    _published(tmp_path, {"date": DAY})
    env = _env_for(fakes, tmp_path, {"inputs_sha256": HASH_A}, FAKE_RELEASES=TAG)
    code, stderr, out = _run_report(env, tmp_path, "--skip-unchanged")
    assert code == 0, stderr
    assert (out / "complete").exists()


def test_a_day_with_no_release_builds_without_a_download(fakes: Path, tmp_path: Path) -> None:
    env = _env_for(fakes, tmp_path, {"inputs_sha256": HASH_A})
    code, stderr, out = _run_report(env, tmp_path, "--skip-unchanged")
    assert code == 0, stderr
    assert (out / "complete").exists()
    assert not any("release download" in line for line in _calls(tmp_path))


def test_without_the_flag_equal_inputs_still_build(fakes: Path, tmp_path: Path) -> None:
    _published(tmp_path, {"inputs_sha256": HASH_A})
    env = _env_for(fakes, tmp_path, {"inputs_sha256": HASH_A}, FAKE_RELEASES=TAG)
    code, stderr, out = _run_report(env, tmp_path)
    assert code == 0, stderr
    assert (out / "complete").exists()
    assert not any(line.startswith("gh ") for line in _calls(tmp_path))


def test_a_download_failure_stops_the_run(fakes: Path, tmp_path: Path) -> None:
    _published(tmp_path, {"inputs_sha256": HASH_A})
    env = _env_for(
        fakes, tmp_path, {"inputs_sha256": HASH_A}, FAKE_RELEASES=TAG, FAKE_DOWNLOAD_FAIL="1"
    )
    code, stderr, out = _run_report(env, tmp_path, "--skip-unchanged")
    assert code == 1
    assert f"cannot download report-{DAY}.json" in stderr
    assert not (out / "complete").exists()


def test_a_release_lookup_failure_stops_the_run(fakes: Path, tmp_path: Path) -> None:
    env = _env_for(fakes, tmp_path, {"inputs_sha256": HASH_A}, FAKE_GH_FAIL="1")
    code, stderr, out = _run_report(env, tmp_path, "--skip-unchanged")
    assert code == 1
    assert f"release lookup for {TAG} failed" in stderr
    assert not (out / "complete").exists()


@pytest.mark.parametrize("built", [{}, {"inputs_sha256": ""}], ids=["no_field", "empty"])
def test_a_built_report_without_a_hash_stops_the_run(
    fakes: Path, tmp_path: Path, built: object
) -> None:
    _published(tmp_path, {"inputs_sha256": HASH_A})
    env = _env_for(fakes, tmp_path, built, FAKE_RELEASES=TAG)
    code, stderr, out = _run_report(env, tmp_path, "--skip-unchanged")
    assert code == 1
    assert "no inputs_sha256" in stderr
    assert not (out / "complete").exists()


def test_help_names_the_flag(fakes: Path, tmp_path: Path) -> None:
    result = _run("daily-report.sh", _env(fakes, tmp_path), "--help")
    assert result.returncode == 0
    assert "--skip-unchanged" in result.stdout
