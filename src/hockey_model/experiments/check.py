"""Experiment checker: validate records and enforce the registry rules.

``hockey-model experiments check`` prints JSON validated against
``CHECK_OUTPUT_SCHEMA`` in ``schemas.py``. It fails when a record is invalid, when
a field other than ``status`` and ``result`` changed since the commit that
registered the record, or when a result names a per-game CSV whose hash no longer
matches the file.

``hockey-model experiments run`` scores one record. A confirmation record scores
only its cohort and runs once.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import TypedDict

from hockey_model.backtest.game_records import GameRecord, write_game_records
from hockey_model.backtest.runner import MODEL_ACTUAL, BacktestReport, ScoredRun, run_backtest
from hockey_model.experiments.cohort import (
    floor_notes,
    plan_for,
    require_cohort_size,
    require_scored_cohort,
)
from hockey_model.experiments.history import (
    check_immutable_fields,
    check_one_shot,
    deleted_record_ids,
    registration_commit,
)
from hockey_model.experiments.looks import look_problem
from hockey_model.experiments.models import (
    STATUS_INCONCLUSIVE,
    STATUS_PROMOTED,
    STATUS_REGISTERED,
    STATUS_REJECTED,
    ExperimentRecord,
    validate_against_schema,
)
from hockey_model.experiments.registry import (
    REGISTRY_DIR_NAME,
    RESULTS_DIR_NAME,
    duplicate_numbers,
    get_record,
    list_records,
    registry_dir,
    results_dir,
)
from hockey_model.experiments.schemas import CHECK_OUTPUT_SCHEMA, EXPERIMENT_SCHEMA
from hockey_model.game_model.features import feature_name_problem
from hockey_model.game_model.seasons import exploration_plan, scored_label
from hockey_model.store.timestamps import iso_utc
from hockey_model.types import HockeyModelError


class CheckEntry(TypedDict, total=False):
    """One record's check result, as the checker prints it."""

    id: str
    status: str
    valid: bool
    error: str
    immutability: str
    registration_commit: str
    has_result: bool


class CheckOutput(TypedDict):
    """The whole check run: whether every record passed, and each record's entry."""

    ok: bool
    records: list[CheckEntry]
    errors: list[str]


class RunPayload(TypedDict, total=False):
    """What one experiment run reports."""

    ok: bool
    record_id: str
    status: str
    cohort: str
    scored: str
    notes: list[str]
    games_out: str


class GameExport(TypedDict):
    """One scored game, for the per-game export."""

    game_id: int
    start: str
    home: str
    away: str
    run: str
    home_win_prob: float
    home_won: bool


def check_all(repo_root: Path | None = None) -> CheckOutput:
    """Check every record in the registry.

    Returns ``ok``, the entry for every record, and the error messages.
    """
    root = repo_root or Path.cwd()
    if not registry_dir(root).is_dir():
        raise HockeyModelError(
            f"no experiment registry at {registry_dir(root)}; run the command from the "
            "repository root"
        )
    results: list[CheckEntry] = []
    ids = list_records(root)
    errors = _registry_errors(ids, root)

    for record_id in ids:
        try:
            results.append(_check_one(record_id, root))
        except HockeyModelError as exc:
            errors.append(f"{record_id}: {exc}")
            results.append({"id": record_id, "valid": False, "error": str(exc)})

    output: CheckOutput = {"ok": len(errors) == 0, "records": results, "errors": errors}
    validate_against_schema(output, CHECK_OUTPUT_SCHEMA)
    return output


