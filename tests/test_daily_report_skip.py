"""Tests that daily-report.sh --skip-unchanged stops a build whose inputs match the release."""

import json
from pathlib import Path

import pytest
from report_script_support import DAY, TAG, _calls, _env, _run, due_json, store_path

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


def test_the_newest_versioned_build_is_the_one_compared(fakes: Path, tmp_path: Path) -> None:
    """A failed alias upload leaves report-DATE.json behind the newest build, so it is not read."""
    remote = _published(tmp_path, {"inputs_sha256": HASH_B})
    newest = f"report-{DAY}-203000Z.json"
    (remote / newest).write_text(json.dumps({"inputs_sha256": HASH_A}), encoding="utf-8")
    assets = "\n".join([f"report-{DAY}-103000Z.json", newest, f"report-{DAY}.json"])
    env = _env_for(
        fakes, tmp_path, {"inputs_sha256": HASH_A}, FAKE_RELEASES=TAG, FAKE_ASSETS=assets
    )
    code, stderr, out = _run_report(env, tmp_path, "--skip-unchanged")
    assert code == 0, stderr
    assert newest in stderr
    assert not (out / "complete").exists()


def test_a_download_that_writes_no_file_stops_the_run(fakes: Path, tmp_path: Path) -> None:
    _published(tmp_path, {"inputs_sha256": HASH_A})
    env = _env_for(
        fakes, tmp_path, {"inputs_sha256": HASH_A}, FAKE_RELEASES=TAG, FAKE_DOWNLOAD_EMPTY="1"
    )
    code, stderr, out = _run_report(env, tmp_path, "--skip-unchanged")
    assert code == 1
    assert f"cannot download report-{DAY}.json" in stderr
    assert not (out / "complete").exists()


def test_a_report_left_by_an_earlier_run_is_not_compared(fakes: Path, tmp_path: Path) -> None:
    """A predict step that writes nothing must not leave a stale file to match the release."""
    _published(tmp_path, {"inputs_sha256": HASH_A})
    env = _env_for(fakes, tmp_path, {"inputs_sha256": HASH_A}, FAKE_RELEASES=TAG)
    del env["FAKE_BUILT_REPORT"]
    out = tmp_path / "out"
    out.mkdir()
    (out / f"report-{DAY}.json").write_text(json.dumps({"inputs_sha256": HASH_A}), encoding="utf-8")
    code, stderr, _ = _run_report(env, tmp_path, "--skip-unchanged")
    assert code == 1
    assert "cannot read the built report" in stderr


@pytest.mark.parametrize("name", [f"backtest-{DAY}.json", f"backtest-games-{DAY}.csv"])
def test_a_marker_without_its_backtest_files_stops_the_run(
    fakes: Path, tmp_path: Path, name: str
) -> None:
    env = _env_for(fakes, tmp_path, {"inputs_sha256": HASH_A})
    (store_path(tmp_path).parent / name).unlink()
    code, stderr, out = _run_report(env, tmp_path)
    assert code == 1
    assert name in stderr
    assert "--force" in stderr
    assert not (out / "complete").exists()
    assert not any("hockey-model refresh" in line for line in _calls(tmp_path))


def test_the_missing_marker_check_reads_no_release_token(fakes: Path, tmp_path: Path) -> None:
    """The token reaches only the release calls, and this path makes none (#313).

    The check reads the score feed over HTTP, so it must not carry the token that the
    workflow hands the release calls. This path runs before the store checks, so a title
    is the only thing that runs before it.
    """
    log = tmp_path / "env.log"
    env = _env(
        fakes,
        tmp_path,
        HOCKEY_MODEL_DB=str(store_path(tmp_path)),
        FAKE_DUE_JSON=due_json(puck_drop_passed=False),
        FAKE_ENV_LOG=str(log),
        REPORT_GH_TOKEN="secret-token",
    )
    result = _run("daily-report.sh", env, "--date", DAY, "--out-dir", str(tmp_path / "out"))
    assert result.returncode == 0, result.stderr
    lines = log.read_text(encoding="utf-8").splitlines()
    assert any("hockey-model report-due" in line for line in _calls(tmp_path))
    assert not any("REPORT_GH_TOKEN=secret-token" in line for line in lines), lines


def test_the_release_token_reaches_only_the_release_calls(fakes: Path, tmp_path: Path) -> None:
    _published(tmp_path, {"inputs_sha256": HASH_B})
    log = tmp_path / "env.log"
    env = _env_for(
        fakes,
        tmp_path,
        {"inputs_sha256": HASH_A},
        FAKE_RELEASES=TAG,
        FAKE_ENV_LOG=str(log),
        REPORT_GH_TOKEN="secret-token",
    )
    code, stderr, _ = _run_report(env, tmp_path, "--skip-unchanged")
    assert code == 0, stderr
    lines = log.read_text(encoding="utf-8").splitlines()
    # The raw value matters: a step that leaves REPORT_GH_TOKEN set leaks it to the package
    # code even when GH_TOKEN is unset (#313).
    assert "gh GH_TOKEN=secret-token REPORT_GH_TOKEN=secret-token" in lines
    assert "uv GH_TOKEN=unset REPORT_GH_TOKEN=unset" in lines
    assert not any("REPORT_GH_TOKEN=secret-token" in line for line in lines if "uv " in line)


def _index(remote: Path, builds: list[tuple[str, str]]) -> None:
    entries = [{"file": name, "generated_at": at} for name, at in builds]
    (remote / f"index-{DAY}.json").write_text(json.dumps({"builds": entries}), encoding="utf-8")


def test_the_newest_build_follows_generated_at_across_midnight_utc(
    fakes: Path, tmp_path: Path
) -> None:
    """A build after midnight UTC has a smaller name than one of the evening before."""
    remote = _published(tmp_path, {"inputs_sha256": HASH_B})
    evening, late = f"report-{DAY}-234400Z.json", f"report-{DAY}-004400Z.json"
    (remote / evening).write_text(json.dumps({"inputs_sha256": HASH_B}), encoding="utf-8")
    (remote / late).write_text(json.dumps({"inputs_sha256": HASH_A}), encoding="utf-8")
    year, month, mday = (int(part) for part in DAY.split("-"))
    after = f"{year:04d}-{month:02d}-{mday + 1:02d}T00:44:00Z"
    _index(remote, [(evening, f"{DAY}T23:44:00Z"), (late, after)])
    assets = "\n".join([evening, late, f"index-{DAY}.json", f"report-{DAY}.json"])
    env = _env_for(
        fakes, tmp_path, {"inputs_sha256": HASH_A}, FAKE_RELEASES=TAG, FAKE_ASSETS=assets
    )
    code, stderr, out = _run_report(env, tmp_path, "--skip-unchanged")
    assert code == 0, stderr
    assert late in stderr
    assert not (out / "complete").exists()


def test_an_index_with_no_build_stops_the_run(fakes: Path, tmp_path: Path) -> None:
    remote = _published(tmp_path, {"inputs_sha256": HASH_A})
    _index(remote, [])
    assets = "\n".join([f"index-{DAY}.json", f"report-{DAY}.json"])
    env = _env_for(
        fakes, tmp_path, {"inputs_sha256": HASH_A}, FAKE_RELEASES=TAG, FAKE_ASSETS=assets
    )
    code, stderr, out = _run_report(env, tmp_path, "--skip-unchanged")
    assert code == 1
    assert "names no build" in stderr
    assert not (out / "complete").exists()
