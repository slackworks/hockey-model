"""Tests that daily-prepare.sh skips a day that is already prepared, and that --force rebuilds."""

import json
from pathlib import Path

import pytest
from report_script_support import DAY, LIVE, _calls, _env, _run, store_path

from hockey_model.backtest.row_cache import CACHE_DIR_NAME

OTHER_DAY = "2026-11-05"
EARLIER_DAY = "2026-11-01"
SKIP_WORDS = f"{DAY} is already prepared"


def _store_dir(tmp_path: Path) -> Path:
    path = store_path(tmp_path).parent
    path.mkdir(parents=True, exist_ok=True)
    return path


def _prepare_day(tmp_path: Path, day: str = DAY, backtest: str | None = None) -> None:
    """Leave the marker and the backtest file that a finished prepare run leaves."""
    store_dir = _store_dir(tmp_path)
    (store_dir / f"prepared-{day}").touch()
    text = json.dumps({"live_features": LIVE}) if backtest is None else backtest
    (store_dir / f"backtest-{day}.json").write_text(text, encoding="utf-8")


def _prepare_env(fakes: Path, tmp_path: Path) -> dict[str, str]:
    # A marker file keeps the run off the first-backfill path.
    store = store_path(tmp_path)
    _store_dir(tmp_path)
    Path(f"{store}.backfilled").touch()
    return _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(store))


def _ran_backtest_or_fit(tmp_path: Path) -> bool:
    return any("hockey-model backtest" in c or "hockey-model fit" in c for c in _calls(tmp_path))


def test_a_prepared_day_skips_the_backtest_and_the_fit(fakes: Path, tmp_path: Path) -> None:
    _prepare_day(tmp_path)
    result = _run("daily-prepare.sh", _prepare_env(fakes, tmp_path), "--date", DAY)
    assert result.returncode == 0, result.stderr
    assert SKIP_WORDS in result.stderr
    assert "pass --force to rebuild" in result.stderr
    assert result.stdout == ""
    assert not _ran_backtest_or_fit(tmp_path)


def test_a_skip_keeps_the_files_of_the_day(fakes: Path, tmp_path: Path) -> None:
    _prepare_day(tmp_path)
    _run("daily-prepare.sh", _prepare_env(fakes, tmp_path), "--date", DAY)
    store_dir = _store_dir(tmp_path)
    assert (store_dir / f"prepared-{DAY}").exists()
    assert (store_dir / f"backtest-{DAY}.json").exists()


def test_a_skip_still_runs_the_refresh(fakes: Path, tmp_path: Path) -> None:
    _prepare_day(tmp_path)
    _run("daily-prepare.sh", _prepare_env(fakes, tmp_path), "--date", DAY)
    assert "uv run hockey-model refresh" in _calls(tmp_path)


def test_force_rebuilds_a_prepared_day(fakes: Path, tmp_path: Path) -> None:
    _prepare_day(tmp_path)
    result = _run("daily-prepare.sh", _prepare_env(fakes, tmp_path), "--date", DAY, "--force")
    assert result.returncode == 0, result.stderr
    assert SKIP_WORDS not in result.stderr
    assert _ran_backtest_or_fit(tmp_path)
    assert (_store_dir(tmp_path) / f"prepared-{DAY}").exists()


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(None, id="missing"),
        pytest.param("not json", id="invalid_json"),
        pytest.param("{}", id="no_live_features"),
        pytest.param(json.dumps({"live_features": [None, "rest"]}), id="not_a_name"),
        pytest.param(json.dumps({"live_features": {"a": "b"}}), id="an_object"),
    ],
)
def test_a_marker_without_a_valid_backtest_rebuilds(
    fakes: Path, tmp_path: Path, text: str | None
) -> None:
    _prepare_day(tmp_path, backtest="" if text is None else text)
    if text is None:
        (_store_dir(tmp_path) / f"backtest-{DAY}.json").unlink()
    result = _run("daily-prepare.sh", _prepare_env(fakes, tmp_path), "--date", DAY)
    assert result.returncode == 0, result.stderr
    assert "rebuilding" in result.stderr
    assert _ran_backtest_or_fit(tmp_path)


def test_the_marker_of_another_day_does_not_skip(fakes: Path, tmp_path: Path) -> None:
    _prepare_day(tmp_path, day=OTHER_DAY)
    result = _run("daily-prepare.sh", _prepare_env(fakes, tmp_path), "--date", DAY)
    assert result.returncode == 0, result.stderr
    assert "already prepared" not in result.stderr
    assert _ran_backtest_or_fit(tmp_path)


def test_a_backtest_file_without_the_marker_rebuilds(fakes: Path, tmp_path: Path) -> None:
    (_store_dir(tmp_path) / f"backtest-{DAY}.json").write_text(
        json.dumps({"live_features": LIVE}), encoding="utf-8"
    )
    result = _run("daily-prepare.sh", _prepare_env(fakes, tmp_path), "--date", DAY)
    assert result.returncode == 0, result.stderr
    assert _ran_backtest_or_fit(tmp_path)


def test_prepare_help_names_force(fakes: Path, tmp_path: Path) -> None:
    result = _run("daily-prepare.sh", _prepare_env(fakes, tmp_path), "--help")
    assert result.returncode == 0
    assert "--force" in result.stdout


