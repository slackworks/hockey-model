"""Tests that daily-report.sh --skip-unchanged stops a build whose inputs match the release."""

import json
from pathlib import Path

import pytest
from report_script_support import DAY, TAG, _calls, _env, _run, due_json, store_path

HASH_A = "a" * 64
HASH_B = "b" * 64
NEWEST = f"report-{DAY}-203000Z"
NEWEST_AT = f"{DAY}T20:30:00.000000+00:00"
EARLIER = f"report-{DAY}-103000Z"
EARLIER_AT = f"{DAY}T10:30:00.000000+00:00"


def _prepared_store(tmp_path: Path) -> Path:
    """Leave the marker and the backtest files that the prepare run leaves for the report."""
    store = store_path(tmp_path)
    store.parent.mkdir(parents=True, exist_ok=True)
    (store.parent / f"prepared-{DAY}").touch()
    (store.parent / f"backtest-{DAY}.json").write_text("{}", encoding="utf-8")
    (store.parent / f"backtest-games-{DAY}.csv").write_text("game_id\n", encoding="utf-8")
    return store


def _notes(build: str) -> str:
    return f"This build: commit abc, finished {DAY}T20:31:00Z UTC, {build}.\n"


def _release(
    tmp_path: Path,
    alias: object,
    builds: tuple[tuple[str, str], ...] = ((NEWEST, NEWEST_AT),),
    notes: str | None = None,
) -> dict[str, str]:
    """Write a published release of the day: its index, its alias, and its notes.

    The alias is ``report-DATE.json``. Return the environment that points the fake gh at it.
    """
    remote = tmp_path / "remote"
    remote.mkdir(exist_ok=True)
    (remote / f"report-{DAY}.json").write_text(json.dumps(alias), encoding="utf-8")
    entries = [{"file": f"{name}.json", "generated_at": at} for name, at in builds]
    (remote / f"index-{DAY}.json").write_text(json.dumps({"builds": entries}), encoding="utf-8")
    names = [f"{name}.{ext}" for name, _ in builds for ext in ("json", "md")]
    names += [f"index-{DAY}.json", f"report-{DAY}.json"]
    newest = max(builds, key=lambda build: build[1])[0] if builds else NEWEST
    return {
        "FAKE_RELEASES": TAG,
        "FAKE_ASSETS": "\n".join(names),
        "FAKE_NOTES": _notes(newest) if notes is None else notes,
    }


def _alias(inputs: str | None, generated: str = NEWEST_AT) -> dict[str, str]:
    alias = {"generated_at": generated}
    if inputs is not None:
        alias["inputs_sha256"] = inputs
    return alias


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


def _skip_run(fakes: Path, tmp_path: Path, release: dict[str, str], **extra: str):
    env = _env_for(fakes, tmp_path, {"inputs_sha256": HASH_A}, **release, **extra)
    return _run_report(env, tmp_path, "--skip-unchanged")


def test_unchanged_inputs_stop_the_build_before_the_complete_marker(
    fakes: Path, tmp_path: Path
) -> None:
    code, stderr, out = _skip_run(fakes, tmp_path, _release(tmp_path, _alias(HASH_A)))
    assert code == 0, stderr
    assert "::notice::" in stderr
    assert f"unchanged since the newest published build {NEWEST}.json" in stderr
    assert not (out / "complete").exists()


