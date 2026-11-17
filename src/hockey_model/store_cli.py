"""CLI commands that work on the SQLite store: refresh, and the model commands."""

import argparse
import os
import sys
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from hockey_model.backtest.game_records import write_game_records
from hockey_model.backtest.runner import run_backtest
from hockey_model.daily_report import build_report, model_identity, report_tag, write_report
from hockey_model.game import game_win_probability
from hockey_model.game_day import game_day_of, game_day_window
from hockey_model.game_model.features import GoalieMode, feature_name_problem
from hockey_model.game_model.imputation import check_imputation
from hockey_model.game_model.logistic import ELO_ONLY, fit_logistic
from hockey_model.game_model.nhl_model import NhlGameModel
from hockey_model.game_model.seasons import FIT_FIRST
from hockey_model.game_model.training import (
    game_input,
    last_trained_season,
    last_trained_start,
    training_rows,
)
from hockey_model.report_gate import decide, warning
from hockey_model.report_trigger import first_puck_drop, prep_too_early, puck_drop_ahead
from hockey_model.sources.http import HttpClient
from hockey_model.sources.nhl import GameRow, fetch_score_day
from hockey_model.store.db import connect, db_path_from_env
from hockey_model.store.fits import LIVE_LABEL, FitRecord, FitRef, load_fit, save_fit
from hockey_model.store.reads import game_row
from hockey_model.store.refresh import RefreshReport, refresh, refresh_starters
from hockey_model.store.timestamps import iso_utc
from hockey_model.store.upgrade import run_store_group
from hockey_model.store_cli_parsers import CANDIDATE_SETS
from hockey_model.types import HockeyModelError

STORE_COMMANDS = frozenset(
    {
        "refresh",
        "fit",
        "game",
        "backtest",
        "experiments",
        "daily",
        "report-due",
        "report-decide",
        "store",
    }
)


def _report_json(report: RefreshReport) -> dict[str, object]:
    return {
        "written": report.written,
        "errors": [{"type": type(e).__name__, "message": str(e)} for e in report.errors],
        "notes": list(report.notes),
        "credits": list(report.credits),
        "backfill_complete": report.backfill_complete,
        "timed_out": report.timed_out,
    }


def _live_ref() -> FitRef:
    """Return the request for the fit that the daily run serves."""
    return FitRef(label=LIVE_LABEL)


def _refresh(args: argparse.Namespace) -> tuple[object, int]:
    if args.starters_only and args.backfill_from is not None:
        raise HockeyModelError("refresh: a backfill reads every source; drop --starters-only")
    if args.starters_only and args.budget_seconds is not None:
        raise HockeyModelError("refresh: --starters-only has no budget; drop --budget-seconds")
    if args.budget_seconds is not None and args.backfill_from is None:
        raise HockeyModelError(
            "refresh: --budget-seconds needs --backfill-from; a plain refresh that stops early "
            "would exit 0 and report nothing"
        )
    if args.budget_seconds is not None and args.budget_seconds <= 0:
        raise HockeyModelError(
            f"refresh: --budget-seconds is {args.budget_seconds}; give a number above 0"
        )
    conn = connect(db_path_from_env(os.environ))
    try:
        if args.starters_only:
            report = refresh_starters(
                conn, HttpClient(), now=datetime.now(UTC), clock=lambda: datetime.now(UTC)
            )
        else:
            report = refresh(
                conn,
                HttpClient(),
                now=datetime.now(UTC),
                backfill_from=args.backfill_from,
                clock=lambda: datetime.now(UTC),
                reread_moneypuck=args.reread_moneypuck,
                retry_skipped=args.retry_skipped,
                budget=None
                if args.budget_seconds is None
                else timedelta(seconds=args.budget_seconds),
            )
    finally:
        conn.close()
    return _report_json(report), 1 if report.errors else 0


