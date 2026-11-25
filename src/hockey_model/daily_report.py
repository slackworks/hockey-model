"""Daily prediction report: every game of one Eastern game day, as Markdown and JSON.

A game that started before the run is still predicted. Its inputs are taken as of the game
start, and the report marks it as a late run. A game that is over is predicted the same
way. A postponed game gets no prediction.
"""

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from hockey_model.game import (
    GamePrediction,
    MissingInput,
    TrainingInfo,
    TrainingMembership,
    game_win_probability,
)
from hockey_model.game_model.logistic import LogisticFit
from hockey_model.game_model.training import game_input
from hockey_model.model_version import model_version
from hockey_model.models import GameModel
from hockey_model.report_interval import away_interval, interval_json, interval_notes, win_cell
from hockey_model.report_schema import SCHEMA_VERSION, check_report
from hockey_model.sources.nhl import GameRow
from hockey_model.store.reads import games_on_day
from hockey_model.store.staleness import OLD_GAP_MEASURED_SHIFT_PP, training_gaps
from hockey_model.store.timestamps import iso_utc
from hockey_model.utc import require_utc


@dataclass(frozen=True, slots=True)
class DailyLine:
    """One game in the daily report. ``prediction`` is None when no prediction was made."""

    row: GameRow
    prediction: GamePrediction | None
    skipped: str


@dataclass(frozen=True, slots=True)
class ModelIdentity:
    """Which model made a report: the fit features, a hash of the fit weights, and the code."""

    features: tuple[str, ...]
    fit_sha256: str
    code_sha: str | None
    # Set by ``model_identity``. Left ``None`` only by a caller that builds an identity by hand.
    version: str | None = None


@dataclass(frozen=True, slots=True)
class DailyReport:
    """The games of one game day, with their predictions and the time the run made them."""

    day: date
    generated_at: datetime
    level: float
    lines: tuple[DailyLine, ...]
    model: ModelIdentity | None = None
    # Source gaps in older training days. They change no prediction, so they sit in the footer.
    footer_gaps: tuple[str, ...] = ()


def _json_sha256(values: dict[str, Any]) -> str:
    """Return the SHA-256 of ``values`` as JSON with sorted keys, so key order changes nothing."""
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode("utf-8")).hexdigest()


def model_identity(fit: LogisticFit, code_sha: str | None) -> ModelIdentity:
    """Return the identity of ``fit``. The hash covers every value that changes a prediction.

    The fit diagnostics describe the fit and change no prediction, so the hash leaves them out.
    """
    values = {
        "features": list(fit.features),
        "intercept": fit.intercept,
        "elo_weight": fit.elo_weight,
        "weights": dict(fit.weights),
    }
    return ModelIdentity(
        tuple(fit.features), _json_sha256(values), code_sha or None, model_version(fit.features)
    )


def report_tag(day: date) -> str:
    """Return the release tag for the report of ``day``, such as ``reports-2026-11-04``."""
    return f"reports-{day.isoformat()}"


def _predict_line(row: GameRow, model: GameModel, now: datetime, level: float) -> DailyLine:
    # A postponed game has no puck drop yet. A game that is over or live is predicted from the
    # inputs known at its puck drop, so a late first report is full.
    if row.state == "PPD":
        return DailyLine(row, None, f"game is postponed (state {row.state}); no prediction")
    # A late run uses the inputs known at puck drop, so the note in the report is true.
    as_of = min(now, row.start)
    prediction = game_win_probability(game_input(row), model, as_of=as_of, level=level)
    return DailyLine(row, prediction, "")


def build_report(
    conn: sqlite3.Connection,
    model: GameModel,
    day: date,
    *,
    now: datetime,
    level: float = 0.8,
) -> DailyReport:
    """Predict every game that starts on ``day``. Raise on the first game that fails."""
    require_utc(now, "daily report now")
    lines = tuple(_predict_line(row, model, now, level) for row in games_on_day(conn, day))
    gaps = tuple(training_gaps(conn, now))
    return DailyReport(day, now.astimezone(UTC), level, lines, footer_gaps=gaps)


