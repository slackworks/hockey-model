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
from hockey_model.store.fits import save_fit


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


class _FrozenDatetime(datetime):
    """A datetime whose now() is a fixed moment."""

    frozen: datetime

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls.frozen


@pytest.mark.parametrize(
    ("moment", "code"),
    [
        (datetime(2026, 11, 4, 12, 0, tzinfo=UTC), 20262027),
        (datetime(2027, 1, 9, 12, 0, tzinfo=UTC), 20262027),
        (datetime(2026, 9, 1, 12, 0, tzinfo=UTC), 20262027),
        (datetime(2026, 8, 31, 12, 0, tzinfo=UTC), 20252026),
        (datetime(2026, 7, 15, 12, 0, tzinfo=UTC), 20252026),
    ],
)
def test_fit_without_a_season_uses_the_shared_season_boundary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    moment: datetime,
    code: int,
) -> None:
    """In July and August the fit goes through the season that just ended, as the cohorts do."""
    _FrozenDatetime.frozen = moment
    monkeypatch.setattr(store_cli, "datetime", _FrozenDatetime)
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(tmp_path / "store.db"))
    assert main(["fit", "--features", ""]) == 0
    assert json.loads(capsys.readouterr().out)["through_season"] == code


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
    save_fit(conn, ELO_ONLY, 20252026, datetime(2026, 11, 1, tzinfo=UTC), [])
    conn.close()
    monkeypatch.setenv("HOCKEY_MODEL_DB", str(db))
    monkeypatch.setenv("GITHUB_SHA", "c0ffee")
    assert main(["daily", "--date", "2026-11-04", "--out-dir", str(tmp_path)]) == 0
    capsys.readouterr()
    payload = json.loads((tmp_path / "report-2026-11-04.json").read_text(encoding="utf-8"))
    assert payload["model"]["code_sha"] == "c0ffee"
    assert payload["model"]["features"] == []
    assert len(payload["model"]["fit_sha256"]) == 64
