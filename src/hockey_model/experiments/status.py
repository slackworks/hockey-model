"""The status that a confirmation result earns, and the notes that explain it.

The run writes the status from the interval, and the check recomputes it from the stored
result, so a hand edit of the status fails. Rule: docs/design/promotion-bar.md
"""

from __future__ import annotations

from hockey_model.backtest.runner import MODEL_ACTUAL, BacktestReport, ScoredRun
from hockey_model.experiments.looks import CONFIRMATION
from hockey_model.experiments.models import (
    STATUS_INCONCLUSIVE,
    STATUS_PROMOTED,
    STATUS_REJECTED,
    ExperimentRecord,
)
from hockey_model.types import HockeyModelError

Interval = tuple[float, float]


def status_for_interval(record: ExperimentRecord, interval: Interval) -> str:
    """Return the status that ``interval`` earns for ``record``.

    Promotion needs the upper end of the interval below zero. The interval tail shrinks with
    the look number (docs/design/promotion-bar.md). A run whose interval is wider than the
    effect the record expects is underpowered, so it is inconclusive rather than rejected.
    """
    low, high = interval
    if record.expected_effect is not None and (high - low) / 2 > abs(record.expected_effect):
        return STATUS_INCONCLUSIVE
    return STATUS_PROMOTED if high < 0.0 else STATUS_REJECTED


def confirmation_status(record: ExperimentRecord, report: BacktestReport) -> str:
    """Return the status a confirmation run earns, from the interval in its report."""
    model = model_run(report)
    if model is None or model.diff_vs_elo_ci is None:
        return STATUS_INCONCLUSIVE
    return status_for_interval(record, model.diff_vs_elo_ci)


def status_problem(record: ExperimentRecord) -> str | None:
    """Return why the stored status is not the one its result earns, or None.

    An exploration record cannot promote, so its status is not derived from a result.
    """
    if record.cohort != CONFIRMATION or record.result is None:
        return None
    earned = status_for_interval(record, record.result.interval)
    if earned == record.status:
        return None
    low, high = record.result.interval
    return (
        f"the stored status {record.status!r} does not support its result: the interval "
        f"{low!r} to {high!r} earns {earned!r}; set the status to {earned!r}"
    )


def model_run(report: BacktestReport) -> ScoredRun | None:
    """Return the scored run of the model with the goalie who started, or None."""
    return next((run for run in report.runs if run.name == MODEL_ACTUAL), None)


def interval_of(model: ScoredRun) -> Interval:
    """Return the interval of a scored run that has one."""
    if model.diff_vs_elo_ci is None:
        raise HockeyModelError(f"scored run {model.name!r} has no interval")
    return model.diff_vs_elo_ci


def require_interval(record: ExperimentRecord, report: BacktestReport) -> ScoredRun:
    """Return the model run, and refuse a run that produced no interval."""
    model = model_run(report)
    if model is None or model.diff_vs_elo_ci is None:
        why = "; ".join(report.ineligible_reasons) or "the model run is missing"
        raise HockeyModelError(
            f"experiment {record.id!r}: the run produced no interval ({why}), so there is no "
            "result to record; the record is unchanged. Refresh the store and run again"
        )
    return model


def inconclusive_notes(record: ExperimentRecord) -> tuple[str, ...]:
    """Say why a run is inconclusive when the interval is wider than the expected effect."""
    if record.status != STATUS_INCONCLUSIVE or record.result is None:
        return ()
    if record.expected_effect is None:
        return ()
    low, high = record.result.interval
    return (
        f"The interval half-width {(high - low) / 2:.4f} is above the expected effect "
        f"{abs(record.expected_effect):.4f}, so the run is inconclusive and the look is spent.",
    )