def _credits(report: DailyReport) -> list[str]:
    return sorted({c for line in report.lines if line.prediction for c in line.prediction.credits})


def _missing_json(missing: tuple[MissingInput, ...]) -> list[dict[str, Any]]:
    return [{"kind": m.kind.value, "team": m.team, "note": m.note} for m in missing]


def _degraded_json(prediction: GamePrediction) -> list[dict[str, Any]]:
    return [
        {"feature": d.feature, "kind": d.kind.value, "teams": list(d.teams)}
        for d in prediction.degraded_inputs
    ]


def _training_json(training: TrainingInfo | None) -> dict[str, Any] | None:
    if training is None:
        return None
    return {
        "fit_id": training.fit_id,
        "fit_through_season": training.fit_through_season,
        "membership": training.membership.value,
    }


def _line_json(line: DailyLine) -> dict[str, Any]:
    row = line.row
    base: dict[str, Any] = {
        "game_id": row.game_id,
        "away": row.away,
        "home": row.home,
        "start_utc": iso_utc(row.start),
        "state": row.state,
        "skipped": line.skipped or None,
    }
    prediction = line.prediction
    if prediction is None:
        return {
            **base,
            "home_win": None,
            "away_win": None,
            "made_after_start": False,
            "missing": [],
            "degraded_inputs": [],
            "training": None,
            "win_interval": None,
        }
    return {
        **base,
        "home_win": prediction.home_win,
        "away_win": prediction.away_win,
        "made_after_start": prediction.made_after_start,
        "missing": _missing_json(prediction.missing),
        "degraded_inputs": _degraded_json(prediction),
        "training": _training_json(prediction.training),
        "win_interval": interval_json(prediction),
    }


# A stale-source note carries the age of the newest read, which grows with the clock and is not
# an input. The hash replaces the age with a fixed word.
_AGE = re.compile(r"[0-9]+ hours old")


def _fingerprint_line(line: DailyLine) -> dict[str, Any]:
    payload = _line_json(line)
    payload["missing"] = [
        {**m, "note": _AGE.sub("aged", m["note"])} if m["kind"] == "stale_source" else m
        for m in payload["missing"]
    ]
    return payload


def inputs_sha256(report: DailyReport) -> str:
    """Return the fingerprint of what a reader sees in the report, without the time of the run.

    Two builds with the same fingerprint publish the same numbers, so the second need not
    publish. The hash covers: the ``schema_version``; the date and the interval level; the
    model identity (features,
    ``fit_sha256``, ``code_sha``); ``training_gaps``; the source ``credits``; and, for each game,
    the game ID, the start, the state, the skip note, both win chances, the win ranges,
    ``made_after_start``,
    ``missing`` (the goalie and injury state that the prediction used), ``degraded_inputs``, and
    ``training``. It leaves out ``generated_at``, and the age in a stale-source note. A source
    read that changes no covered value, such as a fresh read of the same starter, does not
    change the hash.
    """
    values = {
        "schema_version": SCHEMA_VERSION,
        "date": report.day.isoformat(),
        "level": report.level,
        "credits": _credits(report),
        "games": [_fingerprint_line(line) for line in report.lines],
        "model": _model_json(report.model),
        "training_gaps": list(report.footer_gaps),
    }
    return _json_sha256(values)


def report_json(report: DailyReport) -> dict[str, Any]:
    """Return the JSON copy of the report. Every game has the same keys."""
    return {
        "schema_version": SCHEMA_VERSION,
        "date": report.day.isoformat(),
        "generated_at": iso_utc(report.generated_at),
        "inputs_sha256": inputs_sha256(report),
        "level": report.level,
        "games": [_line_json(line) for line in report.lines],
        "credits": _credits(report),
        "model": _model_json(report.model),
        "training_gaps": list(report.footer_gaps),
    }


def _model_json(model: ModelIdentity | None) -> dict[str, Any] | None:
    if model is None:
        return None
    body: dict[str, Any] = {
        "features": list(model.features),
        "fit_sha256": model.fit_sha256,
        "code_sha": model.code_sha,
    }
    if model.version is not None:
        body["version"] = model.version
    return body


