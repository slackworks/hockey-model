"""Run the backtest and write the files and the hashes that a speed change must not move.

Usage:
    uv run python3 scripts/backtest_golden.py --db PATH --out DIR [--workers N]

Writes report.json, games.csv, and hashes.json to DIR, and prints hashes.json on stdout.
The exact_* hashes are the SHA-256 of the two files. Two runs on one machine MUST give equal
values. The canonical_* hashes round every float first, so they also hold across machines; the
golden test commits that form. Run the script before and after a speed change on the real store,
and paste both forms in the pull request. The script opens the store for reading only,
and the season row cache is written to the directory beside the store file.

Exit codes: 0 done; 1 the backtest or a write failed (the message names the cause); 2 the store
file is missing or an argument is wrong.
Design: docs/design/backtest-speed.md
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from hockey_model.backtest.golden import write_golden_files
from hockey_model.backtest.runner import run_backtest
from hockey_model.backtest.units import open_read_only
from hockey_model.game_model.features import FEATURES
from hockey_model.store.db import sqlite_failure_fix
from hockey_model.types import HockeyModelError


def _workers(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, got {value}")
    return value


def _prepare_out(out: Path) -> str | None:
    """Create ``out`` before the long run, so a bad path fails at once. Return the error."""
    try:
        out.mkdir(parents=True, exist_ok=True)
        probe = out / ".write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as error:
        return f"cannot write to {out}: {error}; pass a directory that you can write to"
    return None


def _run(args: argparse.Namespace) -> int:
    """Run the backtest on a read-only connection and write the files. Return the exit code."""
    try:
        conn = open_read_only(args.db)
        try:
            report, records = run_backtest(conn, candidates=FEATURES, workers=args.workers)
        finally:
            conn.close()
    except HockeyModelError as error:
        print(f"backtest_golden: {error}", file=sys.stderr)
        return 1
    except sqlite3.Error as error:
        print(f"backtest_golden: {error}; {sqlite_failure_fix(error)}", file=sys.stderr)
        return 1
    except OSError as error:
        # A worker pool can run out of open files, and a code file can be unreadable.
        print(
            f"backtest_golden: the backtest failed: {error}; fix the cause, then run again",
            file=sys.stderr,
        )
        return 1
    try:
        hashes = write_golden_files(report, records, args.out)
    except OSError as error:
        print(
            f"backtest_golden: cannot write the files to {args.out}: {error}; "
            "pass a directory that you can write to",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(hashes, indent=2))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Write the backtest files and their hashes.")
    parser.add_argument("--db", type=Path, required=True, help="the store file to run on")
    parser.add_argument("--out", type=Path, required=True, help="the directory for the files")
    parser.add_argument("--workers", type=_workers, default=1, help="processes for the builds")
    args = parser.parse_args(argv)
    if not args.db.is_file():
        print(
            f"backtest_golden: no store file at {args.db}; pass the path of a store that "
            "`hockey-model refresh` wrote",
            file=sys.stderr,
        )
        return 2
    problem = _prepare_out(args.out)
    if problem is not None:
        print(f"backtest_golden: {problem}", file=sys.stderr)
        return 1
    return _run(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
