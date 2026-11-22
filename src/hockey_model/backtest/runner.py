"""The walk-forward backtest of the NHL game model against game results.

The backtest uses no price data. It scores the model against the results of the games, a
home win rate, and the results-only Elo.
"""

import math
import sqlite3
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

from hockey_model.backtest.eligibility import SeasonCount, ineligibility_reasons, season_counts
from hockey_model.backtest.game_records import GameRecord
from hockey_model.backtest.metrics import (
    CalibrationBin,
    block_bootstrap_mean_ci,
    bootstrap_problem,
    brier,
    calibration,
    log_loss,
    per_game_log_loss,
)
from hockey_model.backtest.report_notes import (
    MONEYPUCK_FEATURES,
    candidate_notes,
    eligibility_notes,
    is_production,
    joint_test_notes,
)
from hockey_model.backtest.shards import build_sharded, can_shard
from hockey_model.backtest.units import Unit, check_workers, run_units
from hockey_model.game_model.features import GoalieMode
from hockey_model.game_model.imputation import (
    ImputationLimitError,
    check_imputation,
    feature_quality,
)
from hockey_model.game_model.logistic import LogisticFit, fit_logistic, predict_logistic
from hockey_model.game_model.seasons import DEFAULT_PLAN, SeasonPlan, in_cohort, scored_label
from hockey_model.game_model.training import TrainingRow, training_rows
from hockey_model.sources.moneypuck import MONEYPUCK_CREDIT
from hockey_model.types import HockeyModelError

ELO = "elo"
HOME_RATE = "home_rate"
MODEL_ACTUAL = "model_actual"
MODEL_GUESS = "model_guess"
RECORDED_RUNS = (ELO, MODEL_ACTUAL, MODEL_GUESS)
# A feature is vetoed only when it is worse by more than rounding error.
VETO_TOLERANCE = 1e-9
# The wall time to meet with PARALLEL_TARGET_WORKERS workers on the real store: the season
# shards, 20 units of up to 350 s each (1262 s measured), plus a margin. Table:
# docs/design/backtest-speed.md
PARALLEL_TARGET_WORKERS = 4
PARALLEL_TARGET_S = 1500


@dataclass(frozen=True, slots=True)
class ScoredRun:
    """The scores of one way of predicting the scored games."""

    name: str
    games: int
    log_loss: float
    brier: float
    calibration: tuple[CalibrationBin, ...]
    diff_vs_elo_ci: tuple[float, float] | None
    # The mean per-game log-loss difference against the Elo. None for the Elo run.
    mean_diff_vs_elo: float | None = None


@dataclass(frozen=True, slots=True)
class BacktestReport:
    """The result of one backtest."""

    candidate_features: tuple[str, ...]
    vetoed: tuple[str, ...]
    tested_features: tuple[str, ...]
    live_features: tuple[str, ...]
    validation: Mapping[str, Mapping[str, float]]
    runs: tuple[ScoredRun, ...]
    success: bool
    notes: tuple[str, ...]
    credits: tuple[str, ...]
    # Scored games, with the goalie who started, that had each kind of missing input.
    missing_inputs: Mapping[str, int]
    # Scored games by season and game type, with the expected count of a regular season.
    coverage: tuple[SeasonCount, ...] = ()
    # False when the scored games cannot support success: success and live_features then
    # stay empty, and the scores stay descriptive. The reasons say what is missing.
    eligible: bool = True
    ineligible_reasons: tuple[str, ...] = ()
    # How often each tested feature was imputed in the scored games, as FeatureQuality dicts.
    # This is descriptive. The imputation limit gates the training rows of each fit, and it does
    # not change eligible or success.
    imputation: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class _Scored:
    """The probabilities that each run gave for one scored game."""

    row: TrainingRow
    by_run: Mapping[str, float]


def _in(rows: Sequence[TrainingRow], low: int, high: int) -> list[TrainingRow]:
    return [r for r in rows if low <= r.game.season <= high]


def _wins(rows: Sequence[TrainingRow]) -> list[bool]:
    return [r.home_won for r in rows]


def _fit(rows: Sequence[TrainingRow], features: Sequence[str]) -> LogisticFit:
    # The check runs first, so a fit above the imputation limit is refused before it is fitted.
    check_imputation([r.features for r in rows], features)
    return fit_logistic([r.features for r in rows], _wins(rows), features)


