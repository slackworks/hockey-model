"""Tests for the daily prediction report, built from a seeded store."""

import json
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from seeding import add_game, add_goalie, add_team_form, utc

from hockey_model.daily_report import (
    DailyLine,
    DailyReport,
    build_report,
    model_identity,
    report_json,
    report_markdown,
    report_tag,
    write_report,
)
from hockey_model.game import DegradedInput, GamePrediction, MissingKind
from hockey_model.game_model.features import GoalieMode
from hockey_model.game_model.logistic import ELO_ONLY, FitDiagnostics, LogisticFit
from hockey_model.game_model.nhl_model import NhlGameModel
from hockey_model.sources.nhl import GameRow
from hockey_model.types import HockeyModelError, Projection

DAY = date(2026, 1, 20)
NOW = utc("2026-01-20T12:00")


def _model(store: sqlite3.Connection) -> NhlGameModel:
    return NhlGameModel(store, ELO_ONLY, GoalieMode.GUESS)


def _seed_form(store: sqlite3.Connection) -> None:
    add_team_form(store, 1, "TOR", 3.5, 2.0)
    add_team_form(store, 2, "BOS", 2.0, 3.0)


def test_report_tag_is_the_utc_day() -> None:
    assert report_tag(DAY) == "reports-2026-01-20"


def test_day_with_no_games_has_no_lines(store: sqlite3.Connection) -> None:
    report = build_report(store, _model(store), DAY, now=NOW)
    assert report.lines == ()
    assert "No games start on this day." in report_markdown(report)
    assert report_json(report)["games"] == []


def test_upcoming_game_is_predicted_with_no_late_run(store: sqlite3.Connection) -> None:
    _seed_form(store)
    add_game(store, 10, "2026-01-20T23:00", "TOR", "BOS", state="FUT")
    report = build_report(store, _model(store), DAY, now=NOW)
    (line,) = report.lines
    assert line.prediction is not None
    assert line.prediction.made_after_start is False
    assert line.prediction.home_win + line.prediction.away_win == pytest.approx(1.0)


def test_game_over_gets_a_prediction_as_known_at_puck_drop(store: sqlite3.Connection) -> None:
    # A late first report of the day is still full: it predicts every game that has a puck drop.
    _seed_form(store)
    add_game(store, 11, "2026-01-20T11:00", "TOR", "BOS", state="FINAL")
    report = build_report(store, _model(store), DAY, now=NOW)
    (line,) = report.lines
    assert line.prediction is not None
    assert line.prediction.made_after_start is True
    assert line.prediction.home_win + line.prediction.away_win == pytest.approx(1.0)
    game = report_json(report)["games"][0]
    assert game["state"] == "FINAL"
    assert game["home_win"] is not None
    assert game["skipped"] is None


def test_postponed_game_gets_no_prediction(store: sqlite3.Connection) -> None:
    _seed_form(store)
    add_game(store, 14, "2026-01-20T23:00", "TOR", "BOS", state="PPD")
    report = build_report(store, _model(store), DAY, now=NOW)
    (line,) = report.lines
    assert line.prediction is None
    assert "postponed (state PPD)" in report_markdown(report)


def test_in_progress_game_is_not_treated_as_final(store: sqlite3.Connection) -> None:
    _seed_form(store)
    add_game(store, 12, "2026-01-20T11:00", "TOR", "BOS", state="LIVE")
    report = build_report(store, _model(store), DAY, now=NOW)
    (line,) = report.lines
    assert line.prediction is not None


def test_started_game_is_a_late_run_with_inputs_as_of_start(store: sqlite3.Connection) -> None:
    _seed_form(store)
    add_game(store, 13, "2026-01-20T10:00", "TOR", "BOS", state="LIVE")
    report = build_report(store, _model(store), DAY, now=NOW)
    (line,) = report.lines
    assert line.prediction is not None
    assert line.prediction.made_after_start is True
    kinds = {m.kind for m in line.prediction.missing}
    assert MissingKind.LATE_RUN in kinds
    assert "late_run" in report_markdown(report)


@pytest.mark.parametrize("state", ["FUT", "PRE", "LIVE", "CRIT", "FINAL", "OFF", "PPD"])
def test_only_a_postponed_game_gets_no_prediction(store: sqlite3.Connection, state: str) -> None:
    _seed_form(store)
    add_game(store, 20, "2026-01-20T11:00", "TOR", "BOS", state=state)
    (line,) = build_report(store, _model(store), DAY, now=NOW).lines
    assert (line.prediction is None) == (state == "PPD")


def test_a_finished_games_own_score_does_not_change_its_prediction(
    store: sqlite3.Connection,
) -> None:
    _seed_form(store)
    homes = []
    # A later read replaces the earlier one, so the second score is the one the report sees.
    for score, fetched in (((9, 0), utc("2026-01-20T13:00")), ((0, 9), utc("2026-01-20T14:00"))):
        add_game(
            store,
            21,
            "2026-01-20T11:00",
            "TOR",
            "BOS",
            state="FINAL",
            score=score,
            fetched_at=fetched,
        )
        (line,) = build_report(store, _model(store), DAY, now=NOW).lines
        assert line.prediction is not None
        homes.append(line.prediction.home_win)
    assert homes[0] == homes[1]


