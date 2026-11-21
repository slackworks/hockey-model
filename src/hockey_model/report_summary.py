"""The short summary that opens the release notes of a daily report (#212).

One line names each game, the favorite, its win chance in whole percent, and the range of that
win chance. The range is model uncertainty only, and the header line says so.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hockey_model.report_schema import check_report
from hockey_model.types import HockeyModelError

NO_GAMES = "No games start on this day."


def _percent(value: float) -> str:
    return f"{100.0 * value:.0f}"


def _range_text(game: Mapping[str, Any], side: str, features: list[str] | None) -> str:
    interval = game.get("win_interval")
    if interval is not None:
        return f"({_percent(interval[f'{side}_low'])}–{_percent(interval[f'{side}_high'])}%)"
    if features == []:
        return "(no interval: Elo only)"
    return "(no interval: run `hockey-model fit` again)"


def _line(game: Mapping[str, Any], features: list[str] | None) -> str:
    label = f"{game['away']} at {game['home']}"
    if game["home_win"] is None:
        return f"{label}: no prediction ({game['skipped'] or 'no reason given'})"
    home_favored = game["home_win"] >= game["away_win"]
    side, team = ("home", game["home"]) if home_favored else ("away", game["away"])
    chance = game["home_win"] if home_favored else game["away_win"]
    return f"{label}: {team} {_percent(chance)}% {_range_text(game, side, features)}"


def release_summary(report: Mapping[str, Any]) -> list[str]:
    """Return the summary lines of a report, with a header first, in the order of the games."""
    try:
        games = report["games"]
        if not games:
            return [NO_GAMES]
        model = report.get("model")
        features = None if model is None else list(model["features"])
        header = (
            "Win chance of the favorite, with its range "
            f"(model uncertainty only, {report['level']:.0%} level):"
        )
        return [header, *(_line(game, features) for game in games)]
    except (KeyError, TypeError) as exc:
        raise HockeyModelError(
            f"report-summary: the report is missing a field ({type(exc).__name__}: {exc}); "
            "build the report with the current code"
        ) from exc


def summarize_file(path: Path) -> list[str]:
    """Return the summary lines of the report file at ``path``, after the schema check."""
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HockeyModelError(
            f"report-summary: cannot read the report {path}: {error}; "
            "pass the path of a report JSON file"
        ) from error
    if not isinstance(report, dict):
        raise HockeyModelError(
            f"report-summary: the report {path} is not a JSON object; "
            "pass the path of a report JSON file"
        )
    check_report(report)
    return release_summary(report)
