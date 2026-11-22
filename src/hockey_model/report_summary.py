"""The short summary that opens the release notes of a daily report (#212).

One line names each game, the favorite, its win chance in whole percent, and the range of that
win chance. The range is model uncertainty only, and the header line says so.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hockey_model.report_schema import check_report, read_json_object

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


def _plain(text: object) -> str:
    """Return ``text`` on one line. The report holds text that came from a remote feed."""
    return " ".join(str(text).split())


def _line(game: Mapping[str, Any], features: list[str] | None) -> str:
    label = _plain(f"{game['away']} at {game['home']}")
    if game["home_win"] is None:
        return f"{label}: no prediction ({_plain(game['skipped'] or 'no reason given')})"
    home_favored = game["home_win"] >= game["away_win"]
    side, team = ("home", game["home"]) if home_favored else ("away", game["away"])
    chance = game["home_win"] if home_favored else game["away_win"]
    return f"{label}: {_plain(team)} {_percent(chance)}% {_range_text(game, side, features)}"


def release_summary(report: Mapping[str, Any]) -> list[str]:
    """Return the summary lines of a report, with a header first, in the order of the games.

    The report MUST have passed the report schema, which ``summarize_file`` checks first.
    """
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


def summarize_file(path: Path) -> list[str]:
    """Return the summary lines of the report file at ``path``, after the schema check."""
    report = read_json_object(path, "report-summary", "report")
    check_report(report)
    return release_summary(report)
