"""Tests for the report shell scripts, with fake `gh` and `uv` commands on PATH."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

# The mutation run copies src and tests only, so the scripts are not there to test.
if "mutants" in Path(__file__).resolve().parts:
    pytest.skip("the mutation copy has no scripts directory", allow_module_level=True)

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
LIVE = ["xg_form", "rest"]
DAY = "2026-11-04"
TAG = f"reports-{DAY}"
SHA = "0123456789abcdef0123456789abcdef01234567"
REAL_UV = shutil.which("uv") or ""


@pytest.fixture
def fakes(tmp_path: Path) -> Path:
    """Write fake `gh` and `uv` commands. Each records its arguments in calls.log.

    `gh release view TAG` succeeds for a tag in FAKE_RELEASES and otherwise prints
    "release not found", as the real command does. `gh release create` refuses a tag that
    already exists. FAKE_GH_FAIL makes every gh call fail with an auth error.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'echo "gh $*" >> "$FAKE_CALLS"\n'
        'if [[ -n "${FAKE_GH_FAIL:-}" ]]; then echo "auth failed" >&2; exit 4; fi\n'
        'tag="${3:-}"\n'
        'if [[ "$1 $2" == "release view" ]]; then\n'
        '  if grep -qxF "$tag" <<<"${FAKE_RELEASES:-}"; then exit 0; fi\n'
        '  echo "release not found" >&2\n'
        "  exit 1\n"
        "fi\n"
        'if [[ "$1 $2" == "release create" ]]; then\n'
        '  if grep -qxF "$3" <<<"${FAKE_RELEASES:-}"; then\n'
        '    echo "a release with the same tag name already exists" >&2\n'
        "    exit 1\n"
        "  fi\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    uv = bin_dir / "uv"
    uv.write_text(
        '#!/usr/bin/env bash\necho "uv $*" >> "$FAKE_CALLS"\n'
        'if [[ "$*" == "run hockey-model refresh --backfill-from"* ]]; then\n'
        '  if [[ -n "${FAKE_REFRESH_FAIL:-}" ]]; then\n'
        '    printf "{\\"errors\\": [\\"boom\\"]}"; exit 1\n'
        "  fi\n"
        '  printf "{\\"backfill_complete\\": %s}" "${FAKE_BACKFILL_COMPLETE:-true}"\n'
        "fi\n"
        'if [[ "$*" == "run hockey-model report-due"* ]]; then\n'
        '  if [[ -n "${FAKE_UV_FAIL:-}" ]]; then echo "uv boom" >&2; exit 3; fi\n'
        '  printf "%s" "$FAKE_DUE_JSON"\n'
        "fi\n"
        'if [[ "$*" == "run hockey-model report-decide"* ]]; then exec "$FAKE_REAL_UV" "$@"; fi\n'
        'if [[ "$*" == "run hockey-model backtest --games-out "* ]]; then\n'
        '  echo "game_id,elo" > "$5"; printf "%s\\n" "$FAKE_BACKTEST_JSON"\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    for command in (gh, uv):
        command.chmod(0o755)
    return bin_dir


def _env(fakes: Path, tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        "PATH": f"{fakes}{os.pathsep}{os.environ['PATH']}",
        "FAKE_CALLS": str(tmp_path / "calls.log"),
        "FAKE_RELEASES": "",
        "FAKE_DUE_JSON": json.dumps({"due": True, "tag": TAG, "date": DAY, "games": 3}),
        "FAKE_BACKTEST_JSON": json.dumps({"live_features": LIVE}),
        "GITHUB_SHA": SHA,
        # report-decide is the real command: it holds the decision the gate script prints.
        "FAKE_REAL_UV": REAL_UV,
    }
    env.update(extra)
    return env


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _run(script: str, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPTS / script), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


# --- gate ---------------------------------------------------------------------


def test_gate_due_and_not_published_publishes(fakes: Path, tmp_path: Path) -> None:
    result = _run("report-gate.sh", _env(fakes, tmp_path), "--date", DAY)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "due": True,
        "publish": True,
        "tag": TAG,
        "reason": "due",
        "date": DAY,
    }


