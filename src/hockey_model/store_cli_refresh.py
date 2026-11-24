"""The refresh command of the store CLI.

The TLA+ model of the daily prepare covers the exit code that this command returns, so the
model shards name this module and not the whole store CLI (specs/shards.json).
"""

import argparse
import os
from datetime import UTC, datetime, timedelta

from hockey_model.sources.http import HttpClient
from hockey_model.store.db import connect, db_path_from_env
from hockey_model.store.refresh import RefreshReport, refresh, refresh_starters
from hockey_model.types import HockeyModelError


def _report_json(report: RefreshReport) -> dict[str, object]:
    return {
        "written": report.written,
        "errors": [{"type": type(e).__name__, "message": str(e)} for e in report.errors],
        "notes": list(report.notes),
        "credits": list(report.credits),
        "backfill_complete": report.backfill_complete,
        "timed_out": report.timed_out,
    }


def refresh_command(args: argparse.Namespace) -> tuple[object, int]:
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
                conn,
                HttpClient(),
                now=datetime.now(UTC),
                clock=lambda: datetime.now(UTC),
                retry_skipped=args.retry_skipped,
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
