"""Tests for upgrade-store.sh and the two daily scripts that call it."""

import json
from pathlib import Path

import pytest
from report_script_support import DAY, TAG, _calls, _env, _run

# The mutation run copies src and tests only, so the scripts are not there to test.
if "mutants" in Path(__file__).resolve().parts:
    pytest.skip("the mutation copy has no scripts directory", allow_module_level=True)

UPGRADE = "uv run hockey-model store upgrade"


def _store(tmp_path: Path) -> Path:
    return tmp_path / "store" / "store.db"


def _store_file(tmp_path: Path) -> Path:
    """Make the store file that the cache would restore, so the upgrade has one to move."""
    path = _store(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def _prepared_day(tmp_path: Path) -> None:
    """Make the marker and organic files a morning prepare run leaves for the report."""
    store_dir = _store_file(tmp_path).parent
    (store_dir / f"prepared-{DAY}").touch()
    (store_dir / f"backtest-{DAY}.json").write_text("{}", encoding="utf-8")
    (store_dir / f"backtest-games-{DAY}.csv").write_text("game_id\n", encoding="utf-8")


def test_upgrade_script_moves_the_store_it_is_given(fakes: Path, tmp_path: Path) -> None:
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store_file(tmp_path)))
    result = _run("upgrade-store.sh", env)
    assert result.returncode == 0, result.stderr
    assert _calls(tmp_path) == [UPGRADE]
    assert "upgrade-store: " in result.stderr


def test_upgrade_script_skips_a_missing_store_file(fakes: Path, tmp_path: Path) -> None:
    """The first backfill writes a current store, so a missing file is not an error."""
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)))
    result = _run("upgrade-store.sh", env)
    assert result.returncode == 0, result.stderr
    assert "no store file at" in result.stderr
    assert _calls(tmp_path) == []


def test_upgrade_script_stops_on_a_failed_upgrade(fakes: Path, tmp_path: Path) -> None:
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store_file(tmp_path)), FAKE_UPGRADE_FAIL="1")
    result = _run("upgrade-store.sh", env)
    assert result.returncode == 1
    assert "did not upgrade" in result.stderr


def test_upgrade_script_needs_the_store_path(fakes: Path, tmp_path: Path) -> None:
    result = _run("upgrade-store.sh", _env(fakes, tmp_path))
    assert result.returncode == 2
    assert "HOCKEY_MODEL_DB" in result.stderr


def test_upgrade_script_rejects_an_unknown_argument(fakes: Path, tmp_path: Path) -> None:
    result = _run("upgrade-store.sh", _env(fakes, tmp_path), "--db", "x")
    assert result.returncode == 2
    assert "unknown argument" in result.stderr


def test_upgrade_script_documents_itself(fakes: Path, tmp_path: Path) -> None:
    result = _run("upgrade-store.sh", _env(fakes, tmp_path), "--help")
    assert result.returncode == 0
    assert "HOCKEY_MODEL_DB" in result.stdout
    assert "backup" in result.stdout


def test_prepare_upgrades_the_store_before_it_reads_anything(fakes: Path, tmp_path: Path) -> None:
    """A restored store from older code must upgrade before the first refresh opens it."""
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store_file(tmp_path)))
    result = _run("daily-prepare.sh", env, "--date", DAY)
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert calls[0] == UPGRADE
    assert any("refresh" in line for line in calls)


def test_prepare_skips_the_upgrade_when_no_store_file_exists(fakes: Path, tmp_path: Path) -> None:
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store(tmp_path)))
    result = _run("daily-prepare.sh", env, "--date", DAY)
    assert result.returncode == 0, result.stderr
    assert not any("store upgrade" in line for line in _calls(tmp_path))
    assert "the first refresh creates it" in result.stderr


def test_prepare_stops_before_the_refresh_when_the_upgrade_fails(
    fakes: Path, tmp_path: Path
) -> None:
    """A store that cannot upgrade must not be read by a later step."""
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store_file(tmp_path)), FAKE_UPGRADE_FAIL="1")
    result = _run("daily-prepare.sh", env, "--date", DAY)
    assert result.returncode != 0
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
        HOCKEY_MODEL_DB=str(_store(tmp_path)),
        FAKE_DUE_JSON=json.dumps(
            {"due": True, "tag": TAG, "date": DAY, "games": 3, "prep_too_early": False}
        ),
    )
    result = _run("daily-report.sh", env, "--date", DAY, "--out-dir", str(tmp_path / "out"))
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert calls[0] == UPGRADE
    assert any("refresh --starters-only" in line for line in calls)


def test_the_report_stops_when_the_upgrade_fails(fakes: Path, tmp_path: Path) -> None:
    _prepared_day(tmp_path)
    env = _env(
        fakes,
        tmp_path,
        HOCKEY_MODEL_DB=str(_store(tmp_path)),
        FAKE_UPGRADE_FAIL="1",
    )
    result = _run("daily-report.sh", env, "--date", DAY, "--out-dir", str(tmp_path / "out"))
    assert result.returncode != 0
    assert not any("refresh" in line or "hockey-model daily" in line for line in _calls(tmp_path))
    assert not (tmp_path / "out" / "complete").exists()


def test_the_workflow_saves_the_upgraded_store(fakes: Path, tmp_path: Path) -> None:
    """The upgrade edits the file the cache saves, so the saved store is current."""
    env = _env(fakes, tmp_path, HOCKEY_MODEL_DB=str(_store_file(tmp_path)))
    assert _run("daily-prepare.sh", env, "--date", DAY).returncode == 0
    assert UPGRADE in _calls(tmp_path)


@pytest.mark.parametrize("script", ["daily-prepare.sh", "daily-report.sh"])
def test_the_upgrade_runs_only_when_the_store_path_is_set(
    fakes: Path, tmp_path: Path, script: str
) -> None:
    """Neither script may run the upgrade without a store path."""
    env = _env(fakes, tmp_path)
    env.pop("HOCKEY_MODEL_DB", None)
    args = ["--date", DAY] if script == "daily-prepare.sh" else ["--date", DAY, "--out-dir", "x"]
    result = _run(script, env, *args)
    assert result.returncode == 2
    assert not any("store upgrade" in line for line in _calls(tmp_path))
