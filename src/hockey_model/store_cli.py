"""CLI commands that work on the SQLite store: refresh, and the model commands."""

import argparse
import os
import sys
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from hockey_model import progress
from hockey_model.backtest.game_records import write_game_records
from hockey_model.backtest.runner import run_backtest
from hockey_model.backtest.shards import actual_rows
from hockey_model.daily_report import build_report, model_identity, report_tag, write_report
from hockey_model.game import game_win_probability
from hockey_model.game_day import game_day_of, game_day_window
from hockey_model.game_model.features import FEATURES, feature_name_problem
from hockey_model.game_model.imputation import check_imputation
from hockey_model.game_model.logistic import ELO_ONLY, fit_logistic
from hockey_model.game_model.nhl_model import NhlGameModel
from hockey_model.game_model.seasons import FIT_FIRST
from hockey_model.game_model.training import (
    game_input,
    last_trained_season,
    last_trained_start,
)
from hockey_model.report_gate import compare_inputs, decide, warning
from hockey_model.report_schema import write_index
from hockey_model.report_summary import summarize_file
from hockey_model.report_trigger import (
    first_puck_drop,
    prep_blocks_report,
    puck_drop_ahead,
)
from hockey_model.sources.http import HttpClient
from hockey_model.sources.nhl import GameRow, fetch_score_day
from hockey_model.store.db import connect, db_path_from_env
from hockey_model.store.fits import LIVE_LABEL, FitRecord, FitRef, load_fit, save_fit
from hockey_model.store.reads import game_row
from hockey_model.store.timestamps import iso_utc, parse_iso_utc
from hockey_model.store_cli_refresh import refresh_command
from hockey_model.store_group import run_store_group
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
        "report-compare",
        "report-index",
        "report-summary",
        "store",
    }
)


def _live_ref() -> FitRef:
    """Return the request for the fit that the daily run serves."""
    return FitRef(label=LIVE_LABEL)


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
            rows = actual_rows(conn, FIT_FIRST, through, features, args.workers)
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


def _game_day_games(day: date, closes: datetime) -> tuple[list[GameRow], int]:
    """Return the games of the Eastern game ``day`` from its score feed, and the drop count.

    The feed already groups games by Eastern date. The window check keeps a game whose
    start the feed lists under the wrong date out of the report. The count of those games
    goes back to the caller, because a feed with a wrong date must not pass as an off day.
    """
    opens = game_day_window(day)[0]
    feed = fetch_score_day(HttpClient(), day)
    games = [game for game in feed if opens <= game.start < closes]
    outside = [game for game in feed if not opens <= game.start < closes]
    if outside:
        ids = ", ".join(str(game.game_id) for game in outside)
        print(
            f"report-due: {len(outside)} feed game(s) start outside {day}'s window and are "
            f"not counted: {ids}",
            file=sys.stderr,
        )
    return games, len(outside)


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


def _report_compare(args: argparse.Namespace) -> tuple[object, int]:
    return compare_inputs(args.built, args.published), 0


def _report_summary(args: argparse.Namespace) -> tuple[object, int]:
    return {"summary": summarize_file(args.report)}, 0


def _report_index(args: argparse.Namespace) -> tuple[object, int]:
    builds, file_name = write_index(args.report, args.date.isoformat(), args.existing, args.out)
    return {"out": str(args.out), "builds": builds, "file": file_name}, 0


def _prep_blocking(args: argparse.Namespace, first: datetime) -> bool:
    """Return True when a prepare run that holds the store group now outlives the puck drop."""
    if not args.prep_ends:
        return False
    margin = timedelta(seconds=args.margin_seconds)
    return prep_blocks_report(parse_iso_utc(args.prep_ends), first, margin)


def _report_due(args: argparse.Namespace) -> tuple[object, int]:
    now = datetime.now(UTC)
    day = args.date if args.date is not None else game_day_of(now)
    closes = game_day_window(day)[1]
    games, outside = _game_day_games(day, closes)
    first = first_puck_drop(closes, [game.start for game in games])
    return {
        "date": day.isoformat(),
        "tag": report_tag(day),
        "games": len(games),
        "games_outside_window": outside,
        "first_puck_drop_utc": iso_utc(first),
        "puck_drop_passed": now >= first,
        "prep_blocks_report": _prep_blocking(args, first),
        "due": args.force or puck_drop_ahead(now, first, timedelta(seconds=args.margin_seconds)),
    }, 0


def _backtest(args: argparse.Namespace) -> tuple[object, int]:
    conn = connect(db_path_from_env(os.environ))
    try:
        report, records = run_backtest(conn, candidates=FEATURES, workers=args.workers)
    finally:
        conn.close()
    if args.games_out is not None:
        write_game_records(args.games_out, records)
    return asdict(report), 0


def _experiments(args: argparse.Namespace) -> tuple[object, int]:
    """Handle the experiments subcommands."""
    from hockey_model.experiments.command import run_experiments_command

    return run_experiments_command(args)


def _store(args: argparse.Namespace) -> tuple[object, int]:
    """Handle the store subcommands. The file is --db, or HOCKEY_MODEL_DB."""
    path = Path(args.db).expanduser() if args.db else db_path_from_env(os.environ)
    return run_store_group(args, path)


_HANDLERS = {
    "refresh": refresh_command,
    "fit": _fit,
    "game": _game,
    "backtest": _backtest,
    "experiments": _experiments,
    "daily": _daily,
    "report-due": _report_due,
    "report-decide": _report_decide,
    "report-compare": _report_compare,
    "report-index": _report_index,
    "report-summary": _report_summary,
    "store": _store,
}


# These commands can run for more than a minute, so each logs its progress on stderr.
_LONG_COMMANDS = frozenset(
    {"refresh", "fit", "backtest", "daily", "experiments", "report-due", "store"}
)


def run_store_command(args: argparse.Namespace) -> tuple[object, int]:
    """Run a store command. Return the JSON payload and the exit code.

    A long command runs inside a progress step, so stderr shows its start, a tick at a set
    interval, and its end with the time it took.
    """
    handler = _HANDLERS[args.command]
    if args.command not in _LONG_COMMANDS:
        return handler(args)
    with progress.step(args.command, "run"):
        payload, code = handler(args)
        if code != 0:
            progress.stopped(f"exit code {code}")
        return payload, code