def test_gate_skips_a_day_that_is_already_published(fakes: Path, tmp_path: Path) -> None:
    # After the puck drop, a published day is skipped and never replaced.
    not_due = json.dumps({"due": False, "tag": TAG, "date": DAY, "games": 3})
    env = _env(fakes, tmp_path, FAKE_DUE_JSON=not_due, FAKE_RELEASES=f"reports-2026-11-03\n{TAG}\n")
    result = _run("report-gate.sh", env, "--date", DAY)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "due": False,
        "publish": False,
        "tag": TAG,
        "reason": "already-published",
        "date": DAY,
    }


def test_gate_replaces_a_published_day_before_the_puck_drop(fakes: Path, tmp_path: Path) -> None:
    due = json.dumps({"due": True, "tag": TAG, "date": DAY, "games": 3})
    env = _env(fakes, tmp_path, FAKE_DUE_JSON=due, FAKE_RELEASES=f"{TAG}\n")
    result = _run("report-gate.sh", env, "--date", DAY)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "due": True,
        "publish": True,
        "tag": TAG,
        "reason": "replace",
        "date": DAY,
    }


def test_gate_forced_run_does_not_replace_a_published_day(fakes: Path, tmp_path: Path) -> None:
    due = json.dumps({"due": True, "tag": TAG, "date": DAY, "games": 3})
    env = _env(fakes, tmp_path, FAKE_DUE_JSON=due, FAKE_RELEASES=f"{TAG}\n")
    result = _run("report-gate.sh", env, "--date", DAY, "--force")
    assert json.loads(result.stdout)["reason"] == "already-published"


@pytest.mark.parametrize(
    ("puck_drop_passed", "margin_args"),
    [(True, []), (False, ["--margin-seconds", "600"])],
    ids=["after-the-puck-drop", "inside-the-margin"],
)
def test_gate_publishes_the_first_report_of_a_day_late_and_warns(
    fakes: Path, tmp_path: Path, puck_drop_passed: bool, margin_args: list[str]
) -> None:
    due = json.dumps(
        {
            "due": False,
            "tag": TAG,
            "date": DAY,
            "games": 3,
            "first_puck_drop_utc": "2026-11-05T00:00:00Z",
            "puck_drop_passed": puck_drop_passed,
        }
    )
    env = _env(fakes, tmp_path, FAKE_DUE_JSON=due)
    result = _run("report-gate.sh", env, "--date", DAY, *margin_args)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "due": True,
        "publish": True,
        "tag": TAG,
        "reason": "late",
        "date": DAY,
    }
    assert "gh release view" in "\n".join(_calls(tmp_path))
    assert "::warning::" in result.stderr
    assert "has passed or is less than" in result.stderr
    assert "2026-11-05T00:00:00Z" in result.stderr


def test_gate_after_the_puck_drop_skips_a_day_already_published(
    fakes: Path, tmp_path: Path
) -> None:
    due = json.dumps({"due": False, "tag": TAG, "date": DAY, "games": 3})
    env = _env(fakes, tmp_path, FAKE_DUE_JSON=due, FAKE_RELEASES=f"{TAG}\n")
    result = _run("report-gate.sh", env, "--date", DAY)
    assert json.loads(result.stdout)["reason"] == "already-published"
    assert "::warning::" not in result.stderr


def test_gate_without_a_date_lets_report_due_pick_the_game_day(fakes: Path, tmp_path: Path) -> None:
    result = _run("report-gate.sh", _env(fakes, tmp_path))
    assert result.returncode == 0, result.stderr
    assert any("uv run hockey-model report-due" in line for line in _calls(tmp_path))
    assert json.loads(result.stdout)["date"] == DAY


