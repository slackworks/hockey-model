"""The ``store`` command group: ``upgrade`` the file, and list the live feed ``gaps``."""

import argparse
import sqlite3
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from hockey_model.live_gaps import live_gap_report
from hockey_model.store.db import StoreError, connect
from hockey_model.store.upgrade import upgrade_store


def add_store_group(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    """Add the ``store`` command group with its ``upgrade`` and ``gaps`` subcommands."""
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


def run_store_group(args: argparse.Namespace, db_path: Path) -> tuple[object, int]:
    """Run a ``store`` subcommand and return its JSON payload and exit code."""
    if args.store_command == "upgrade":
        return asdict(upgrade_store(db_path, if_missing=args.if_missing)), 0
    if args.store_command == "gaps":
        return _gaps(args, db_path)
    raise StoreError(
        "store: name a subcommand, such as `hockey-model store upgrade` or "
        "`hockey-model store gaps --live`"
    )