def _loss(fit: LogisticFit, rows: Sequence[TrainingRow]) -> float:
    return log_loss([predict_logistic(fit, r.features) for r in rows], _wins(rows))


def _validation_unit(
    train: Sequence[TrainingRow],
    validate_actual: Sequence[TrainingRow],
    validate_guess: Sequence[TrainingRow],
    features: Sequence[str],
) -> dict[str, float] | None:
    """Fit on the training seasons and score the validation seasons. None means refused."""
    try:
        fit = _fit(train, features)
    except ImputationLimitError:
        # The Elo alone is never refused: it has no feature to impute.
        if not features:
            raise
        return None
    return {"actual": _loss(fit, validate_actual), "guess": _loss(fit, validate_guess)}


def _validate(
    actual: Sequence[TrainingRow],
    guess: Sequence[TrainingRow],
    plan: SeasonPlan,
    candidates: Sequence[str],
) -> tuple[dict[str, dict[str, float]], tuple[str, ...]]:
    """Score the Elo alone and the Elo plus each feature on the validation seasons.

    Each fit uses the training seasons with the goalie who started. It is scored with that
    goalie ("actual") and with the guessed goalie ("guess"). A feature is vetoed when its
    guess score is worse than the Elo alone. Each fit is a named unit. Each takes a fraction of a
    second on the real store, so they run in this process (docs/design/backtest-speed.md).
    """
    train = _in(actual, *plan.select_train)
    validate_actual = _in(actual, *plan.select_validate)
    validate_guess = _in(guess, *plan.select_validate)
    if not train or not validate_actual:
        raise HockeyModelError(
            f"backtest: no games in the selection seasons {plan.select_train} and "
            f"{plan.select_validate}; run `hockey-model refresh --backfill-from 2014-10-01`"
        )
    jobs = [(ELO, ()), *[(f"{ELO}+{f}", (f,)) for f in candidates]]
    units = [
        Unit(f"validate {name}", _validation_unit, (train, validate_actual, validate_guess, fs))
        for name, fs in jobs
    ]
    validation: dict[str, dict[str, float]] = {}
    # A candidate above the imputation limit is vetoed, so one such feature does not stop the
    # run.
    refused: set[str] = set()
    for (name, features), result in zip(jobs, run_units(units, 1, "validate"), strict=True):
        if result is None:
            refused.update(features)
        else:
            validation[name] = result
    refused |= _refused_in_windows(actual, plan, [f for f in candidates if f not in refused])
    baseline = validation[ELO]["guess"]
    vetoed = tuple(
        f
        for f in candidates
        if f in refused or validation[f"{ELO}+{f}"]["guess"] > baseline + VETO_TOLERANCE
    )
    return validation, vetoed


def _fit_windows(
    actual: Sequence[TrainingRow], plan: SeasonPlan
) -> list[tuple[int, list[TrainingRow]]]:
    """Return ``(season, fit rows)`` for each scored season: all earlier seasons from the first."""
    cohort = [r for r in actual if in_cohort(r.game, plan)]
    windows = []
    for season in sorted({r.game.season for r in cohort}):
        # Only seasons before the scored one, whatever the id step between seasons.
        rows = [r for r in actual if plan.fit_first <= r.game.season < season]
        if not rows:
            raise HockeyModelError(
                f"backtest: no games to fit before season {season}; "
                "run `hockey-model refresh --backfill-from 2014-10-01`"
            )
        windows.append((season, rows))
    return windows


def _refused_in_windows(
    actual: Sequence[TrainingRow], plan: SeasonPlan, candidates: Sequence[str]
) -> set[str]:
    """Return the candidates above the imputation limit in any walk-forward fit window.

    The scoring fits use windows that selection never saw, such as the first season alone, so
    the veto is decided here, before scoring.
    """
    refused: set[str] = set()
    for _, rows in _fit_windows(actual, plan):
        for name in candidates:
            try:
                check_imputation([r.features for r in rows], (name,))
            except ImputationLimitError:
                refused.add(name)
    return refused