def test_the_skip_adds_its_notice_to_the_step_summary(fakes: Path, tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"
    release = _release(tmp_path, _alias(HASH_A))
    _skip_run(fakes, tmp_path, release, GITHUB_STEP_SUMMARY=str(summary))
    assert "Nothing publishes" in summary.read_text(encoding="utf-8")


def test_changed_inputs_complete_the_build(fakes: Path, tmp_path: Path) -> None:
    code, stderr, out = _skip_run(fakes, tmp_path, _release(tmp_path, _alias(HASH_B)))
    assert code == 0, stderr
    assert (out / "complete").exists()


def test_a_published_report_without_a_hash_builds_once(fakes: Path, tmp_path: Path) -> None:
    code, stderr, out = _skip_run(fakes, tmp_path, _release(tmp_path, _alias(None)))
    assert code == 0, stderr
    assert (out / "complete").exists()


def test_a_day_with_no_release_builds_without_a_download(fakes: Path, tmp_path: Path) -> None:
    env = _env_for(fakes, tmp_path, {"inputs_sha256": HASH_A})
    code, stderr, out = _run_report(env, tmp_path, "--skip-unchanged")
    assert code == 0, stderr
    assert (out / "complete").exists()
    assert not any("release download" in line for line in _calls(tmp_path))


def test_without_the_flag_equal_inputs_still_build(fakes: Path, tmp_path: Path) -> None:
    release = _release(tmp_path, _alias(HASH_A))
    env = _env_for(fakes, tmp_path, {"inputs_sha256": HASH_A}, **release)
    code, stderr, out = _run_report(env, tmp_path)
    assert code == 0, stderr
    assert (out / "complete").exists()
    assert not any(line.startswith("gh ") for line in _calls(tmp_path))


@pytest.mark.parametrize("fault", ["FAKE_DOWNLOAD_FAIL", "FAKE_DOWNLOAD_EMPTY"])
def test_a_failed_or_empty_download_stops_the_run(fakes: Path, tmp_path: Path, fault: str) -> None:
    release = _release(tmp_path, _alias(HASH_A))
    code, stderr, out = _skip_run(fakes, tmp_path, release, **{fault: "1"})
    assert code == 1
    assert f"cannot download index-{DAY}.json" in stderr
    assert not (out / "complete").exists()


def test_a_release_lookup_failure_stops_the_run(fakes: Path, tmp_path: Path) -> None:
    env = _env_for(fakes, tmp_path, {"inputs_sha256": HASH_A}, FAKE_GH_FAIL="1")
    code, stderr, out = _run_report(env, tmp_path, "--skip-unchanged")
    assert code == 1
    assert f"release lookup for {TAG} failed" in stderr
    assert not (out / "complete").exists()


def test_a_failed_notes_read_stops_the_run(fakes: Path, tmp_path: Path) -> None:
    release = _release(tmp_path, _alias(HASH_A))
    code, stderr, out = _skip_run(fakes, tmp_path, release, FAKE_NOTES_FAIL="1")
    assert code == 1
    assert f"cannot read the notes of release {TAG}" in stderr
    assert not (out / "complete").exists()


@pytest.mark.parametrize("built", [{}, {"inputs_sha256": ""}], ids=["no_field", "empty"])
def test_a_built_report_without_a_hash_stops_the_run(
    fakes: Path, tmp_path: Path, built: object
) -> None:
    release = _release(tmp_path, _alias(HASH_A))
    env = _env_for(fakes, tmp_path, built, **release)
    code, stderr, out = _run_report(env, tmp_path, "--skip-unchanged")
    assert code == 1
    assert "no inputs_sha256" in stderr
    assert not (out / "complete").exists()


def test_help_names_the_flag(fakes: Path, tmp_path: Path) -> None:
    result = _run("daily-report.sh", _env(fakes, tmp_path), "--help")
    assert result.returncode == 0
    assert "--skip-unchanged" in result.stdout


def _repaired(stderr: str, out: Path, reason: str) -> None:
    """Assert that the run built the report to publish again, and named why."""
    assert f"behind its newest build: {reason}. This build publishes to repair it." in stderr
    assert (out / "complete").exists()


@pytest.mark.parametrize("missing", [f"index-{DAY}.json", f"report-{DAY}.json"])
def test_a_release_without_its_index_or_alias_publishes_again(
    fakes: Path, tmp_path: Path, missing: str
) -> None:
    """A release from before the index, or a failed upload, cannot show its newest build."""
    release = _release(tmp_path, _alias(HASH_A))
    assets = [name for name in release["FAKE_ASSETS"].splitlines() if name != missing]
    release["FAKE_ASSETS"] = "\n".join(assets)
    code, stderr, out = _skip_run(fakes, tmp_path, release)
    assert code == 0, stderr
    _repaired(stderr, out, f"it has no {missing}")


def test_an_alias_behind_the_newest_build_is_repaired_when_the_inputs_match(
    fakes: Path, tmp_path: Path
) -> None:
    """A failed alias upload, or an older retry, leaves report-DATE.json behind (#318)."""
    builds = ((EARLIER, EARLIER_AT), (NEWEST, NEWEST_AT))
    release = _release(tmp_path, _alias(HASH_A, EARLIER_AT), builds)
    code, stderr, out = _skip_run(fakes, tmp_path, release)
    assert code == 0, stderr
    _repaired(stderr, out, f"its report-{DAY}.json is not the newest build {NEWEST}")


def test_notes_that_do_not_name_the_newest_build_are_repaired(fakes: Path, tmp_path: Path) -> None:
    builds = ((EARLIER, EARLIER_AT), (NEWEST, NEWEST_AT))
    release = _release(tmp_path, _alias(HASH_A), builds, notes=_notes(EARLIER))
    code, stderr, out = _skip_run(fakes, tmp_path, release)
    assert code == 0, stderr
    _repaired(stderr, out, f"its notes do not name the newest build {NEWEST}")


@pytest.mark.parametrize("alias", ["not json", {"inputs_sha256": HASH_A}])
def test_an_alias_with_no_generated_at_is_repaired(
    fakes: Path, tmp_path: Path, alias: object
) -> None:
    release = _release(tmp_path, {})
    text = alias if isinstance(alias, str) else json.dumps(alias)
    (tmp_path / "remote" / f"report-{DAY}.json").write_text(text, encoding="utf-8")
    code, stderr, out = _skip_run(fakes, tmp_path, release)
    assert code == 0, stderr
    _repaired(stderr, out, f"its report-{DAY}.json holds no generated_at")


def test_a_report_left_by_an_earlier_run_is_not_compared(fakes: Path, tmp_path: Path) -> None:
    """A predict step that writes nothing must not leave a stale file to match the release."""
    release = _release(tmp_path, _alias(HASH_A))
    env = _env_for(fakes, tmp_path, {"inputs_sha256": HASH_A}, **release)
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
    log = tmp_path / "env.log"
    release = _release(tmp_path, _alias(HASH_B))
    code, stderr, _ = _skip_run(
        fakes, tmp_path, release, FAKE_ENV_LOG=str(log), REPORT_GH_TOKEN="secret-token"
    )
    assert code == 0, stderr
    lines = log.read_text(encoding="utf-8").splitlines()
    # The raw value matters: the script unsets REPORT_GH_TOKEN once at the top and re-exports
    # it only as GH_TOKEN, so a `gh` call carries one token and no other command carries any
    # (#313).
    release_line = "gh GH_TOKEN=secret-token REPORT_GH_TOKEN=unset"
    assert all(line == release_line for line in lines if line.startswith("gh "))
    assert "uv GH_TOKEN=unset REPORT_GH_TOKEN=unset" in lines
    assert not any("REPORT_GH_TOKEN=secret-token" in line for line in lines)
    assert any("--json body" in line for line in _calls(tmp_path))


def test_the_newest_build_follows_generated_at_across_midnight_utc(
    fakes: Path, tmp_path: Path
) -> None:
    """A build after midnight UTC has a smaller name than one of the evening before."""
    evening, late = f"report-{DAY}-234400Z", f"report-{DAY}-004400Z"
    year, month, mday = (int(part) for part in DAY.split("-"))
    after = f"{year:04d}-{month:02d}-{mday + 1:02d}T00:44:00.000000+00:00"
    builds = ((evening, f"{DAY}T23:44:00.000000+00:00"), (late, after))
    code, stderr, out = _skip_run(
        fakes, tmp_path, _release(tmp_path, _alias(HASH_A, after), builds)
    )
    assert code == 0, stderr
    assert f"newest published build {late}.json" in stderr
    assert not (out / "complete").exists()


def test_an_index_with_no_build_stops_the_run(fakes: Path, tmp_path: Path) -> None:
    code, stderr, out = _skip_run(fakes, tmp_path, _release(tmp_path, _alias(HASH_A), builds=()))
    assert code == 1
    assert "names no build" in stderr
    assert not (out / "complete").exists()