def _cell(text: str) -> str:
    """Make one value safe inside a Markdown table cell. The value comes from a remote feed."""
    return " ".join(text.replace("|", "\\|").split())


def _training_note(training: TrainingInfo | None) -> str:
    """Return the in-sample marker, or an empty string for a game outside the fit seasons."""
    if training is None or training.membership is not TrainingMembership.IN_SAMPLE:
        return ""
    return (
        f"in_sample: fit {training.fit_id} trained through season "
        f"{training.fit_through_season}; this game is inside its training data"
    )


def _notes(line: DailyLine) -> str:
    if line.skipped:
        return _cell(line.skipped)
    prediction = line.prediction
    if prediction is None:
        return ""
    notes = [f"{m.kind.value}: {m.note}" for m in prediction.missing]
    marker = _training_note(prediction.training)
    if marker:
        notes.insert(0, marker)
    return _cell("; ".join(notes) or "none")


def report_markdown(report: DailyReport) -> str:
    """Return the Markdown copy of the report, one table row per game."""
    out = [
        f"# NHL predictions for {report.day.isoformat()}",
        "",
        f"Generated {iso_utc(report.generated_at)}. Interval level {report.level:.0%}.",
        "",
    ]
    if not report.lines:
        out.append("No games start on this day.")
    else:
        out.append("| Game | Start (UTC) | State | Home win | Away win | Notes |")
        out.append("| --- | --- | --- | --- | --- | --- |")
        for line in report.lines:
            row = line.row
            p = line.prediction
            out.append(
                f"| {_cell(f'{row.away} at {row.home}')} ({row.game_id}) | {iso_utc(row.start)}"
                f" | {_cell(row.state)}"
                f" | {win_cell(p.home_win if p else None, p.home_win_interval if p else None)}"
                f" | {win_cell(p.away_win if p else None, away_interval(p))} | {_notes(line)} |"
            )
    out.append("")
    predictions = [line.prediction for line in report.lines if line.prediction is not None]
    for note in interval_notes(predictions, report.level):
        out.extend([note, ""])
    if report.model is not None:
        features = ", ".join(report.model.features) or "Elo only"
        code = report.model.code_sha or "unknown"
        out.append(f"Model: {features}; fit {report.model.fit_sha256[:12]}; code {code[:12]}.")
        out.append("")
    credits = _credits(report)
    if credits:
        out.append("Sources: " + " ".join(credits))
        out.append("")
    if report.footer_gaps:
        out.append(
            "Gaps outside the scored, current, and previous seasons (no game row; in the measured "
            "case, a gap of up to a week two seasons back moved a win chance by at most "
            f"{OLD_GAP_MEASURED_SHIFT_PP} percentage points):"
        )
        out.extend(f"- {gap}" for gap in report.footer_gaps)
        out.append("")
    return "\n".join(out)


def write_report(report: DailyReport, out_dir: Path) -> tuple[Path, Path]:
    """Write the Markdown and JSON copies. Return their paths, Markdown first.

    Both strings are built before either file is written, and the JSON must pass the report
    schema, so a build failure or an invalid report writes no file. Both copies go to temporary
    names first, so a failed disk write leaves the files of an earlier build as they were.
    """
    markdown = report_markdown(report)
    data = report_json(report)
    check_report(data)
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    stem = f"report-{report.day.isoformat()}"
    md_path = out_dir / f"{stem}.md"
    json_path = out_dir / f"{stem}.json"
    staged = [(md_path, markdown), (json_path, payload)]
    partials = [path.with_name(f"{path.name}.tmp") for path, _ in staged]
    try:
        for partial, (_, text) in zip(partials, staged, strict=True):
            partial.write_text(text, encoding="utf-8")
    except OSError:
        for partial in partials:
            partial.unlink(missing_ok=True)
        raise
    # The JSON is the copy that a program reads, so it moves into place last.
    for partial, (path, _) in zip(partials, staged, strict=True):
        partial.replace(path)
    return md_path, json_path