def _season_fit(
    season: int, fit_rows: Sequence[TrainingRow], tested: Sequence[str]
) -> tuple[int, LogisticFit, float]:
    """Fit on the rows before ``season``. Return the fit and the home win rate of those rows."""
    return season, _fit(fit_rows, tested), sum(_wins(fit_rows)) / len(fit_rows)


def merge_scored(parts: Sequence[_Scored]) -> list[_Scored]:
    """Order scored games by season, then start, then game ID.

    The block bootstrap depends on the order of its input, so every merge of units uses this
    order, whatever order the units finished in.
    """
    return sorted(parts, key=lambda g: (g.row.game.season, g.row.game.start, g.row.game.game_id))


def _check_same_games(actual: Sequence[TrainingRow], guess: Sequence[TrainingRow]) -> None:
    """Raise unless the actual run and the guess run scored the same games."""
    only_actual = {r.game.game_id for r in actual} - {r.game.game_id for r in guess}
    only_guess = {r.game.game_id for r in guess} - {r.game.game_id for r in actual}
    if only_actual or only_guess:
        raise HockeyModelError(
            f"backtest: the actual run and the guess run differ in the scored games: "
            f"{len(only_actual)} only in the actual run (first {sorted(only_actual)[:5]}), "
            f"{len(only_guess)} only in the guess run (first {sorted(only_guess)[:5]}); "
            "build both runs from the same games"
        )


def _score_seasons(
    actual: Sequence[TrainingRow],
    guess: Sequence[TrainingRow],
    tested: Sequence[str],
    plan: SeasonPlan,
) -> list[_Scored]:
    """Fit before each scored season on all earlier seasons, then score that season once."""
    by_game = {r.game.game_id: r for r in guess}
    scored: list[_Scored] = []
    cohort = [r for r in actual if in_cohort(r.game, plan)]
    _check_same_games(cohort, [r for r in guess if in_cohort(r.game, plan)])
    windows = _fit_windows(actual, plan)
    units = [
        Unit(f"walk-forward fit before season {season}", _season_fit, (season, rows, tested))
        for season, rows in windows
    ]
    for season, fit, home_rate in run_units(units, 1, "walk-forward"):
        for row in [r for r in cohort if r.game.season == season]:
            scored.append(
                _Scored(
                    row,
                    {
                        ELO: 1.0 / (1.0 + math.exp(-row.features.elo_logit)),
                        HOME_RATE: home_rate,
                        MODEL_ACTUAL: predict_logistic(fit, row.features),
                        MODEL_GUESS: predict_logistic(fit, by_game[row.game.game_id].features),
                    },
                )
            )
    return merge_scored(scored)


def _scored_run(
    name: str,
    probs: Sequence[float],
    wins: Sequence[bool],
    elo_losses: Sequence[float],
    tail: float,
) -> ScoredRun:
    ci = None
    mean_diff = None
    if name != ELO:
        diffs = [a - b for a, b in zip(per_game_log_loss(probs, wins), elo_losses, strict=True)]
        # Too few blocks leave the interval unavailable, never a false certainty.
        if bootstrap_problem(len(diffs)) is None:
            ci = block_bootstrap_mean_ci(diffs, tail)
        mean_diff = sum(diffs) / len(diffs)
    return ScoredRun(
        name,
        len(probs),
        log_loss(probs, wins),
        brier(probs, wins),
        calibration(probs, wins),
        ci,
        mean_diff,
    )


def _records(games: Sequence[_Scored]) -> list[GameRecord]:
    return [
        GameRecord(
            g.row.game.game_id,
            g.row.game.start,
            g.row.game.home,
            g.row.game.away,
            run,
            g.by_run[run],
            g.row.home_won,
        )
        for run in RECORDED_RUNS
        for g in games
    ]


def _missing_counts(games: Sequence[_Scored]) -> dict[str, int]:
    """Count the scored games that had each kind of missing input."""
    counts: Counter[str] = Counter()
    for game in games:
        counts.update({m.kind.value for m in game.row.features.missing})
    return dict(sorted(counts.items()))


