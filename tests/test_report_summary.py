"""Tests for the release-notes summary of the daily report (#212)."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from report_script_support import report_game, valid_report

from hockey_model.cli import main
from hockey_model.report_summary import release_summary
from hockey_model.types import HockeyModelError


def _report(*games: dict[str, Any], features: list[str] | None = None) -> dict[str, Any]:
    report = valid_report(games=list(games))
    if features is not None:
        report["model"] = {"features": features, "fit_sha256": "b" * 64, "code_sha": None}
    return report


def test_the_summary_names_the_favorite_with_a_whole_percent_and_its_range() -> None:
    lines = release_summary(_report(report_game()))
    assert lines[1:] == ["TOR at MTL: TOR 58% (52\u201364%)"]


def test_a_home_favorite_gets_the_home_range() -> None:
    lines = release_summary(_report(report_game(home_win=0.61, interval=(0.55, 0.67))))
    assert lines[1] == "TOR at MTL: MTL 61% (55\u201367%)"


def test_an_even_game_names_the_home_team() -> None:
    lines = release_summary(_report(report_game(home_win=0.5, interval=(0.45, 0.55))))
    assert lines[1] == "TOR at MTL: MTL 50% (45\u201355%)"


def test_the_header_says_model_uncertainty_only_and_names_the_level() -> None:
    header = release_summary(_report(report_game()))[0]
    assert "model uncertainty only" in header
    assert "80%" in header


def test_the_summary_keeps_the_report_order() -> None:
    games = [
        report_game(home="BOS", away="NYR"),
        report_game(home="MTL"),
        report_game(home="SEA", away="VAN"),
    ]
    lines = release_summary(_report(*games))
    assert [line.split(":")[0] for line in lines[1:]] == [
        "NYR at BOS",
        "TOR at MTL",
        "VAN at SEA",
    ]


def test_an_elo_only_fit_says_no_interval_and_prints_no_range() -> None:
    lines = release_summary(_report(report_game(interval=None), features=[]))
    assert lines[1] == "TOR at MTL: TOR 58% (no interval: Elo only)"


def test_a_fit_with_features_but_no_range_says_to_fit_again() -> None:
    lines = release_summary(_report(report_game(interval=None), features=["xg_form"]))
    assert lines[1] == "TOR at MTL: TOR 58% (no interval: run `hockey-model fit` again)"


def test_a_game_without_a_prediction_is_named_with_its_reason() -> None:
    skipped = report_game(home_win=None, interval=None, skipped="game is postponed (state PPD)")
    lines = release_summary(_report(skipped))
    assert lines[1] == "TOR at MTL: no prediction (game is postponed (state PPD))"


def test_a_day_without_games_says_so() -> None:
    assert release_summary(_report()) == ["No games start on this day."]


@given(
    home_win=st.floats(min_value=0.01, max_value=0.99),
    below=st.floats(min_value=0.0, max_value=0.5),
    above=st.floats(min_value=0.0, max_value=0.5),
    count=st.integers(min_value=1, max_value=6),
)
def test_every_game_gets_one_line_with_the_point_inside_its_range(
    home_win: float, below: float, above: float, count: int
) -> None:
    interval = (home_win * (1 - below), min(1.0, home_win + (1 - home_win) * above))
    games = [report_game(home_win=home_win, interval=interval) for _ in range(count)]
    lines = release_summary(_report(*games))
    assert len(lines) == count + 1
    for line in lines[1:]:
        match = re.fullmatch(r"TOR at MTL: (TOR|MTL) (\d+)% \((\d+)\u2013(\d+)%\)", line)
        assert match is not None, line
        low, point, high = int(match[3]), int(match[2]), int(match[4])
        assert low <= point <= high <= 100


def test_the_command_prints_the_lines_as_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps(_report(report_game())), encoding="utf-8")
    assert main(["report-summary", "--report", str(path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"][1] == "TOR at MTL: TOR 58% (52\u201364%)"


def test_the_command_refuses_a_report_that_fails_the_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"games": []}), encoding="utf-8")
    assert main(["report-summary", "--report", str(path)]) != 0
    assert "daily_report.schema.json" in capsys.readouterr().err


def test_a_malformed_report_raises_with_the_fix() -> None:
    with pytest.raises(HockeyModelError, match="report-summary"):
        release_summary({"games": [{"home": "MTL"}]})


def test_a_game_without_the_optional_win_interval_key_says_there_is_no_interval() -> None:
    """The schema does not require the key, so a report from before the range has none."""
    game = report_game()
    del game["win_interval"]
    lines = release_summary(_report(game, features=["xg_form"]))
    assert lines[1] == "TOR at MTL: TOR 58% (no interval: run `hockey-model fit` again)"
