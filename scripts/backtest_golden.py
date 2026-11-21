"""Run the backtest and write the files and the hashes that a speed change must not move.

Usage:
    uv run python3 scripts/backtest_golden.py --db PATH --out DIR [--workers N]

Writes report.json, games.csv, and hashes.json to DIR, and prints hashes.json on stdout.
"exact" holds the SHA-256 of the two files. Two runs on one machine MUST give equal values.
"canonical" rounds every float first, so it also holds across machines; the golden test commits
that form. Run the script before and after a speed change on the real store, and paste both
hashes in the pull request. Design: docs/design/backtest-speed.md
"""

import argparse
import json
import sys
from pathlib import Path

from hockey_model.backtest.golden import write_golden_files
from hockey_model.backtest.runner import run_backtest
from hockey_model.store.db import connect
from hockey_model.store_cli_parsers import CANDIDATE_SETS
from hockey_model.types import HockeyModelError


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Write the backtest files and their hashes.")
    parser.add_argument("--db", type=Path, required=True, help="the store file to run on")
    parser.add_argument("--out", type=Path, required=True, help="the directory for the files")
    parser.add_argument("--workers", type=int, default=1, help="worker processes for the builds")
    args = parser.parse_args(argv)
    if not args.db.is_file():
        print(
            f"backtest_golden: no store file at {args.db}; pass the path of a store that "
            "`hockey-model refresh` wrote",
            file=sys.stderr,
        )
        return 2
    conn = connect(args.db)
    try:
        report, records = run_backtest(conn, candidates=CANDIDATE_SETS["xg"], workers=args.workers)
    except HockeyModelError as error:
        print(f"backtest_golden: {error}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(json.dumps(write_golden_files(report, records, args.out), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