def parse_features(text: str | None) -> tuple[str, ...]:
    """Return the features of a fit. The caller names them: no default is approved.

    The exact empty string means the Elo alone. A blank entry in a longer list is an error,
    because it would change the feature set without a word.
    """
    if text is None:
        raise HockeyModelError(
            "fit: --features is required. Pass the live_features of the latest backtest "
            'report, or "" for the Elo alone. A default would serve a feature that the '
            "backtest may have vetoed"
        )
    if text == "":
        return ()
    names = tuple(name.strip() for name in text.split(","))
    blanks = [str(i) for i, name in enumerate(names, start=1) if not name]
    if blanks:
        raise HockeyModelError(
            f"fit: --features {text!r} has a blank entry at position {', '.join(blanks)}; "
            'name each feature, or pass "" for the Elo alone'
        )
    problem = feature_name_problem(names)
    if problem is not None:
        raise HockeyModelError(f"fit: {problem}")
    return names


def _fit(args: argparse.Namespace) -> tuple[object, int]:
    features = parse_features(args.features)
    conn = connect(db_path_from_env(os.environ))
    try:
        # The cutoff follows the data, not the clock. A fit that records a season whose games
        # it never read would name those games in sample (#238).
        newest = last_trained_season(conn)
        if newest is None:
            raise HockeyModelError(
                f"fit: the store holds no final game from {FIT_FIRST}; "
                "run `hockey-model refresh --backfill-from 2014-10-01`"
            )
        through = newest if args.through_season is None else args.through_season
        if through > newest:
            raise HockeyModelError(
                f"fit through {through}: the store holds no final game after {newest}; "
                f"run `hockey-model refresh`, or pass --through-season {newest}"
            )
        if features:
            rows = training_rows(conn, FIT_FIRST, through, GoalieMode.ACTUAL, names=features)
            if not rows:
                raise HockeyModelError(
                    f"fit through {through}: no games in the store from {FIT_FIRST}; "
                    "run `hockey-model refresh --backfill-from 2014-10-01`"
                )
            # The check runs before the fit, so a refused fit saves no row and no artifact.
            quality = [asdict(q) for q in check_imputation([r.features for r in rows], features)]
            fit = fit_logistic([r.features for r in rows], [r.home_won for r in rows], features)
            games = len(rows)
            # The newest game that this fit read, not one that a coarser season cutoff names.
            trained_through = rows[-1].game.start
        else:
            quality, fit, games = [], ELO_ONLY, 0
            # The Elo replayed every stored result, so the season cutoff's newest game is the
            # bound. It read no feature row, but its ratings did read those results.
            trained_through = last_trained_start(conn, through)
        fit_id = save_fit(
            conn,
            FitRecord(
                fit,
                through,
                datetime.now(UTC),
                quality,
                label=args.label,
                trained_through=trained_through,
            ),
        )
    finally:
        conn.close()
    return {
        "fit_id": fit_id,
        "through_season": through,
        "trained_through": None if trained_through is None else iso_utc(trained_through),
        "games": games,
        "imputation": quality,
        **asdict(fit),
    }, 0


def _game(args: argparse.Namespace) -> tuple[object, int]:
    conn = connect(db_path_from_env(os.environ))
    try:
        row = game_row(conn, args.game_id)
        if row is None:
            raise HockeyModelError(
                f"predict game {args.game_id}: the game is not in the store; "
                "run `hockey-model refresh`"
            )
        ref = FitRef(fit_id=args.fit_id) if args.fit_id is not None else _live_ref()
        artifact = load_fit(conn, ref)
        model = NhlGameModel(conn, artifact)
        return game_win_probability(game_input(row), model, level=args.level), 0
    finally:
        conn.close()


def _daily(args: argparse.Namespace) -> tuple[object, int]:
    now = datetime.now(UTC)
    day = args.date if args.date is not None else game_day_of(now)
    conn = connect(db_path_from_env(os.environ))
    try:
        artifact = load_fit(conn, _live_ref())
        report = build_report(conn, NhlGameModel(conn, artifact), day, now=now, level=args.level)
        # Actions sets GITHUB_SHA. A local run has no commit to name.
        report = replace(report, model=model_identity(artifact.fit, os.environ.get("GITHUB_SHA")))
    finally:
        conn.close()
    md_path, json_path = write_report(report, args.out_dir)
    return {
        "date": day.isoformat(),
        "games": len(report.lines),
        "files": [str(md_path), str(json_path)],
    }, 0