def test_gate_forwards_force(fakes: Path, tmp_path: Path) -> None:
    _run("report-gate.sh", _env(fakes, tmp_path), "--date", DAY, "--force")
    assert any(
        f"uv run hockey-model report-due --date {DAY} --force" in line for line in _calls(tmp_path)
    )


def test_gate_forwards_the_margin_so_a_late_run_is_not_due(fakes: Path, tmp_path: Path) -> None:
    _run("report-gate.sh", _env(fakes, tmp_path), "--date", DAY, "--margin-seconds", "600")
    assert any("--margin-seconds 600" in line for line in _calls(tmp_path))


def test_gate_rejects_a_margin_that_is_not_whole_seconds(fakes: Path, tmp_path: Path) -> None:
    result = _run("report-gate.sh", _env(fakes, tmp_path), "--margin-seconds", "10m")
    assert result.returncode == 2
    assert "--margin-seconds needs whole seconds" in result.stderr


def test_gate_dry_run_builds_a_published_day_without_a_release_lookup(
    fakes: Path, tmp_path: Path
) -> None:
    env = _env(fakes, tmp_path, FAKE_RELEASES=f"{TAG}\n")
    result = _run("report-gate.sh", env, "--date", DAY, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "due": True,
        "publish": False,
        "tag": TAG,
        "reason": "dry-run",
        "date": DAY,
    }
    assert any(
        f"uv run hockey-model report-due --date {DAY} --force" in line for line in _calls(tmp_path)
    )
    assert not any(line.startswith("gh ") for line in _calls(tmp_path))


def test_gate_dry_run_still_skips_a_day_that_is_not_due(fakes: Path, tmp_path: Path) -> None:
    due = json.dumps({"due": False, "tag": TAG, "date": DAY, "games": 3})
    result = _run(
        "report-gate.sh", _env(fakes, tmp_path, FAKE_DUE_JSON=due), "--date", DAY, "--dry-run"
    )
    assert json.loads(result.stdout)["reason"] == "dry-run"


def test_gate_fails_when_the_release_lookup_fails_for_another_reason(
    fakes: Path, tmp_path: Path
) -> None:
    result = _run("report-gate.sh", _env(fakes, tmp_path, FAKE_GH_FAIL="1"), "--date", DAY)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "auth failed" in result.stderr


def test_gate_fails_on_a_payload_without_the_gate_fields(fakes: Path, tmp_path: Path) -> None:
    bad = json.dumps({"due": "yes", "tag": TAG, "date": DAY, "games": 3})
    result = _run("report-gate.sh", _env(fakes, tmp_path, FAKE_DUE_JSON=bad), "--date", DAY)
    assert result.returncode == 1
    assert "lacks due, tag, games, or date" in result.stderr


def test_gate_publishes_nothing_for_an_empty_schedule(fakes: Path, tmp_path: Path) -> None:
    # An empty schedule can be a failed feed read, so it must not make a permanent release.
    empty = json.dumps({"due": True, "tag": TAG, "date": DAY, "games": 0})
    result = _run("report-gate.sh", _env(fakes, tmp_path, FAKE_DUE_JSON=empty), "--date", DAY)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "due": False,
        "publish": False,
        "tag": TAG,
        "reason": "no-games",
        "date": DAY,
    }
    assert "::warning::" in result.stderr
    assert not any(line.startswith("gh ") for line in _calls(tmp_path))


def test_gate_names_a_failed_report_due_call(fakes: Path, tmp_path: Path) -> None:
    result = _run("report-gate.sh", _env(fakes, tmp_path, FAKE_UV_FAIL="1"), "--date", DAY)
    assert result.returncode == 1
    assert "report-due failed" in result.stderr


def test_gate_rejects_an_unknown_argument(fakes: Path, tmp_path: Path) -> None:
    result = _run("report-gate.sh", _env(fakes, tmp_path), "--bogus")
    assert result.returncode == 2
    assert "unknown argument" in result.stderr


