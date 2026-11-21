"""The ``experiments`` subcommands of the CLI: ``check`` and ``run``."""

import argparse
import os
import sys

from hockey_model.experiments.check import check_all, run_experiment
from hockey_model.store.db import connect, db_path_from_env
from hockey_model.types import HockeyModelError


def run_experiments_command(args: argparse.Namespace) -> tuple[object, int]:
    """Run one ``experiments`` subcommand. Return the JSON payload and the exit code."""
    command = getattr(args, "experiment_command", None)
    if command == "check":
        result = check_all()
        # A note says that a check did not run. The exit code stays 0, so the note also goes
        # to standard error, where a person or a log reads it.
        for note in result["notes"]:
            print(f"experiments check: {note}", file=sys.stderr)
        return result, 0 if result["ok"] else 1
    if command == "run":
        conn = connect(db_path_from_env(os.environ))
        try:
            result, _ = run_experiment(args.record_id, conn, args.games_out, workers=args.workers)
            return result, 0
        finally:
            conn.close()
    raise HockeyModelError("experiments: specify check or run")
