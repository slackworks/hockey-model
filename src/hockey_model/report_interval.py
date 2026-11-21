"""The win-chance ranges of the daily report: the JSON object, the table cell, the footer notes.

A range is model uncertainty only. It leaves out Elo noise, feature noise, and drift between
seasons, and the report says so beside it (#212).
"""

from collections.abc import Sequence

from hockey_model.game import GamePrediction

RANGE_NOTE = (
    "Ranges in brackets are model uncertainty only ({level:.0%} level): they come from the "
    "fitted weights and leave out Elo noise, feature noise, and drift between seasons."
)
NO_RANGE_NOTE = (
    "No range: the fit has no coefficient uncertainty (Elo only, or saved before ranges existed)."
)


def interval_json(prediction: GamePrediction) -> dict[str, float] | None:
    """Return both sides of the win range, or ``None`` when the fit has no range."""
    interval = prediction.home_win_interval
    if interval is None:
        return None
    low, high = interval
    return {
        "home_low": low,
        "home_high": high,
        "away_low": 1.0 - high,
        "away_high": 1.0 - low,
    }


def win_cell(value: float | None, interval: tuple[float, float] | None) -> str:
    """Return a win chance as a percent, with its range in brackets when there is one."""
    if value is None:
        return "-"
    text = f"{value:.1%}"
    if interval is None:
        return text
    return f"{text} ({interval[0]:.1%}\u2013{interval[1]:.1%})"


def away_interval(prediction: GamePrediction | None) -> tuple[float, float] | None:
    """Return the away side of the win range of ``prediction``."""
    if prediction is None or prediction.home_win_interval is None:
        return None
    low, high = prediction.home_win_interval
    return 1.0 - high, 1.0 - low


def interval_notes(predictions: Sequence[GamePrediction], level: float) -> list[str]:
    """Return the footer lines that say what the ranges in the table mean."""
    notes = []
    if any(p.home_win_interval is not None for p in predictions):
        notes.append(RANGE_NOTE.format(level=level))
    if any(p.home_win_interval is None for p in predictions):
        notes.append(NO_RANGE_NOTE)
    return notes