def test_gate_help_names_every_option(fakes: Path, tmp_path: Path) -> None:
    result = _run("report-gate.sh", _env(fakes, tmp_path), "--help")
    assert result.returncode == 0
    assert "--date" in result.stdout and "--force" in result.stdout
    assert "set -euo" not in result.stdout


# --- publish ------------------------------------------------------------------


def _write_report(report_dir: Path) -> None:
    report_dir.mkdir()
    (report_dir / f"report-{DAY}.md").write_text("# report\n", encoding="utf-8")
    (report_dir / f"report-{DAY}.json").write_text("{}\n", encoding="utf-8")
    (report_dir / f"backtest-{DAY}.json").write_text("{}\n", encoding="utf-8")
    (report_dir / f"backtest-games-{DAY}.csv").write_text("game_id,elo\n1,1\n", encoding="utf-8")


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


def test_publish_replaces_the_files_of_an_existing_release(fakes: Path, tmp_path: Path) -> None:
    _write_report(tmp_path / "report")
    result = _publish(fakes, tmp_path, FAKE_RELEASES=f"{TAG}\n")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    upload = [line for line in calls if line.startswith("gh release upload")]
    assert upload and "--clobber" in upload[0] and f" {TAG} " in upload[0]
    assert any(line.startswith("gh release edit") for line in calls)
    assert not any(line.startswith("gh release create") for line in calls)


def test_publish_notes_say_the_release_can_change_before_puck_drop(
    fakes: Path, tmp_path: Path
) -> None:
    _write_report(tmp_path / "report")
    result = _publish(fakes, tmp_path)
    assert result.returncode == 0, result.stderr
    notes = (tmp_path / "report" / f"release-notes-{DAY}.md").read_text(encoding="utf-8")
    assert "can be replaced by a later build until the first puck drop" in notes
    assert f"commit {SHA}" in notes


def test_publish_needs_the_build_commit(fakes: Path, tmp_path: Path) -> None:
    _write_report(tmp_path / "report")
    env = _env(fakes, tmp_path)
    del env["GITHUB_SHA"]
    args = ("--date", DAY, "--report-dir", str(tmp_path / "report"))
    result = _run("publish-daily-report.sh", env, *args)
    assert result.returncode == 2
    assert "GITHUB_SHA" in result.stderr


@pytest.mark.parametrize("bad", ["../2026-11-04", "2026-11-04 extra", "2026-1-4", "tomorrow"])
def test_publish_refuses_a_malformed_date(fakes: Path, tmp_path: Path, bad: str) -> None:
    _write_report(tmp_path / "report")
    args = ("--date", bad, "--report-dir", str(tmp_path / "report"))
    result = _run("publish-daily-report.sh", _env(fakes, tmp_path), *args)
    assert result.returncode == 2
    assert "must be YYYY-MM-DD" in result.stderr
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


def test_publish_help_names_the_delete_first_step(fakes: Path, tmp_path: Path) -> None:
    result = _run("publish-daily-report.sh", _env(fakes, tmp_path), "--help")
    assert result.returncode == 0
    assert "replaces the release files" in result.stdout
    assert "--report-dir" in result.stdout


# --- prepare and daily run ----------------------------------------------------


def _store(tmp_path: Path) -> Path:
    return tmp_path / "store" / "store.db"


def test_prepare_needs_the_store_path(fakes: Path, tmp_path: Path) -> None:
    result = _run("daily-prepare.sh", _env(fakes, tmp_path), "--date", DAY)
    assert result.returncode == 2
    assert "HOCKEY_MODEL_DB" in result.stderr


def test_prepare_backfills_until_the_marker_exists(fakes: Path, tmp_path: Path) -> None:
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)))
    first = _run("daily-prepare.sh", env, "--date", DAY)
    assert first.returncode == 0, first.stderr
    assert "backfilling from 2014-10-01" in first.stderr
    assert (tmp_path / "store" / "store.db.backfilled").exists()

    _run("daily-prepare.sh", env, "--date", DAY)
    refreshes = [line for line in _calls(tmp_path) if "refresh" in line]
    assert refreshes == [
        "uv run hockey-model refresh --backfill-from 2014-10-01 --budget-seconds 1800",
        "uv run hockey-model refresh",
    ]