def run_experiment(
    record_id: str,
    conn: sqlite3.Connection,
    games_out: Path | None = None,
    repo_root: Path | None = None,
) -> tuple[RunPayload, list[GameExport]]:
    """Run one backtest for one experiment record.

    A confirmation record refuses when its status is not registered, when its
    cohort holds fewer than ``min_games`` games, or when it already has a result.
    It scores only cohort games, writes the status and the result back to the
    record, and writes the per-game CSV.

    An exploration record may run again and again, and its notes say the run is
    exploratory.
    """
    root = repo_root or Path.cwd()
    record = get_record(record_id, root)
    validate_against_schema(record, EXPERIMENT_SCHEMA)

    cohort_games = 0
    plan_notes: list[str] = []
    if record.cohort == "confirmation":
        _check_confirmation_can_run(record)
        _check_look(record, root)
        plan = plan_for(record)
        plan_notes = floor_notes(record, plan)
        cohort_games = require_cohort_size(conn, record, plan)
    else:
        plan = exploration_plan()

    report, game_records = run_backtest(conn, plan, candidates=record.candidate_features)
    model = _model_run(report)
    if record.cohort == "confirmation" and model is not None:
        require_scored_cohort(record, model.games, cohort_games)
    # The caller's export goes first. A write failure then leaves the look unspent.
    if games_out is not None:
        write_game_records(games_out, game_records)

    if record.cohort == "confirmation":
        csv_path = results_dir(root) / f"{record.id}-games.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        write_game_records(csv_path, game_records)
        updated = record_with_result(record, report, csv_path, root)
        updated.save(registry_dir(root) / f"{updated.id}.json")
        notes = report.notes + tuple(plan_notes)
    else:
        updated = record
        notes = report.notes + ("This run is exploratory: it cannot promote a feature.",)

    payload: RunPayload = {
        "ok": True,
        "record_id": updated.id,
        "status": updated.status,
        "cohort": updated.cohort,
        "scored": scored_label(plan),
        "notes": list(notes),
    }
    if games_out is not None:
        payload["games_out"] = str(games_out)
    return payload, [_export_entry(record) for record in game_records]


def _registry_errors(ids: list[str], repo_root: Path) -> list[str]:
    """Return the errors that belong to the registry as a whole, not to one record."""
    errors = [
        f"registry number {number} is used by more than one record; a new record needs a new number"
        for number in duplicate_numbers(ids)
    ]
    errors.extend(
        f"{record_id}: tracked in the repository history but missing from the working tree; "
        "a registered record cannot be deleted"
        for record_id in deleted_record_ids(ids, repo_root)
    )
    return errors


def _check_one(record_id: str, repo_root: Path) -> CheckEntry:
    """Validate one record, then check its status, immutability, and result."""
    record = get_record(record_id, repo_root)
    if record.id != record_id:
        raise HockeyModelError(
            f"the file is named {record_id!r} but the record id is {record.id!r}; "
            "rename the file or restore the id"
        )
    validate_against_schema(record, EXPERIMENT_SCHEMA)
    _check_look(record, repo_root)
    _check_features_known(record)
    _check_status_matches_result(record)
    entry: CheckEntry = {"id": record.id, "status": record.status, "valid": True}

    commit = registration_commit(record.id, repo_root)
    if commit is None:
        entry["immutability"] = "not checked: git does not track the record"
    else:
        check_immutable_fields(record, commit, repo_root)
        check_one_shot(record, repo_root)
        entry["registration_commit"] = commit

    if record.result is not None:
        entry["has_result"] = True
        _verify_result_hash(record, repo_root)

    return entry


def _check_features_known(record: ExperimentRecord) -> None:
    """Raise when the candidate features are not a usable feature list."""
    problem = feature_name_problem(record.candidate_features)
    if problem is not None:
        raise HockeyModelError(f"experiment {record.id!r}: candidate features: {problem}")


def _check_status_matches_result(record: ExperimentRecord) -> None:
    """Raise when the status and the result disagree.

    A registered record holds no result. A promoted or rejected record holds one.
    An inconclusive record may lack one, because a run without an interval writes none.
    """
    has_result = record.result is not None
    if record.status == STATUS_REGISTERED and has_result:
        raise HockeyModelError(
            f"experiment {record.id!r}: status is 'registered' but the record holds a result"
        )
    if record.status in {STATUS_PROMOTED, STATUS_REJECTED} and not has_result:
        raise HockeyModelError(
            f"experiment {record.id!r}: status is {record.status!r} but the record holds no result"
        )