def _game_day_games(day: date, closes: datetime) -> list[GameRow]:
    """Return the games of the Eastern game ``day`` from its score feed.

    The feed already groups games by Eastern date. The window check keeps a game whose
    start the feed lists under the wrong date out of the report.
    """
    opens = game_day_window(day)[0]
    feed = fetch_score_day(HttpClient(), day)
    games = [game for game in feed if opens <= game.start < closes]
    # A dropped game is named, so a feed with a wrong date does not pass as an off day.
    dropped = [str(game.game_id) for game in feed if not opens <= game.start < closes]
    if dropped:
        print(
            f"report-due: {len(dropped)} feed game(s) start outside {day}'s window and are "
            f"not counted: {', '.join(dropped)}",
            file=sys.stderr,
        )
    return games


def _report_decide(args: argparse.Namespace) -> tuple[object, int]:
    published = None if args.published == "unknown" else args.published == "true"
    decision = decide(
        due=args.due == "true",
        force=args.force,
        dry_run=args.dry_run,
        games=args.games,
        published=published,
    )
    note = warning(
        decision,
        day=args.date.isoformat(),
        first_drop=args.first_puck_drop,
        margin_seconds=args.margin_seconds,
    )
    return {**asdict(decision), "warning": note}, 0


def _report_due(args: argparse.Namespace) -> tuple[object, int]:
    now = datetime.now(UTC)
    day = args.date if args.date is not None else game_day_of(now)
    closes = game_day_window(day)[1]
    games = _game_day_games(day, closes)
    first = first_puck_drop(closes, [game.start for game in games])
    return {
        "date": day.isoformat(),
        "tag": report_tag(day),
        "games": len(games),
        "first_puck_drop_utc": iso_utc(first),
        "puck_drop_passed": now >= first,
        "prep_too_early": day == game_day_of(now) and prep_too_early(now, first),
        "due": args.force or puck_drop_ahead(now, first, timedelta(seconds=args.margin_seconds)),
    }, 0


def _backtest(args: argparse.Namespace) -> tuple[object, int]:
    conn = connect(db_path_from_env(os.environ))
    try:
        report, records = run_backtest(conn, candidates=CANDIDATE_SETS[args.candidates])
    finally:
        conn.close()
    if args.games_out is not None:
        write_game_records(args.games_out, records)
    return asdict(report), 0


def _experiments(args: argparse.Namespace) -> tuple[object, int]:
    """Handle the experiments subcommands."""
    command = getattr(args, "experiment_command", None)
    if command == "check":
        from hockey_model.experiments.check import check_all

        result = check_all()
        return result, 0 if result["ok"] else 1
    if command == "run":
        conn = connect(db_path_from_env(os.environ))
        try:
            from hockey_model.experiments.check import run_experiment

            result, _ = run_experiment(args.record_id, conn, args.games_out)
            return result, 0
        finally:
            conn.close()
    raise HockeyModelError("experiments: specify check or run")


def _store(args: argparse.Namespace) -> tuple[object, int]:
    """Handle the store subcommands. The file is --db, or HOCKEY_MODEL_DB."""
    path = Path(args.db) if args.db else db_path_from_env(os.environ)
    return run_store_group(args, path)


_HANDLERS = {
    "refresh": _refresh,
    "fit": _fit,
    "game": _game,
    "backtest": _backtest,
    "experiments": _experiments,
    "daily": _daily,
    "report-due": _report_due,
    "report-decide": _report_decide,
    "store": _store,
}


def run_store_command(args: argparse.Namespace) -> tuple[object, int]:
    """Run a store command. Return the JSON payload and the exit code."""
    return _HANDLERS[args.command](args)
