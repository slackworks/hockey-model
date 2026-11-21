"""Tests for the `report-due` and `daily` store commands."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

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


def test_report_due_counts_the_games_that_fall_outside_the_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A feed with the wrong date drops every game, so the count must reach the caller."""
    feed = {
        date(2026, 11, 4): [
            _row(1, "2026-11-05T00:00"),
            _row(2, "2026-11-05T06:00"),
            _row(3, "2026-11-05T08:00"),
        ],
    }
    _feed(monkeypatch, feed)
    assert main(["report-due", "--date", "2026-11-04", "--force"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert (payload["games"], payload["games_outside_window"]) == (1, 2)
    assert "2 feed game(s) start outside" in captured.err
    assert "2, 3" in captured.err


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
        "games_outside_window",
        "first_puck_drop_utc",
        "puck_drop_passed",
        "prep_blocks_report",
        "due",
    }
    assert payload["games_outside_window"] == 0


def test_report_due_without_games_uses_the_end_of_the_day(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _feed(monkeypatch, {})
    assert main(["report-due", "--date", "2026-11-04"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["games"] == 0
    assert payload["first_puck_drop_utc"] == "2026-11-05T05:00:00.000000+00:00"


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


def test_report_due_says_a_future_day_has_no_puck_drop_yet(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _feed(monkeypatch, {})
    assert main(["report-due", "--date", "2099-01-01"]) == 0
    assert json.loads(capsys.readouterr().out)["puck_drop_passed"] is False


def test_report_due_reads_no_prepare_run_as_no_block(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # No run holds the store group, so no run can hold it past the puck drop.
    _feed(monkeypatch, {})
    assert main(["report-due", "--date", "2026-11-04"]) == 0
    assert json.loads(capsys.readouterr().out)["prep_blocks_report"] is False


def _slate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve the Nov 4 slate with one 7 pm EST game, which starts at midnight UTC on Nov 5."""
    _feed(monkeypatch, {date(2026, 11, 4): [_row(1, "2026-11-05T00:00")]})


def test_report_due_says_a_late_prepare_blocks_the_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A prepare that started at 22:00 runs until 01:00 UTC, past the midnight puck drop.
    _slate(monkeypatch)
    args = ["report-due", "--date", "2026-11-04", "--prep-started", "2026-11-04T22:00:00Z"]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["prep_blocks_report"] is True


def test_report_due_says_an_on_time_prepare_blocks_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The 08:00 prepare plus 180 minutes ends at 11:00, well before the midnight puck drop.
    _slate(monkeypatch)
    args = ["report-due", "--date", "2026-11-04", "--prep-started", "2026-11-04T08:00:00Z"]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["prep_blocks_report"] is False


def test_report_due_counts_the_report_budget_after_the_prepare(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A prepare that ends a minute before the puck drop leaves no time for the report."""
    _slate(monkeypatch)
    base = ["report-due", "--date", "2026-11-04", "--prep-started", "2026-11-04T20:58:00Z"]
    assert main(base) == 0
    assert json.loads(capsys.readouterr().out)["prep_blocks_report"] is False
    assert main([*base, "--margin-seconds", "600"]) == 0
    assert json.loads(capsys.readouterr().out)["prep_blocks_report"] is True
