"""The walk-forward backtest of the NHL game model against game results.

The backtest uses no price data. It scores the model against the results of the games, a
home win rate, and the results-only Elo.
"""

import math
import sqlite3
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
from hockey_model.game_model.features import FEATURES, GOALIE_FEATURE, GoalieMode
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

MONEYPUCK_LEAK_NOTE = (
    "The MoneyPuck expected-goals model is today's model. MoneyPuck possibly trained it on "
    "games after the scored seasons, so results that use MoneyPuck data may look better than "
    "they would have then."
)
GUESS_EQUALS_ACTUAL_NOTE = (
    "No tested feature reads the starting goalie, so the actual run and the guess run are the "
    "same by construction. The equal scores do not show that a wrong goalie costs nothing."
)
INJURY_HISTORY_NOTE = (
    "The store has no ESPN injury history before it began to keep injury reads, so the goalie "
    "guess run never leaves out an injured goalie. The live model does."
)
# Every feature here reads MoneyPuck data, so a run that uses one carries the credit and the note.
MONEYPUCK_FEATURES = frozenset({"xg_form", "attempt_form", "goalie_gsax"})
ELO = "elo"
HOME_RATE = "home_rate"
MODEL_ACTUAL = "model_actual"
MODEL_GUESS = "model_guess"
RECORDED_RUNS = (ELO, MODEL_ACTUAL, MODEL_GUESS)
# A feature is vetoed only when it is worse by more than rounding error.
VETO_TOLERANCE = 1e-9


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


def _validate(
    actual: Sequence[TrainingRow],
    guess: Sequence[TrainingRow],
    plan: SeasonPlan,
    candidates: Sequence[str],
) -> tuple[dict[str, dict[str, float]], tuple[str, ...]]:
    """Score the Elo alone and the Elo plus each feature on the validation seasons.

    Each fit uses the training seasons with the goalie who started. It is scored with that
    goalie ("actual") and with the guessed goalie ("guess"). A feature is vetoed when its
    guess score is worse than the Elo alone.
    """
    train = _in(actual, *plan.select_train)
    validate_actual = _in(actual, *plan.select_validate)
    validate_guess = _in(guess, *plan.select_validate)
    if not train or not validate_actual:
        raise HockeyModelError(
            f"backtest: no games in the selection seasons {plan.select_train} and "
            f"{plan.select_validate}; run `hockey-model refresh --backfill-from 2014-10-01`"
        )
    validation: dict[str, dict[str, float]] = {}
    # A candidate above the imputation limit is vetoed, so one such feature does not stop the
    # run. The Elo alone is never refused: it has no feature to impute.
    refused: set[str] = set()
    for name, features in [(ELO, ()), *[(f"{ELO}+{f}", (f,)) for f in candidates]]:
        try:
            fit = _fit(train, features)
        except ImputationLimitError:
            if not features:
                raise
            refused.update(features)
            continue
        validation[name] = {
            "actual": _loss(fit, validate_actual),
            "guess": _loss(fit, validate_guess),
        }
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
    for season, fit_rows in _fit_windows(actual, plan):
        fit = _fit(fit_rows, tested)
        home_rate = sum(_wins(fit_rows)) / len(fit_rows)
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
    return scored


def _scored_run(
    name: str, games: Sequence[_Scored], elo_losses: Sequence[float], tail: float
) -> ScoredRun:
    probs = [g.by_run[name] for g in games]
    wins = [g.row.home_won for g in games]
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
        len(games),
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


def _notes(tested: Sequence[str], vetoed: Sequence[str], success: bool) -> tuple[str, ...]:
    count = len(tested) + len(vetoed)
    notes = [f"{count} candidate features were tested together as one joint test."]
    if vetoed:
        notes.append(f"The guess run vetoed: {', '.join(vetoed)}.")
    if not success:
        notes.append("The joint test failed, so the Elo alone ships.")
    if MONEYPUCK_FEATURES & set(tested):
        notes.append(MONEYPUCK_LEAK_NOTE)
    if GOALIE_FEATURE not in tested:
        notes.append(GUESS_EQUALS_ACTUAL_NOTE)
    notes.append(INJURY_HISTORY_NOTE)
    return tuple(notes)


def _is_production(candidates: Sequence[str]) -> bool:
    return tuple(candidates) == FEATURES


def _candidate_notes(candidates: Sequence[str]) -> tuple[str, ...]:
    """Say so when a run tests a set other than the production set, so no result reads as live."""
    if _is_production(candidates):
        return ()
    return (
        "This candidate set is a test. Nothing ships from it, and the live model reads only the "
        "production features.",
    )


def _eligibility_notes(reasons: Sequence[str], coverage: Sequence[SeasonCount]) -> tuple[str, ...]:
    """Say why success is refused, and name the seasons with stored games that never finished."""
    notes = [f"Not eligible for success: {reason}." for reason in reasons]
    notes.extend(
        f"Season {c.season} game type {c.game_type} holds {c.unresolved} stored game(s) that are "
        "not final long after their start."
        for c in coverage
        if c.unresolved
    )
    return tuple(notes)


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
    runs = tuple(
        _scored_run(name, games, elo_losses, plan.interval_tail)
        for name in (HOME_RATE, ELO, MODEL_ACTUAL, MODEL_GUESS)
    )
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
        live_features=tested if success and _is_production(candidates) else (),
        validation=validation,
        runs=runs,
        success=success,
        notes=_notes(tested, vetoed, success)
        + _candidate_notes(candidates)
        + _eligibility_notes(reasons, coverage),
        credits=(MONEYPUCK_CREDIT,) if uses_moneypuck else (),
        missing_inputs=_missing_counts(games),
        coverage=coverage,
        eligible=not reasons,
        ineligible_reasons=reasons,
        imputation=tuple(asdict(q) for q in quality),
    )


def run_backtest(
    conn: sqlite3.Connection,
    plan: SeasonPlan = DEFAULT_PLAN,
    *,
    candidates: Sequence[str],
) -> tuple[BacktestReport, list[GameRecord]]:
    """Run the walk-forward backtest on the stored games.

    Success is one test: the 95th percentile of the block-bootstrap interval of the
    per-game log loss of the model minus that of the Elo, with the goalie who started,
    is below zero. The guess run never changes success. Success also needs a covered
    cohort and enough independent blocks; see ``BacktestReport.eligible``.
    """
    first, last = plan.fit_first, plan.scored[1]
    actual = training_rows(conn, first, last, GoalieMode.ACTUAL, plan.burn_in, names=candidates)
    guess = training_rows(conn, first, last, GoalieMode.GUESS, plan.burn_in, names=candidates)
    scoring = _validate(actual, guess, plan, candidates)
    tested = tuple(f for f in candidates if f not in scoring[1])
    games = _score_seasons(actual, guess, tested, plan)
    if not games:
        raise HockeyModelError(
            f"backtest: no games in the scored {scored_label(plan)}; "
            "run `hockey-model refresh --backfill-from 2014-10-01`"
        )
    return _build_report(conn, plan, candidates, scoring, tested, games), _records(games)
