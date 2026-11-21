"""Tests for the win-chance interval in the daily report (#212)."""

import math
import sqlite3
from dataclasses import replace
from datetime import date
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from seeding import add_game, add_team_form, artifact, utc

from hockey_model.daily_report import (
    DailyLine,
    DailyReport,
    build_report,
    inputs_sha256,
    report_json,
    report_markdown,
)
from hockey_model.game import Game, GamePrediction, game_win_probability
from hockey_model.game_model.features import GoalieMode
from hockey_model.game_model.logistic import ELO_ONLY, FitDiagnostics, LogisticFit, sigmoid
from hockey_model.game_model.nhl_model import NhlGameModel
from hockey_model.matchup import check_win_interval
from hockey_model.report_schema import check_report
from hockey_model.types import ProbabilityError, Projection

DAY = date(2026, 1, 20)
NOW = utc("2026-01-20T12:00")
GOALS = Projection(3.0, 2.0, 4.0, 0.8)
Z80 = 1.2815515655446004


def _covariant_fit() -> LogisticFit:
    """An Elo-only fit with a diagonal covariance, so an Elo logit of 0 has se 0.2."""
    diagnostics = FitDiagnostics(
        iterations=1,
        objective=0.0,
        gradient_norm=0.0,
        condition=1.0,
        l2=1.0,
        means={"elo_logit": 0.0},
        scales={"elo_logit": 1.0},
        stop_reason="converged",
        covariance=((0.04, 0.0), (0.0, 0.09)),
    )
    return LogisticFit((), 0.0, 1.0, {}, diagnostics)


def _expected(point: float) -> tuple[float, float]:
    """The range of the covariant fit, from its diagonal covariance and the score of the game."""
    score = math.log(point / (1.0 - point))
    spread = Z80 * math.sqrt(0.04 + 0.09 * score**2)
    return sigmoid(score - spread), sigmoid(score + spread)


def _seed(store: sqlite3.Connection) -> None:
    add_team_form(store, 1, "TOR", 3.5, 2.0)
    add_team_form(store, 2, "BOS", 2.0, 3.0)
    add_game(store, 10, "2026-01-20T23:00", "TOR", "BOS", state="FUT")


def _report(store: sqlite3.Connection, fit: LogisticFit) -> DailyReport:
    model = NhlGameModel(store, artifact(fit), GoalieMode.GUESS)
    return build_report(store, model, DAY, now=NOW)


def test_the_model_gives_the_interval_of_its_fit(store: sqlite3.Connection) -> None:
    _seed(store)
    (line,) = _report(store, _covariant_fit()).lines
    assert line.prediction is not None
    assert line.prediction.home_win_interval == pytest.approx(
        _expected(line.prediction.home_win), abs=1e-9
    )


def test_an_elo_only_fit_gives_no_interval(store: sqlite3.Connection) -> None:
    _seed(store)
    (line,) = _report(store, ELO_ONLY).lines
    assert line.prediction is not None and line.prediction.home_win_interval is None


def test_the_json_gives_both_sides_of_the_interval_and_null_without_one(
    store: sqlite3.Connection,
) -> None:
    _seed(store)
    game = report_json(_report(store, _covariant_fit()))["games"][0]
    with_interval = game["win_interval"]
    low, high = _expected(game["home_win"])
    assert (with_interval["home_low"], with_interval["home_high"]) == pytest.approx((low, high))
    assert with_interval["away_low"] == pytest.approx(1 - with_interval["home_high"])
    assert with_interval["away_high"] == pytest.approx(1 - with_interval["home_low"])
    assert report_json(_report(store, ELO_ONLY))["games"][0]["win_interval"] is None


def test_every_game_has_the_win_interval_key_and_the_report_passes_the_schema(
    store: sqlite3.Connection,
) -> None:
    _seed(store)
    add_game(store, 11, "2026-01-20T23:00", "TOR", "BOS", state="PPD")
    payload = report_json(_report(store, _covariant_fit()))
    assert all("win_interval" in game for game in payload["games"])
    check_report(payload)


def test_the_markdown_shows_the_range_and_says_it_is_model_uncertainty_only(
    store: sqlite3.Connection,
) -> None:
    _seed(store)
    report = _report(store, _covariant_fit())
    (line,) = report.lines
    assert line.prediction is not None
    low, high = _expected(line.prediction.home_win)
    text = report_markdown(report)
    assert f" | {line.prediction.home_win:.1%} ({low:.1%}\u2013{high:.1%}) | " in text
    assert f" | {line.prediction.away_win:.1%} ({1 - high:.1%}\u2013{1 - low:.1%}) | " in text
    assert "Ranges in brackets are model uncertainty only (80% level)" in text
    assert "No range:" not in text


def test_the_markdown_says_why_a_game_has_no_range(store: sqlite3.Connection) -> None:
    _seed(store)
    text = report_markdown(_report(store, ELO_ONLY))
    assert "No range: the fit has no coefficient uncertainty (Elo only" in text
    assert "Ranges in brackets" not in text


def test_a_different_interval_changes_the_inputs_fingerprint(store: sqlite3.Connection) -> None:
    _seed(store)
    report = _report(store, _covariant_fit())
    (line,) = report.lines
    assert line.prediction is not None
    moved = replace(line.prediction, home_win_interval=(0.1, 0.9))
    other = replace(report, lines=(DailyLine(line.row, moved, ""),))
    assert inputs_sha256(report) != inputs_sha256(other)


class _FixedModel:
    def __init__(self, interval: Any, home_win: float = 0.6) -> None:
        self._interval = interval
        self._home_win = home_win

    def predict(self, game: Game, as_of: object, level: float) -> GamePrediction:
        return GamePrediction(
            self._home_win,
            1.0 - self._home_win,
            GOALS,
            GOALS,
            (),
            (),
            home_win_interval=self._interval,
        )


def _game() -> Game:
    return Game(1, "TOR", "BOS", utc("2026-01-20T23:00"))


@pytest.mark.parametrize(
    "interval",
    [(0.7, 0.8), (0.4, 0.5), (0.8, 0.4), (-0.1, 0.9), (0.1, 1.2), (float("nan"), 0.9), (0.5,)],
)
def test_a_model_interval_that_does_not_hold_the_point_is_refused(interval: object) -> None:
    with pytest.raises(ProbabilityError, match="win interval"):
        game_win_probability(_game(), _FixedModel(interval), as_of=utc("2026-01-20T12:00"))


def test_a_model_interval_around_the_point_passes() -> None:
    result = game_win_probability(_game(), _FixedModel((0.5, 0.7)), as_of=utc("2026-01-20T12:00"))
    assert result.home_win_interval == (0.5, 0.7)


@given(
    point=st.floats(min_value=0.0, max_value=1.0),
    below=st.floats(min_value=0.0, max_value=1.0),
    above=st.floats(min_value=0.0, max_value=1.0),
)
def test_check_win_interval_accepts_exactly_a_pair_that_holds_the_point(
    point: float, below: float, above: float
) -> None:
    low, high = point * (1 - below), point + (1 - point) * above
    check_win_interval(point, (low, high), "op")
    if point > 1e-6:
        with pytest.raises(ProbabilityError):
            check_win_interval(point, (point + 1e-6, 1.0), "op")
