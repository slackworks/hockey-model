"""The argument parsers of the store commands."""

import argparse
from collections.abc import Callable
from datetime import date
from pathlib import Path

from hockey_model.game_model.features import ATTEMPT_FEATURES, FEATURES
from hockey_model.store.upgrade import add_store_group

_CANDIDATE_SETS: dict[str, tuple[str, ...]] = {"xg": FEATURES, "attempt": ATTEMPT_FEATURES}


def _add_refresh(parser: argparse.ArgumentParser) -> None:
    """Add the refresh options of a backfill and its known gaps."""
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


def _add_fit(parser: argparse.ArgumentParser) -> None:
    """Add the fit options that pick the season and the feature set."""
    parser.add_argument(
        "--through-season",
        type=int,
        default=None,
        metavar="YYYYYYYY",
        help="last season in the fit; default is the season that holds today (UTC)",
    )
    parser.add_argument(
        "--features",
        default=None,
        help=(
            "comma-separated features, required. Use the live_features of the latest backtest "
            'report; "" fits the Elo alone, which is the result of a failed joint test'
        ),
    )


def _add_backtest(parser: argparse.ArgumentParser) -> None:
    """Add the backtest options that pick the candidate set and the game records."""
    parser.add_argument(
        "--games-out", type=Path, metavar="PATH", help="write one CSV row per predicted game"
    )
    parser.add_argument(
        "--candidates",
        choices=sorted(_CANDIDATE_SETS),
        default="xg",
        help="xg: the production candidates; attempt: xg_form replaced by attempt_form (#77)",
    )


def _add_daily(parser: argparse.ArgumentParser) -> None:
    """Add the options that pick the predicted game day and the interval level."""
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help="Eastern game day to predict; default is the game day that holds now",
    )
    parser.add_argument("--out-dir", type=Path, required=True, metavar="DIR")
    parser.add_argument("--level", type=float, default=0.8)


def _add_experiments(parser: argparse.ArgumentParser) -> None:
    """Add the two experiment subcommands."""
    sub = parser.add_subparsers(dest="experiment_command")
    sub.add_parser("check", help="validate all experiment records")
    run = sub.add_parser("run", help="run a backtest for one experiment record")
    run.add_argument("record_id", type=str)
    run.add_argument(
        "--games-out",
        type=Path,
        metavar="PATH",
        help="write one CSV row per predicted game, in addition to the record's result CSV",
    )


def _add_report_due(parser: argparse.ArgumentParser) -> None:
    """Add the options of the trigger check: the day, the force, and the run margin."""
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help="Eastern game day to check; default is the game day that holds now",
    )
    parser.add_argument("--force", action="store_true", help="publish even outside the trigger")
    parser.add_argument(
        "--margin-seconds",
        type=int,
        default=0,
        help="seconds the run needs after it starts; due only when it can finish before puck drop",
    )


def _add_report_decide(parser: argparse.ArgumentParser) -> None:
    """Add the facts that the gate read and the options of the publish decision."""
    parser.add_argument("--due", choices=("true", "false"), required=True)
    parser.add_argument("--games", type=int, required=True)
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        required=True,
        metavar="YYYY-MM-DD",
    )
    parser.add_argument("--first-puck-drop", default="", metavar="ISO")
    parser.add_argument("--margin-seconds", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--published",
        choices=("true", "false", "unknown"),
        default="unknown",
        help="whether the day has a release. With unknown, check lookup_needed in the answer:"
        " when it is true, read the release and ask again",
    )


def add_store_commands(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    """Add the store commands to the CLI parser."""
    add_store_group(add_parser)
    refresh_parser = add_parser("refresh", help="read every source into the store")
    _add_refresh(refresh_parser)
    _add_fit(add_parser("fit", help="fit the game model on the stored games and save the fit"))
    _add_backtest(add_parser("backtest", help="score the model on the stored games"))
    game = add_parser("game", help="predict one stored game with the newest fit")
    game.add_argument("game_id", type=int)
    game.add_argument("--level", type=float, default=0.8)
    _add_experiments(add_parser("experiments", help="manage the experiment registry"))
    daily = add_parser(
        "daily", help="predict every game of one Eastern game day; write Markdown and JSON"
    )
    _add_daily(daily)
    _add_report_due(
        add_parser("report-due", help="say whether this trigger publishes the daily report")
    )
    _add_report_decide(
        add_parser(
            "report-decide",
            help="decide the publish step of a trigger from the facts the gate read",
        )
    )


def candidates(name: str) -> tuple[str, ...]:
    """Return the feature candidate set that ``--candidates`` names."""
    return _CANDIDATE_SETS[name]
