"""Tests that the daily scripts upgrade a store that older code wrote, before they read it."""

import json
from pathlib import Path

import pytest
from report_script_support import DAY, TAG, _calls, _env, _run, store_path

# The mutation run copies src and tests only, so the scripts are not there to test.
if "mutants" in Path(__file__).resolve().parts:
    pytest.skip("the mutation copy has no scripts directory", allow_module_level=True)

UPGRADE = "uv run hockey-model store upgrade"


def _store_file(tmp_path: Path) -> Path:
    """Make the store file that the cache would restore, so the upgrade has one to move."""
    path = store_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def _prepared_day(tmp_path: Path) -> None:
    """Make the marker and the files that a morning prepare run leaves for the report."""
    store_dir = _store_file(tmp_path).parent
    (store_dir / f"prepared-{DAY}").touch()
    (store_dir / f"backtest-{DAY}.json").write_text("{}", encoding="utf-8")
    (store_dir / f"backtest-games-{DAY}.csv").write_text("game_id\n", encoding="utf-8")


def test_prepare_upgrades_the_store_before_it_reads_anything(fakes: Path, tmp_path: Path) -> None:
    """A restored store from older code must upgrade before the first refresh opens it."""
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store_file(tmp_path)))
    result = _run("daily-prepare.sh", env, "--date", DAY)
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert calls[0] == f"{UPGRADE} --if-missing skip"
    assert any("refresh" in line for line in calls)


def test_prepare_stops_before_the_refresh_when_the_upgrade_fails(
    fakes: Path, tmp_path: Path
) -> None:
    """A store that cannot upgrade must not be read by a later step."""
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store_file(tmp_path)), FAKE_UPGRADE_FAIL="1")
    result = _run("daily-prepare.sh", env, "--date", DAY)
    assert result.returncode == 2
    assert "upgrade boom" in result.stderr
    assert not any("refresh" in line for line in _calls(tmp_path))
    assert not (tmp_path / "store" / f"prepared-{DAY}").exists()


def test_the_report_upgrades_the_store_before_it_reads_anything(
    fakes: Path, tmp_path: Path
) -> None:
    """The cache can hold a store from older code, and the report reads it directly."""
    _prepared_day(tmp_path)
    env = _env(
        fakes,
        tmp_path,
        HOCKEY_MODEL_DB=str(store_path(tmp_path)),
        FAKE_DUE_JSON=json.dumps(
            {"due": True, "tag": TAG, "date": DAY, "games": 3, "prep_too_early": False}
        ),
    )
    result = _run("daily-report.sh", env, "--date", DAY, "--out-dir", str(tmp_path / "out"))
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert calls[0] == UPGRADE
    assert any("refresh --starters-only" in line for line in calls)
    assert "step upgrade took" in result.stderr


def test_the_report_stops_when_the_upgrade_fails(fakes: Path, tmp_path: Path) -> None:
    """The step log names the upgrade, and its own exit code reaches the caller."""
    _prepared_day(tmp_path)
    env = _env(
        fakes,
        tmp_path,
        HOCKEY_MODEL_DB=str(store_path(tmp_path)),
        FAKE_UPGRADE_FAIL="1",
    )
    result = _run("daily-report.sh", env, "--date", DAY, "--out-dir", str(tmp_path / "out"))
    assert result.returncode == 2
    assert "step upgrade failed with exit code 2" in result.stderr
    assert not any("refresh" in line or "hockey-model daily" in line for line in _calls(tmp_path))
    assert not (tmp_path / "out" / "complete").exists()


def test_the_report_names_a_missing_store_as_missing_preparation(
    fakes: Path, tmp_path: Path
) -> None:
    """A cache that restored no store must not read as a fit that was never saved."""
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(store_path(tmp_path)))
    result = _run("daily-report.sh", env, "--date", DAY, "--out-dir", str(tmp_path / "out"))
    assert result.returncode == 1
    assert "no preparation" in result.stderr
    assert not any("store upgrade" in line for line in _calls(tmp_path))