def test_prepare_exits_75_and_writes_no_marker_when_the_backfill_stops(
    fakes: Path, tmp_path: Path
) -> None:
    env = _env(
        fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)), FAKE_BACKFILL_COMPLETE="false"
    )
    result = _run("daily-prepare.sh", env, "--date", DAY)
    assert result.returncode == 75, result.stderr
    assert "run it again" in result.stderr
    assert not (tmp_path / "store" / "store.db.backfilled").exists()
    assert not any("backtest" in line for line in _calls(tmp_path))


def test_prepare_prints_the_refresh_report_when_the_refresh_fails(
    fakes: Path, tmp_path: Path
) -> None:
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)), FAKE_REFRESH_FAIL="1")
    result = _run("daily-prepare.sh", env, "--date", DAY)
    assert result.returncode == 1
    assert "boom" in result.stdout
    assert not (tmp_path / "store" / "store.db.backfilled").exists()


BAD_DAYS = ["../2026-11-04", "2026-13-45", "2026-02-30", "2025-02-29", "2026-00-10", "2026-4-5"]


@pytest.mark.parametrize("bad", BAD_DAYS)
def test_prepare_refuses_a_date_that_is_not_a_real_date(
    fakes: Path, tmp_path: Path, bad: str
) -> None:
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)))
    result = _run("daily-prepare.sh", env, "--date", bad)
    assert result.returncode == 2
    assert "not a real date" in result.stderr
    assert _calls(tmp_path) == []


@pytest.mark.parametrize("bad", BAD_DAYS)
def test_report_refuses_a_date_that_is_not_a_real_date(
    fakes: Path, tmp_path: Path, bad: str
) -> None:
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)))
    result = _run("daily-report.sh", env, "--date", bad, "--out-dir", str(tmp_path / "out"))
    assert result.returncode == 2
    assert "not a real date" in result.stderr
    assert _calls(tmp_path) == []


@pytest.mark.parametrize("good", ["2024-02-29", "2000-02-29", "2026-12-31", "2026-11-05"])
def test_prepare_accepts_a_real_date(fakes: Path, tmp_path: Path, good: str) -> None:
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)))
    assert _run("daily-prepare.sh", env, "--date", good).returncode == 0


def test_prepare_passes_the_budget_from_the_environment(fakes: Path, tmp_path: Path) -> None:
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)), BACKFILL_BUDGET_S="60")
    _run("daily-prepare.sh", env, "--date", DAY)
    assert "uv run hockey-model refresh --backfill-from 2014-10-01 --budget-seconds 60" in _calls(
        tmp_path
    )


def test_prepare_writes_the_backtest_and_the_marker_and_drops_old_days(
    fakes: Path, tmp_path: Path
) -> None:
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    (store_dir / "store.db.backfilled").touch()
    (store_dir / "prepared-2026-11-03").touch()
    (store_dir / "backtest-2026-11-03.json").write_text("{}", encoding="utf-8")
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)))
    result = _run("daily-prepare.sh", env, "--date", DAY)
    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in store_dir.iterdir()) == [
        f"backtest-{DAY}.json",
        f"backtest-games-{DAY}.csv",
        f"prepared-{DAY}",
        "store.db.backfilled",
    ]
    assert any(
        "uv run hockey-model fit --features xg_form,rest" in line for line in _calls(tmp_path)
    )


