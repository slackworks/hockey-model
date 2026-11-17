"""Tests for the `report-due` and `daily` store commands."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from seeding import add_game

from hockey_model import store_cli
from hockey_model.cli import main
from hockey_model.game_model.logistic import ELO_ONLY
from hockey_model.sources.nhl import GameRow
from hockey_model.store.db import connect
from hockey_model.store.fits import FitRecord, save_fit


def _row(game_id: int, start: str) -> GameRow:
    when = datetime.fromisoformat(start).replace(tzinfo=UTC)
    return GameRow(game_id, 20262027, 2, when, "TOR", "BOS", "FUT", None, None, None)


def _feed(monkeypatch: pytest.MonkeyPatch, by_day: dict[date, list[GameRow]]) -> list[date]:
    """Serve one fake score feed per day and return the list of days that were read."""
    requested: list[date] = []

    def fake_fetch(client: object, day: date) -> list[GameRow]:
        requested.append(day)
        return by_day.get(day, [])

    monkeypatch.setattr(store_cli, "fetch_score_day", fake_fetch)
    return requested


def test_report_due_keys_the_slate_to_its_eastern_game_day(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The Nov 4 Eastern slate: a 7 pm EST game and a 10 pm EST game, both on Nov 5 in UTC.
    # The third row has a start outside the Eastern day, so the window drops it.
    feed = {
        date(2026, 11, 4): [
            _row(1, "2026-11-05T00:00"),
            _row(2, "2026-11-05T03:00"),
            _row(3, "2026-11-05T06:00"),
        ],
    }
    requested = _feed(monkeypatch, feed)
    assert main(["report-due", "--date", "2026-11-04", "--force"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert requested == [date(2026, 11, 4)]
    assert payload["games"] == 2
    assert payload["first_puck_drop_utc"] == "2026-11-05T00:00:00.000000+00:00"
    assert payload["tag"] == "reports-2026-11-04"
    assert payload["due"] is True


def test_report_due_prints_only_the_gate_fields(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _feed(monkeypatch, {})
    assert main(["report-due", "--date", "2026-11-04"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "date",
        "tag",
        "games",
        "first_puck_drop_utc",
        "puck_drop_passed",
        "prep_too_early",
        "due",
    }


def test_report_due_without_games_uses_the_end_of_the_day(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _feed(monkeypatch, {})
    assert main(["report-due", "--date", "2026-11-04"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["games"] == 0
    assert payload["first_puck_drop_utc"] == "2026-11-05T05:00:00.000000+00:00"


def test_fit_without_a_season_uses_the_newest_season_in_the_store(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The cutoff follows the data, so the clock cannot name a season the fit never read."""
    db = tmp_path / "store.db"
    conn = connect(db)
    add_game(conn, 1, "2024-11-01T00:00", season=20242025)
    add_game(conn, 2, "2025-11-01T00:00", season=20252026)
    # A scheduled game of a later season did not train the fit, so it does not set the cutoff.
    add_game(conn, 3, "2026-12-01T00:00", season=20262027, state="FUT")
    conn.commit()
    conn.close()
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(db))
    assert main(["fit", "--features", ""]) == 0
    assert json.loads(capsys.readouterr().out)["through_season"] == 20252026


def test_fit_without_a_season_in_an_empty_store_says_to_refresh(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(tmp_path / "store.db"))
    assert main(["fit", "--features", ""]) == 2
    assert "hockey-model refresh" in capsys.readouterr().err


def test_daily_requires_an_out_dir(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["daily", "--date", "2026-11-04"]) == 2
    assert "--out-dir" in capsys.readouterr().err


def test_daily_refuses_a_day_with_no_model_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(tmp_path / "store.db"))
    assert main(["daily", "--date", "2026-11-04", "--out-dir", str(tmp_path)]) == 2
    assert "error:" in capsys.readouterr().err


def test_refresh_refuses_starters_only_with_a_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(tmp_path / "store.db"))
    code = main(["refresh", "--starters-only", "--backfill-from", "2014-10-01"])
    assert code == 2
    assert "drop --starters-only" in capsys.readouterr().err


def test_daily_names_the_fit_and_the_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "store.db"
    conn = connect(db)
    save_fit(conn, FitRecord(ELO_ONLY, 20252026, datetime(2026, 11, 1, tzinfo=UTC), label="live"))
    conn.close()
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(db))
    monkeypatch.setenv("GITHUB_SHA", "c0ffee")
    assert main(["daily", "--date", "2026-11-04", "--out-dir", str(tmp_path)]) == 0
    capsys.readouterr()
    payload = json.loads((tmp_path / "report-2026-11-04.json").read_text(encoding="utf-8"))
    assert payload["model"]["code_sha"] == "c0ffee"
    assert payload["model"]["features"] == []
    assert len(payload["model"]["fit_sha256"]) == 64


def test_report_due_never_skips_a_day_that_is_not_the_current_game_day(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A mistyped future date must not read as "prep is early": prep_too_early is for today only.
    _feed(monkeypatch, {})
    assert main(["report-due", "--date", "2099-01-01"]) == 0
    assert json.loads(capsys.readouterr().out)["prep_too_early"] is False
