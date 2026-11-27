"""The ``store`` command group: ``upgrade``, ``rebuild``, ``reconcile``, and list ``gaps``."""

import argparse
import sqlite3
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from hockey_model.live_gaps import live_gap_report
from hockey_model.store.db import StoreError, connect
from hockey_model.store.rebuild import rebuild_source
from hockey_model.store.reconcile_game import reconcile_games
from hockey_model.store.upgrade import upgrade_store


def add_store_group(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    """Add the ``store`` command group with its ``upgrade``, ``rebuild``, and ``gaps`` parts."""
    group = add_parser("store", help="manage the store file")
    sub = group.add_subparsers(dest="store_command")
    upgrade = sub.add_parser(
        "upgrade", help="move the store to the current schema, keeping every row"
    )
    _add_db(upgrade, "store file to upgrade; default is HOCKEY_MODEL_DB")
    upgrade.add_argument(
        "--if-missing",
        choices=("fail", "skip"),
        default="fail",
        help="'skip' exits 0 when the store file does not exist; the default 'fail' names it",
    )
    rebuild = sub.add_parser(
        "rebuild",
        help="parse the archived payloads of one source again, with no network request",
    )
    _add_db(rebuild, "store file to rebuild; default is HOCKEY_MODEL_DB")
    rebuild.add_argument(
        "--source", required=True, help="the source to rebuild, such as nhl_boxscores"
    )
    rebuild.add_argument(
        "--parser",
        required=True,
        help="the parser of this code, as name:version; a stale name is refused",
    )
    rebuild.add_argument(
        "--allow-refused",
        action="store_true",
        help="drop a payload that an earlier parser accepted and this parser refuses",
    )
    reconcile = sub.add_parser(
        "reconcile",
        help="build the stints and store the reconciliation status of each stored game; "
        "exit 1 when a game fails or a named game has no inputs",
    )
    _add_db(reconcile, "store file to read and write; default is HOCKEY_MODEL_DB")
    reconcile.add_argument(
        "--game", type=int, default=None, help="one NHL game id; the default is every game"
    )
    gaps = sub.add_parser(
        "gaps", help="list the feeds that are known gaps, as JSON, and say which are too old"
    )
    _add_db(gaps, "store file to read; default is HOCKEY_MODEL_DB")
    gaps.add_argument(
        "--live",
        action="store_true",
        help="list the live feeds: the ESPN goalie and injury feeds and Daily Faceoff",
    )


def _add_db(parser: argparse.ArgumentParser, help_text: str) -> None:
    parser.add_argument("--db", default=None, metavar="PATH", help=help_text)


def _gaps(args: argparse.Namespace, db_path: Path) -> tuple[object, int]:
    if not args.live:
        raise StoreError("store gaps: name the feeds to list; pass --live")
    if not db_path.is_file():
        raise StoreError(
            f"store gaps: there is no store file at {str(db_path)!r}; set HOCKEY_MODEL_DB or "
            "pass --db, and run `hockey-model store upgrade` first"
        )
    conn: sqlite3.Connection = connect(db_path)
    try:
        return live_gap_report(conn, datetime.now(UTC)), 0
    finally:
        conn.close()


def _rebuild(args: argparse.Namespace, db_path: Path) -> tuple[object, int]:
    if not db_path.is_file():
        raise StoreError(
            f"store rebuild: there is no store file at {str(db_path)!r}; set HOCKEY_MODEL_DB or "
            "pass --db, and run `hockey-model store upgrade` first"
        )
    conn: sqlite3.Connection = connect(db_path)
    try:
        return asdict(
            rebuild_source(conn, args.source, args.parser, allow_refused=args.allow_refused)
        ), 0
    finally:
        conn.close()


def _reconcile(args: argparse.Namespace, db_path: Path) -> tuple[object, int]:
    if not db_path.is_file():
        raise StoreError(
            f"store reconcile: there is no store file at {str(db_path)!r}; set HOCKEY_MODEL_DB "
            "or pass --db, and run `hockey-model store upgrade` first"
        )
    conn: sqlite3.Connection = connect(db_path)
    try:
        games = None if args.game is None else [args.game]
        report = reconcile_games(conn, datetime.now(UTC), games)
    finally:
        conn.close()
    named_and_skipped = args.game is not None and bool(report.skipped)
    return asdict(report), 1 if report.failed or named_and_skipped else 0


def run_store_group(args: argparse.Namespace, db_path: Path) -> tuple[object, int]:
    """Run a ``store`` subcommand and return its JSON payload and exit code."""
    if args.store_command == "upgrade":
        return asdict(upgrade_store(db_path, if_missing=args.if_missing)), 0
    if args.store_command == "rebuild":
        return _rebuild(args, db_path)
    if args.store_command == "reconcile":
        return _reconcile(args, db_path)
    if args.store_command == "gaps":
        return _gaps(args, db_path)
    raise StoreError(
        "store: name a subcommand, such as `hockey-model store upgrade`, "
        "`hockey-model store rebuild`, `hockey-model store reconcile`, or "
        "`hockey-model store gaps --live`"
    )