def test_prepare_fits_on_the_features_the_backtest_kept(fakes: Path, tmp_path: Path) -> None:
    """The served fit uses live_features, so a vetoed candidate never reaches the report."""
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)))
    env["FAKE_BACKTEST_JSON"] = json.dumps({"live_features": ["rest"], "tested_features": LIVE})
    assert _run("daily-prepare.sh", env, "--date", DAY).returncode == 0
    fits = [line for line in _calls(tmp_path) if "hockey-model fit" in line]
    assert fits == ["uv run hockey-model fit --features rest"]


def test_prepare_fits_the_elo_alone_when_the_joint_test_failed(fakes: Path, tmp_path: Path) -> None:
    """An empty live_features list is the raw Elo: the fit command gets an empty value."""
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)))
    env["FAKE_BACKTEST_JSON"] = json.dumps({"live_features": []})
    result = _run("daily-prepare.sh", env, "--date", DAY)
    assert result.returncode == 0
    fits = [line for line in _calls(tmp_path) if "hockey-model fit" in line]
    assert fits == ["uv run hockey-model fit --features "]
    assert "the fit is the Elo alone" in result.stderr


def test_prepare_stops_when_the_backtest_names_no_live_features(
    fakes: Path, tmp_path: Path
) -> None:
    """A report without live_features is an error, not a fit on a guess."""
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)))
    env["FAKE_BACKTEST_JSON"] = "{}"
    result = _run("daily-prepare.sh", env, "--date", DAY)
    assert result.returncode == 1
    assert "no live_features list" in result.stderr
    assert not any("hockey-model fit" in line for line in _calls(tmp_path))
    assert not (tmp_path / "store" / f"prepared-{DAY}").exists()


def test_prepare_stops_at_a_failed_refresh(fakes: Path, tmp_path: Path) -> None:
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)))
    failing = fakes / "uv"
    failing.write_text(
        '#!/usr/bin/env bash\necho "uv $*" >> "$FAKE_CALLS"\n'
        'if [[ "$*" == *refresh* ]]; then exit 1; fi\nexit 0\n',
        encoding="utf-8",
    )
    failing.chmod(0o755)
    result = _run("daily-prepare.sh", env, "--date", DAY)
    assert result.returncode == 1
    assert not any("fit" in line for line in _calls(tmp_path))
    assert not (tmp_path / "store" / "store.db.backfilled").exists()
    assert not (tmp_path / "store" / f"prepared-{DAY}").exists()


def test_daily_run_needs_the_store_path(fakes: Path, tmp_path: Path) -> None:
    result = _run("daily-report.sh", _env(fakes, tmp_path), "--date", DAY, "--out-dir", "x")
    assert result.returncode == 2
    assert "HOCKEY_MODEL_DB" in result.stderr


def test_daily_run_needs_a_date(fakes: Path, tmp_path: Path) -> None:
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)))
    result = _run("daily-report.sh", env, "--out-dir", "x")
    assert result.returncode == 2
    assert "--date and --out-dir are required" in result.stderr


def test_daily_run_after_the_morning_refreshes_only_the_starters(
    fakes: Path, tmp_path: Path
) -> None:
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)))
    assert _run("daily-prepare.sh", env, "--date", DAY).returncode == 0
    (tmp_path / "calls.log").unlink()
    out = tmp_path / "out"
    result = _run("daily-report.sh", env, "--date", DAY, "--out-dir", str(out))
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert calls[0] == "uv run hockey-model refresh --starters-only"
    assert not any(" hockey-model fit" in line or " backtest " in line for line in calls)
    assert (out / f"backtest-{DAY}.json").exists()
    assert (out / f"backtest-games-{DAY}.csv").exists()


def test_daily_run_without_the_morning_fails_and_runs_nothing(fakes: Path, tmp_path: Path) -> None:
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)))
    out = tmp_path / "out"
    result = _run("daily-report.sh", env, "--date", DAY, "--out-dir", str(out))
    assert result.returncode == 1
    assert f"no preparation for {DAY}" in result.stderr
    assert f"daily-prepare workflow with date {DAY}" in result.stderr
    assert "came back from the cache" in result.stderr
    assert _calls(tmp_path) == []
    assert not (out / f"backtest-{DAY}.json").exists()