def _verify_result_hash(record: ExperimentRecord, repo_root: Path) -> None:
    """Check that the per-game CSV named by the result still matches its recorded hash.

    The path is relative to the repository root and must stay inside the results directory.
    """
    result = record.result
    if result is None:
        return
    path = (repo_root / result.csv_path).resolve()
    if not path.is_relative_to(results_dir(repo_root).resolve()):
        raise HockeyModelError(
            f"experiment {record.id!r}: the result path {result.csv_path!r} is not inside "
            f"{REGISTRY_DIR_NAME}/{RESULTS_DIR_NAME}/; use a path relative to the repository root"
        )
    if not path.is_file():
        raise HockeyModelError(
            f"experiment {record.id!r}: the result names {result.csv_path!r}, which is not "
            "in the repository; add the per-game CSV with the result"
        )
    if _file_hash(path) != result.csv_hash:
        raise HockeyModelError(
            f"experiment {record.id!r}: {result.csv_path!r} does not match the hash "
            f"{result.csv_hash!r} recorded with the result"
        )


def _check_confirmation_can_run(record: ExperimentRecord) -> None:
    """Check that a confirmation record may run."""
    if record.status != STATUS_REGISTERED:
        raise HockeyModelError(
            f"experiment {record.id!r}: confirmation status is {record.status!r}, "
            "expected registered"
        )
    if record.result is not None:
        raise HockeyModelError(
            f"experiment {record.id!r}: already has a result; "
            "a change requires a new record, not a re-run"
        )


def _model_run(report: BacktestReport) -> ScoredRun | None:
    """Return the scored run of the model with the goalie who started, or None."""
    return next((run for run in report.runs if run.name == MODEL_ACTUAL), None)


def _check_look(record: ExperimentRecord, repo_root: Path) -> None:
    """Raise when the look number of a confirmation record breaks the registry rule."""
    number = record.id[:4]
    earlier = [
        get_record(other, repo_root) for other in list_records(repo_root) if other[:4] < number
    ]
    problem = look_problem(record, earlier)
    if problem is not None:
        raise HockeyModelError(f"experiment {record.id!r}: {problem}")


def _confirmation_status(record: ExperimentRecord, report: BacktestReport) -> str:
    """Return the status a confirmation run earns.

    Promotion needs the upper end of the interval below zero. The interval tail shrinks with
    the look number (docs/design/promotion-bar.md). A run whose
    interval is wider than the effect the record expects is underpowered, so it is
    inconclusive rather than rejected.
    """
    model = _model_run(report)
    if model is None or model.diff_vs_elo_ci is None:
        return STATUS_INCONCLUSIVE
    low, high = model.diff_vs_elo_ci
    if record.expected_effect is not None and (high - low) / 2 > abs(record.expected_effect):
        return STATUS_INCONCLUSIVE
    return STATUS_PROMOTED if high < 0.0 else STATUS_REJECTED


def record_with_result(
    record: ExperimentRecord, report: BacktestReport, csv_path: Path, repo_root: Path
) -> ExperimentRecord:
    """Return a copy of the record with the run's status and result written in.

    A run with no interval has no result to record. The run then fails before it
    saves, so the look is not spent on a run that scored nothing.
    """
    model = _model_run(report)
    if model is None or model.diff_vs_elo_ci is None:
        why = "; ".join(report.ineligible_reasons) or "the model run is missing"
        raise HockeyModelError(
            f"experiment {record.id!r}: the run produced no interval ({why}), so there is no "
            "result to record; the record is unchanged. Refresh the store and run again"
        )
    data = record.to_dict()
    data["status"] = _confirmation_status(record, report)
    data["result"] = {
        "games": model.games,
        "difference": model.mean_diff_vs_elo,
        "interval": list(model.diff_vs_elo_ci),
        "csv_path": csv_path.relative_to(repo_root).as_posix(),
        "csv_hash": _file_hash(csv_path),
    }
    return ExperimentRecord.from_dict(data)


def _export_entry(game: GameRecord) -> GameExport:
    """Turn one backtest game record into an export row."""
    return {
        "game_id": game.game_id,
        "start": iso_utc(game.start),
        "home": game.home,
        "away": game.away,
        "run": game.run,
        "home_win_prob": game.home_win_prob,
        "home_won": game.home_won,
    }


def _file_hash(path: Path) -> str:
    """Compute the SHA-256 hash of a file's contents."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise HockeyModelError(f"cannot read {path} to hash it: {exc}") from exc


def main() -> None:
    """CLI entry point for ``hockey-model experiments check``."""
    result = check_all()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
