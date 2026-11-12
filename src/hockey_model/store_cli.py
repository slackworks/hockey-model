"""CLI commands that work on the SQLite store: refresh, and the model commands."""

import argparse
import os
import sys
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from hockey_model.backtest.game_records import write_game_records
from hockey_model.backtest.runner import run_backtest
from hockey_model.daily_report import build_report, model_identity, report_tag, write_report
from hockey_model.game import game_win_probability
from hockey_model.game_day import game_day_of, game_day_window
from hockey_model.game_model.features import (
    ATTEMPT_FEATURES,
    FEATURES,
    GoalieMode,
    feature_name_problem,
)
from hockey_model.game_model.imputation import check_imputation
from hockey_model.game_model.logistic import ELO_ONLY, fit_logistic
from hockey_model.game_model.nhl_model import NhlGameModel
from hockey_model.game_model.seasons import FIT_FIRST, season_of
from hockey_model.game_model.training import game_input, training_rows
from hockey_model.report_gate import decide, warning
from hockey_model.report_trigger import first_puck_drop, puck_drop_ahead
from hockey_model.sources.http import HttpClient
from hockey_model.sources.nhl import GameRow, fetch_score_day
from hockey_model.store.db import connect, db_path_from_env
from hockey_model.store.fits import newest_fit, save_fit
from hockey_model.store.reads import game_row
from hockey_model.store.refresh import RefreshReport, refresh, refresh_starters
from hockey_model.store.timestamps import iso_utc
from hockey_model.store.upgrade import add_store_group, run_store_group
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
_CANDIDATE_SETS = {"xg": FEATURES, "attempt": ATTEMPT_FEATURES}


def add_store_commands(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    """Add the store commands to the CLI parser."""
    add_store_group(add_parser)
    parser = add_parser("refresh", help="read every source into the store")
    parser.add_argument(
        "--backfill-from",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="read score days from this date; days already read after they ended are skipped",
    )
    parser.add_argument(
        "--reread-moneypuck",
        action="store_true",
        help="read the MoneyPuck files even when they are fresh",
    )
    parser.add_argument(
        "--retry-skipped",
        action="store_true",
        help="read again the score days, boxscores, and MoneyPuck files that earlier reads"
        " skipped as known gaps; one good read clears a gap. A boxscore whose last read failed"
        " less than a day ago still waits",
    )
    parser.add_argument(
        "--budget-seconds",
        type=int,
        default=None,
        metavar="N",
        help="stop reading score days and boxscores after N seconds; a backfill then reports"
        " backfill_complete false and the next run continues it",
    )
    parser.add_argument(
        "--starters-only",
        action="store_true",
        help="read only the schedule, the goalie sources, and the injuries",
    )
    fit = add_parser("fit", help="fit the game model on the stored games and save the fit")
    fit.add_argument(
        "--through-season",
        type=int,
        default=None,
        metavar="YYYYYYYY",
        help="last season in the fit; default is the season that holds today (UTC)",
    )
    fit.add_argument(
        "--features",
        default=None,
        help=(
            "comma-separated features, required. Use the live_features of the latest backtest "
            'report; "" fits the Elo alone, which is the result of a failed joint test'
        ),
    )
    backtest = add_parser("backtest", help="score the model on the stored games")
    backtest.add_argument(
        "--games-out", type=Path, metavar="PATH", help="write one CSV row per predicted game"
    )
    backtest.add_argument(
        "--candidates",
        choices=sorted(_CANDIDATE_SETS),
        default="xg",
        help="xg: the production candidates; attempt: xg_form replaced by attempt_form (#77)",
    )
    game = add_parser("game", help="predict one stored game with the newest fit")
    game.add_argument("game_id", type=int)
    game.add_argument("--level", type=float, default=0.8)
    experiments = add_parser("experiments", help="manage the experiment registry")
    exp_sub = experiments.add_subparsers(dest="experiment_command")
    exp_sub.add_parser("check", help="validate all experiment records")
    exp_run = exp_sub.add_parser(
        "run",
        help="run a backtest for one experiment record",
    )
    exp_run.add_argument("record_id", type=str)
    exp_run.add_argument(
        "--games-out",
        type=Path,
        metavar="PATH",
        help="write one CSV row per predicted game, in addition to the record's result CSV",
    )
    daily = add_parser(
        "daily", help="predict every game of one Eastern game day; write Markdown and JSON"
    )
    daily.add_argument(
        "--date",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help="Eastern game day to predict; default is the game day that holds now",
    )
    daily.add_argument("--out-dir", type=Path, required=True, metavar="DIR")
    daily.add_argument("--level", type=float, default=0.8)
    due = add_parser("report-due", help="say whether this trigger publishes the daily report")
    due.add_argument(
        "--date",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help="Eastern game day to check; default is the game day that holds now",
    )
    due.add_argument("--force", action="store_true", help="publish even outside the trigger")
    decide_parser = add_parser(
        "report-decide", help="decide the publish step of a trigger from the facts the gate read"
    )
    decide_parser.add_argument("--due", choices=("true", "false"), required=True)
    decide_parser.add_argument("--games", type=int, required=True)
    decide_parser.add_argument("--date", required=True, metavar="YYYY-MM-DD")
    decide_parser.add_argument("--first-puck-drop", default="", metavar="ISO")
    decide_parser.add_argument("--margin-seconds", type=int, default=0)
    decide_parser.add_argument("--force", action="store_true")
    decide_parser.add_argument("--dry-run", action="store_true")
    decide_parser.add_argument(
        "--published",
        choices=("true", "false", "unknown"),
        default="unknown",
        help="whether the day has a release; unknown until the caller has read it",
    )
    due.add_argument(
        "--margin-seconds",
        type=int,
        default=0,
        help="seconds the run needs after it starts; due only when it can finish before puck drop",
    )


def _report_json(report: RefreshReport) -> dict[str, object]:
    return {
        "written": report.written,
        "errors": [{"type": type(e).__name__, "message": str(e)} for e in report.errors],
        "notes": list(report.notes),
        "credits": list(report.credits),
        "backfill_complete": report.backfill_complete,
    }


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
    through = args.through_season
    if through is None:
        through = season_of(datetime.now(UTC).date())
    conn = connect(db_path_from_env(os.environ))
    try:
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
        else:
            quality, fit, games = [], ELO_ONLY, 0
        fit_id = save_fit(conn, fit, through, datetime.now(UTC), quality)
    finally:
        conn.close()
    return {
        "fit_id": fit_id,
        "through_season": through,
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
        model = NhlGameModel(conn, newest_fit(conn))
        return game_win_probability(game_input(row), model, level=args.level), 0
    finally:
        conn.close()


def _daily(args: argparse.Namespace) -> tuple[object, int]:
    now = datetime.now(UTC)
    day = args.date if args.date is not None else game_day_of(now)
    conn = connect(db_path_from_env(os.environ))
    try:
        fit = newest_fit(conn)
        report = build_report(conn, NhlGameModel(conn, fit), day, now=now, level=args.level)
        # Actions sets GITHUB_SHA. A local run has no commit to name.
        report = replace(report, model=model_identity(fit, os.environ.get("GITHUB_SHA")))
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
        decision, day=args.date, first_drop=args.first_puck_drop, margin_seconds=args.margin_seconds
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
        "due": args.force or puck_drop_ahead(now, first, timedelta(seconds=args.margin_seconds)),
    }, 0


def _backtest(args: argparse.Namespace) -> tuple[object, int]:
    conn = connect(db_path_from_env(os.environ))
    try:
        report, records = run_backtest(conn, candidates=_CANDIDATE_SETS[args.candidates])
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