def _build_report(
    conn: sqlite3.Connection,
    plan: SeasonPlan,
    candidates: Sequence[str],
    scoring: tuple[Mapping[str, Mapping[str, float]], tuple[str, ...]],
    tested: tuple[str, ...],
    games: Sequence[_Scored],
) -> BacktestReport:
    validation, vetoed = scoring
    wins = [g.row.home_won for g in games]
    elo_losses = per_game_log_loss([g.by_run[ELO] for g in games], wins)
    names = (HOME_RATE, ELO, MODEL_ACTUAL, MODEL_GUESS)
    units = [
        Unit(
            f"score {name}",
            _scored_run,
            (name, [g.by_run[name] for g in games], wins, elo_losses, plan.interval_tail),
        )
        for name in names
    ]
    runs = tuple(run_units(units, 1, "score"))
    coverage = season_counts(conn, ((g.row.game.season, g.row.game.game_type) for g in games), plan)
    reasons = ineligibility_reasons(coverage, len(games))
    model = next(r for r in runs if r.name == MODEL_ACTUAL)
    success = not reasons and model.diff_vs_elo_ci is not None and model.diff_vs_elo_ci[1] < 0.0
    uses_moneypuck = bool(MONEYPUCK_FEATURES & set(tested))
    quality = feature_quality([g.row.features for g in games], tested)
    return BacktestReport(
        candidate_features=tuple(candidates),
        vetoed=vetoed,
        tested_features=tested,
        live_features=tested if success and is_production(candidates) else (),
        validation=validation,
        runs=runs,
        success=success,
        notes=joint_test_notes(tested, vetoed, success)
        + candidate_notes(candidates)
        + eligibility_notes(reasons, coverage),
        credits=(MONEYPUCK_CREDIT,) if uses_moneypuck else (),
        missing_inputs=_missing_counts(games),
        coverage=coverage,
        eligible=not reasons,
        ineligible_reasons=reasons,
        imputation=tuple(asdict(q) for q in quality),
    )


def _build_rows(
    conn: sqlite3.Connection,
    first: int,
    last: int,
    mode: GoalieMode,
    burn_in: int,
    names: Sequence[str],
) -> list[TrainingRow]:
    return training_rows(conn, first, last, mode, burn_in, names=names)


def run_backtest(
    conn: sqlite3.Connection,
    plan: SeasonPlan = DEFAULT_PLAN,
    *,
    candidates: Sequence[str],
    workers: int = 1,
) -> tuple[BacktestReport, list[GameRecord]]:
    """Run the walk-forward backtest on the stored games.

    Success is one test: the upper end of the block-bootstrap interval of the
    per-game log loss of the model minus that of the Elo, with the goalie who started,
    is below zero. The guess run never changes success. Success also needs a covered
    cohort and enough independent blocks; see ``BacktestReport.eligible``.

    The feature builds, the fits, and the bootstrap runs are named units, and each logs its
    wall time. ``workers`` above 1 builds the rows one season at a time in worker processes,
    each with its own read-only store connection, because the builds take more than 99.9% of
    the time. The report and the records are the same for every worker count. A store file
    without uncommitted changes always builds this way, even with one worker, and reads a
    season from the season row cache (``row_cache``) when its key has not changed.
    """
    check_workers(workers)
    started = time.monotonic()
    first, last = plan.fit_first, plan.scored[1]
    if can_shard(conn, workers):
        actual, guess = build_sharded(conn, plan, candidates, workers)
    else:
        builds = [
            Unit(
                f"build {mode.value} rows",
                _build_rows,
                (conn, first, last, mode, plan.burn_in, candidates),
            )
            for mode in (GoalieMode.ACTUAL, GoalieMode.GUESS)
        ]
        actual, guess = run_units(builds, 1, "build-rows")
    scoring = _validate(actual, guess, plan, candidates)
    tested = tuple(f for f in candidates if f not in scoring[1])
    games = _score_seasons(actual, guess, tested, plan)
    if not games:
        raise HockeyModelError(
            f"backtest: no games in the scored {scored_label(plan)}; "
            "run `hockey-model refresh --backfill-from 2014-10-01`"
        )
    report = _build_report(conn, plan, candidates, scoring, tested, games)
    elapsed = time.monotonic() - started
    target = (
        f"; the target on the real store is {PARALLEL_TARGET_S}s"
        if workers == PARALLEL_TARGET_WORKERS
        else ""
    )
    print(f"backtest: total {elapsed:.1f}s with {workers} worker(s){target}", file=sys.stderr)
    return report, _records(games)