def test_a_run_for_a_past_day_keeps_the_files_of_today(fakes: Path, tmp_path: Path) -> None:
    """A manual run for an earlier day must not remove the marker of a later day."""
    _prepare_day(tmp_path, day=OTHER_DAY)
    result = _run("daily-prepare.sh", _prepare_env(fakes, tmp_path), "--date", DAY)
    assert result.returncode == 0, result.stderr
    store_dir = _store_dir(tmp_path)
    assert (store_dir / f"prepared-{OTHER_DAY}").exists()
    assert (store_dir / f"backtest-{OTHER_DAY}.json").exists()


def test_a_run_removes_the_files_of_earlier_days(fakes: Path, tmp_path: Path) -> None:
    _prepare_day(tmp_path, day=EARLIER_DAY)
    result = _run("daily-prepare.sh", _prepare_env(fakes, tmp_path), "--date", DAY)
    assert result.returncode == 0, result.stderr
    store_dir = _store_dir(tmp_path)
    assert not (store_dir / f"prepared-{EARLIER_DAY}").exists()
    assert not (store_dir / f"backtest-{EARLIER_DAY}.json").exists()


@pytest.mark.parametrize("failing", ["FAKE_BACKTEST_FAIL", "FAKE_FIT_FAIL"])
def test_a_failed_forced_rebuild_keeps_the_prepared_day(
    fakes: Path, tmp_path: Path, failing: str
) -> None:
    _prepare_day(tmp_path)
    env = _prepare_env(fakes, tmp_path) | {failing: "1"}
    result = _run("daily-prepare.sh", env, "--date", DAY, "--force")
    assert result.returncode != 0
    store_dir = _store_dir(tmp_path)
    assert (store_dir / f"prepared-{DAY}").exists()
    assert json.loads((store_dir / f"backtest-{DAY}.json").read_text(encoding="utf-8")) == {
        "live_features": LIVE
    }


def test_a_rebuild_replaces_the_backtest_files(fakes: Path, tmp_path: Path) -> None:
    _prepare_day(tmp_path, backtest=json.dumps({"live_features": ["old"]}))
    result = _run("daily-prepare.sh", _prepare_env(fakes, tmp_path), "--date", DAY, "--force")
    assert result.returncode == 0, result.stderr
    store_dir = _store_dir(tmp_path)
    assert json.loads((store_dir / f"backtest-{DAY}.json").read_text(encoding="utf-8")) == {
        "live_features": LIVE
    }
    assert (store_dir / f"backtest-games-{DAY}.csv").exists()
    assert not list(store_dir.glob("*.new"))


def test_a_backtest_that_will_not_parse_names_the_parse_error(fakes: Path, tmp_path: Path) -> None:
    _prepare_day(tmp_path, backtest="{not json")
    result = _run("daily-prepare.sh", _prepare_env(fakes, tmp_path), "--date", DAY)
    assert result.returncode == 0, result.stderr
    assert "has no live_features list of names (" in result.stderr
    assert "parse error" in result.stderr.lower()


@pytest.mark.parametrize(("extra", "expected"), [({}, "4"), ({"BACKTEST_WORKERS": "3"}, "3")])
def test_the_backtest_and_the_fit_get_the_worker_count(
    fakes: Path, tmp_path: Path, extra: dict[str, str], expected: str
) -> None:
    env = _prepare_env(fakes, tmp_path) | extra
    result = _run("daily-prepare.sh", env, "--date", DAY)
    assert result.returncode == 0, result.stderr
    calls = [c for c in _calls(tmp_path) if "hockey-model backtest" in c or "hockey-model fit" in c]
    assert len(calls) == 2
    assert all(call.endswith(f"--workers {expected}") for call in calls)


def _cache_file(tmp_path: Path) -> Path:
    directory = _store_dir(tmp_path) / CACHE_DIR_NAME
    directory.mkdir(exist_ok=True)
    path = directory / "actual-3-tag-key.json"
    path.write_text("[]")
    return path


def test_force_deletes_the_season_row_cache(fakes: Path, tmp_path: Path) -> None:
    _prepare_day(tmp_path)
    cached = _cache_file(tmp_path)
    result = _run("daily-prepare.sh", _prepare_env(fakes, tmp_path), "--date", DAY, "--force")
    assert result.returncode == 0, result.stderr
    assert not cached.parent.exists()


def test_a_skip_and_an_unforced_rebuild_keep_the_season_row_cache(
    fakes: Path, tmp_path: Path
) -> None:
    _prepare_day(tmp_path)
    cached = _cache_file(tmp_path)
    _run("daily-prepare.sh", _prepare_env(fakes, tmp_path), "--date", DAY)
    assert cached.exists()
    _run("daily-prepare.sh", _prepare_env(fakes, tmp_path), "--date", OTHER_DAY)
    assert cached.exists()


def test_force_finds_the_cache_beside_the_file_that_a_link_names(
    fakes: Path, tmp_path: Path
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "store.db").touch()
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "store.db").symlink_to(real / "store.db")
    (linked / "store.db.backfilled").touch()
    cache = real / CACHE_DIR_NAME
    cache.mkdir()
    (cache / "actual-3-tag-key.json").write_text("[]")
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(linked / "store.db"))
    result = _run("daily-prepare.sh", env, "--date", DAY, "--force")
    assert result.returncode == 0, result.stderr
    assert not cache.exists()