def test_prepare_and_daily_help_document_the_split(fakes: Path, tmp_path: Path) -> None:
    prepare = _run("daily-prepare.sh", _env(fakes, tmp_path), "--help")
    daily = _run("daily-report.sh", _env(fakes, tmp_path), "--help")
    assert prepare.returncode == daily.returncode == 0
    assert "resumes on the next run" in prepare.stdout
    assert "daily-prepare workflow" in daily.stdout


def test_prepare_stops_when_live_features_holds_a_value_that_is_not_a_name(
    fakes: Path, tmp_path: Path
) -> None:
    """A null in the list would vanish in a join and change the feature set."""
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)))
    env["FAKE_BACKTEST_JSON"] = json.dumps({"live_features": [None, "rest"]})
    result = _run("daily-prepare.sh", env, "--date", DAY)
    assert result.returncode == 1
    assert "no live_features list of names" in result.stderr
    assert not any("hockey-model fit" in line for line in _calls(tmp_path))


def test_prepare_stops_when_live_features_is_an_object_of_names(
    fakes: Path, tmp_path: Path
) -> None:
    """An object of strings would iterate as a list of names, so only an array may pass."""
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)))
    env["FAKE_BACKTEST_JSON"] = json.dumps({"live_features": {"a": "rest"}})
    result = _run("daily-prepare.sh", env, "--date", DAY)
    assert result.returncode == 1
    assert "no live_features list of names" in result.stderr
    assert not any("hockey-model fit" in line for line in _calls(tmp_path))


@pytest.mark.parametrize(
    ("script", "flag", "message"),
    [
        ("daily-prepare.sh", "--date", "--date needs a value"),
        ("daily-report.sh", "--date", "--date needs a value"),
        ("daily-report.sh", "--out-dir", "--out-dir needs a directory"),
        ("publish-daily-report.sh", "--date", "--date needs a value"),
        ("publish-daily-report.sh", "--report-dir", "--report-dir needs a directory"),
    ],
)
def test_a_flag_without_its_value_is_a_usage_error(
    fakes: Path, tmp_path: Path, script: str, flag: str, message: str
) -> None:
    """The check is explicit, so the semgrep bash parser reads the script, and the exit is 2."""
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)))
    result = _run(script, env, flag)
    assert result.returncode == 2
    assert message in result.stderr
    assert script.removesuffix(".sh") in result.stderr


def _slow_uv(fakes: Path, tmp_path: Path) -> Path:
    """Put a `uv` ahead of the fake one that waits one second, so a zero budget is exceeded."""
    slow = tmp_path / "slow-bin"
    slow.mkdir()
    (slow / "uv").write_text(
        f'#!/usr/bin/env bash\nsleep 1\nexec "{fakes / "uv"}" "$@"\n', encoding="utf-8"
    )
    (slow / "uv").chmod(0o755)
    return slow


def test_daily_run_logs_each_step_with_its_seconds(fakes: Path, tmp_path: Path) -> None:
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)))
    assert _run("daily-prepare.sh", env, "--date", DAY).returncode == 0
    result = _run("daily-report.sh", env, "--date", DAY, "--out-dir", str(tmp_path / "out"))
    assert result.returncode == 0, result.stderr
    assert "daily-report: step refresh took " in result.stderr
    assert "daily-report: step predict took " in result.stderr
    assert "daily-report: total " in result.stderr


def test_daily_run_fails_loudly_when_the_report_passes_the_budget(
    fakes: Path, tmp_path: Path
) -> None:
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)), REPORT_BUDGET_S="0")
    env["PATH"] = f"{_slow_uv(fakes, tmp_path)}{os.pathsep}{env['PATH']}"
    assert _run("daily-prepare.sh", env | {"REPORT_BUDGET_S": "600"}, "--date", DAY).returncode == 0
    result = _run("daily-report.sh", env, "--date", DAY, "--out-dir", str(tmp_path / "out"))
    assert result.returncode == 1
    assert "over the 0s budget" in result.stderr
    assert "REPORT_BUDGET_S" in result.stderr