def test_a_finished_game_in_reads_mode_names_the_boxscore_starter(
    store: sqlite3.Connection,
) -> None:
    _seed_form(store)
    add_game(store, 22, "2026-01-20T11:00", "TOR", "BOS", state="FINAL")
    add_goalie(store, 22, "TOR", 8001, "A. Home")
    add_goalie(store, 22, "BOS", 8002, "B. Away")
    model = NhlGameModel(store, ELO_ONLY, GoalieMode.READS)
    (line,) = build_report(store, model, DAY, now=NOW).lines
    assert line.prediction is not None
    notes = [m.note for m in line.prediction.missing if m.kind == MissingKind.LATE_RUN]
    assert any("boxscore starter" in note for note in notes)


def test_naive_now_is_an_error(store: sqlite3.Connection) -> None:
    with pytest.raises(HockeyModelError, match="must be a timezone-aware UTC datetime"):
        build_report(store, _model(store), DAY, now=datetime(2026, 1, 20, 12, 0))


def test_non_utc_offset_now_is_an_error_even_with_no_games(store: sqlite3.Connection) -> None:
    eastern = NOW.astimezone(timezone(timedelta(hours=5)))
    with pytest.raises(HockeyModelError, match="offset"):
        build_report(store, _model(store), DAY, now=eastern)


def test_write_report_writes_markdown_and_json(store: sqlite3.Connection, tmp_path: Path) -> None:
    report = build_report(store, _model(store), DAY, now=NOW)
    md_path, json_path = write_report(report, tmp_path)
    assert md_path.name == "report-2026-01-20.md"
    assert json_path.name == "report-2026-01-20.json"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["date"] == "2026-01-20"
    assert md_path.read_text(encoding="utf-8").startswith("# NHL predictions for 2026-01-20")


def test_remote_state_cannot_break_the_markdown_table(tmp_path: Path) -> None:
    hostile = GameRow(
        99, 20252026, 2, utc("2026-01-20T23:00"), "TOR", "BOS", "A|B\nC", None, None, None
    )
    report = DailyReport(DAY, NOW, 0.8, (DailyLine(hostile, None, "weird | state"),))
    table = [line for line in report_markdown(report).splitlines() if line.startswith("| BOS at")]
    assert len(table) == 1
    assert table[0].count("\\|") == 2
    assert "A\\|B C" in table[0]
    assert "weird \\| state" in table[0]


@given(st.lists(st.booleans(), max_size=6), st.integers(min_value=0, max_value=3))
def test_json_games_share_keys_and_credits_are_the_unique_union(
    predicted: list[bool], credit_count: int
) -> None:
    lines = [
        DailyLine(_row(index + 1), _prediction(credit_count) if has else None, "")
        for index, has in enumerate(predicted)
    ]
    payload = report_json(DailyReport(DAY, NOW, 0.8, tuple(lines)))
    assert len(payload["games"]) == len(predicted)
    assert len({frozenset(game) for game in payload["games"]}) <= 1
    expected = [f"src{n}" for n in range(credit_count)] if any(predicted) else []
    assert payload["credits"] == expected


def _row(game_id: int) -> GameRow:
    when = utc("2026-01-20T23:00")
    return GameRow(game_id, 20252026, 2, when, "TOR", "BOS", "FUT", None, None, None)


def _prediction(credit_count: int) -> GamePrediction:
    point = Projection(3.0, 2.0, 4.0, 0.8)
    credits = tuple(f"src{n}" for n in range(credit_count))
    return GamePrediction(0.5, 0.5, point, point, (), credits)


def _fit(intercept: float = 0.1) -> LogisticFit:
    return LogisticFit(("xg_form", "rest"), intercept, 1.0, {"xg_form": 0.4, "rest": 0.05})


def test_model_identity_is_stable_and_follows_the_weights() -> None:
    first = model_identity(_fit(), "abc123")
    assert first == model_identity(_fit(), "abc123")
    assert first.fit_sha256 != model_identity(_fit(intercept=0.2), "abc123").fit_sha256
    assert first.features == ("xg_form", "rest")
    assert model_identity(_fit(), "").code_sha is None


def test_model_identity_ignores_the_fit_diagnostics() -> None:
    plain = _fit()
    diagnostics = FitDiagnostics(
        12, 0.68, 1e-9, 3.0, 1.0, {"elo_logit": 0.0}, {"elo_logit": 1.0}, "converged"
    )
    with_diagnostics = replace(plain, diagnostics=diagnostics)
    assert model_identity(plain, None) == model_identity(with_diagnostics, None)


def test_report_names_the_model_in_both_copies(store: sqlite3.Connection) -> None:
    report = replace(
        build_report(store, _model(store), DAY, now=NOW), model=model_identity(_fit(), "c0ffee")
    )
    payload = report_json(report)["model"]
    assert payload == {
        "features": ["xg_form", "rest"],
        "fit_sha256": model_identity(_fit(), None).fit_sha256,
        "code_sha": "c0ffee",
    }
    assert "Model: xg_form, rest; fit " in report_markdown(report)


def test_report_without_a_model_says_null(store: sqlite3.Connection) -> None:
    report = build_report(store, _model(store), DAY, now=NOW)
    assert report_json(report)["model"] is None
    assert "Model:" not in report_markdown(report)


def test_json_lists_the_degraded_inputs_of_a_prediction() -> None:
    degraded = (DegradedInput("xg_form", MissingKind.XG_FORM, ("TOR",)),)
    predicted = replace(_prediction(0), degraded_inputs=degraded)
    lines = (DailyLine(_row(1), predicted, ""), DailyLine(_row(2), None, "over"))
    games = report_json(DailyReport(DAY, NOW, 0.8, lines))["games"]
    assert games[0]["degraded_inputs"] == [
        {"feature": "xg_form", "kind": "xg_form", "teams": ["TOR"]}
    ]
    assert games[1]["degraded_inputs"] == []