@pytest.mark.parametrize("bad", ["ten", "-5", "1.5"])
def test_daily_run_refuses_a_budget_that_is_not_whole_seconds(
    fakes: Path, tmp_path: Path, bad: str
) -> None:
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)), REPORT_BUDGET_S=bad)
    result = _run("daily-report.sh", env, "--date", DAY, "--out-dir", str(tmp_path / "out"))
    assert result.returncode == 2
    assert "REPORT_BUDGET_S must be whole seconds" in result.stderr
    assert _calls(tmp_path) == []


def test_a_failed_step_names_itself_and_stops_the_run(fakes: Path, tmp_path: Path) -> None:
    failing = tmp_path / "failing-bin"
    failing.mkdir()
    (failing / "uv").write_text(
        '#!/usr/bin/env bash\nif [[ "$*" == *"refresh --starters-only"* ]]; then exit 3; fi\n'
        f'exec "{fakes / "uv"}" "$@"\n',
        encoding="utf-8",
    )
    (failing / "uv").chmod(0o755)
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)))
    env["PATH"] = f"{failing}{os.pathsep}{env['PATH']}"
    assert _run("daily-prepare.sh", env, "--date", DAY).returncode == 0
    result = _run("daily-report.sh", env, "--date", DAY, "--out-dir", str(tmp_path / "out"))
    assert result.returncode == 3
    assert "step refresh failed with exit code 3" in result.stderr
    assert not (tmp_path / "out" / f"backtest-{DAY}.json").exists()


def test_a_complete_report_writes_the_marker(fakes: Path, tmp_path: Path) -> None:
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)))
    assert _run("daily-prepare.sh", env, "--date", DAY).returncode == 0
    out = tmp_path / "out"
    assert _run("daily-report.sh", env, "--date", DAY, "--out-dir", str(out)).returncode == 0
    assert (out / "complete").is_file()


def test_a_report_cut_short_writes_no_marker(fakes: Path, tmp_path: Path) -> None:
    failing = tmp_path / "predict-bin"
    failing.mkdir()
    (failing / "uv").write_text(
        '#!/usr/bin/env bash\nif [[ "$*" == *"hockey-model daily"* ]]; then exit 4; fi\n'
        f'exec "{fakes / "uv"}" "$@"\n',
        encoding="utf-8",
    )
    (failing / "uv").chmod(0o755)
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)))
    env["PATH"] = f"{failing}{os.pathsep}{env['PATH']}"
    assert _run("daily-prepare.sh", env, "--date", DAY).returncode == 0
    out = tmp_path / "out"
    assert _run("daily-report.sh", env, "--date", DAY, "--out-dir", str(out)).returncode == 4
    assert not (out / "complete").exists()


def test_a_rerun_clears_the_previous_marker_before_it_writes(fakes: Path, tmp_path: Path) -> None:
    failing = tmp_path / "rerun-bin"
    failing.mkdir()
    (failing / "uv").write_text(
        '#!/usr/bin/env bash\nif [[ "$*" == *"hockey-model daily"* ]]; then exit 4; fi\n'
        f'exec "{fakes / "uv"}" "$@"\n',
        encoding="utf-8",
    )
    (failing / "uv").chmod(0o755)
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)))
    assert _run("daily-prepare.sh", env, "--date", DAY).returncode == 0
    out = tmp_path / "out"
    assert _run("daily-report.sh", env, "--date", DAY, "--out-dir", str(out)).returncode == 0
    assert (out / "complete").is_file()
    env["PATH"] = f"{failing}{os.pathsep}{env['PATH']}"
    assert _run("daily-report.sh", env, "--date", DAY, "--out-dir", str(out)).returncode == 4
    assert not (out / "complete").exists()
