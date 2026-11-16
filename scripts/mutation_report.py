#!/usr/bin/env python3
# /// script
# requires-python = ">=3.14"
# ///
"""Decide the status of a mutation run and build its tracking-issue text.

A run has one of three statuses:

- ``clean``: the survivor count is at or below the baseline.
- ``regression``: the survivor count is above the baseline.
- ``execution_failed``: the stats are missing, malformed, or marked interrupted,
    a shard did not finish, or a mutant did not finish. The outcome counts must sum to the total.

The baseline is provisional. It is the survivor count from the first pass in #31.
Replace it when #31 publishes a machine-readable baseline.

Subcommands:

- ``status``: read the stats and the job results, print one status word. With ``--out``,
    also write the issue body to that file.
- ``find --cap N``: read ``gh issue list --json number,body,author`` JSON on stdin. Print the
    number of the open report, or nothing when none exists. Only a bot-authored issue counts,
    so a user cannot hide a report by copying the marker. Raise when the list reaches N.
- ``title``: print the title for a new report.

Diagnostics go to stderr. Exit 0 for a valid call. Exit 1 for invalid input from gh.
Exit 2 for a bad argument.

Usage: uv run python3 scripts/mutation_report.py status --stats mutants/mutmut-cicd-stats.json \
    --execution success
"""

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

MARKER = "<!-- hockey-model-mutation-report -->"
TITLE = "Mutation report: the weekly run needs a look"
PROVISIONAL_BASELINE = 524
# ``no_tests`` is a finished outcome: the test run reached the mutant and no test covers it.
COUNT_KEYS = ("killed", "survived", "timeout", "no_tests", "total")
STATUS_CLEAN = "clean"
STATUS_REGRESSION = "regression"
STATUS_FAILED = "execution_failed"
# The job results that a run needs. Each must be success for the run to count as a run.
RESULT_KEYS = ("shard", "merge", "export", "download")
# The download runs as a step of the report job, so the body names it as a step.
RESULT_NOUNS = {"download": "step"}
# ``unknown`` is the value the shell passes for a job that did not report a result.
RESULT_VALUES = ("success", "failure", "cancelled", "skipped", "unknown")


class MalformedStats(ValueError):
    """The stats file is missing, unreadable, not a JSON object, or has a bad count."""


@dataclass(frozen=True)
class Stats:
    """Survivor and outcome counts from ``mutmut export-cicd-stats``."""

    counts: dict[str, int]
    interrupted: bool


def _reason(error: Exception) -> str:
    """Return the OS reason when there is one, or the error text otherwise."""
    return getattr(error, "strerror", None) or str(error)


def read_stats(path: Path) -> Stats:
    """Read the stats file. Raise MalformedStats with the path and the bad field.

    A missing file, a file that is not UTF-8 or not JSON, and a count that is not
    a whole number all raise. A raise means the run did not finish, so the caller
    reports a failure.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise MalformedStats(f"cannot read {path}: {_reason(error)}") from error
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise MalformedStats(f"{path} is not JSON: {error.msg} at line {error.lineno}") from error
    if not isinstance(data, dict):
        raise MalformedStats(f"{path} must hold a JSON object, got {type(data).__name__}")
    counts: dict[str, int] = {}
    for key in COUNT_KEYS:
        value = data.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise MalformedStats(f"{path} field {key!r} must be a count, got {value!r}")
        counts[key] = value
    return Stats(counts=counts, interrupted=data.get("check_was_interrupted_by_user") is True)


def unfinished(counts: dict[str, int]) -> int:
    """Return how many mutants the outcome counts do not account for."""
    finished = counts["killed"] + counts["survived"] + counts["timeout"] + counts["no_tests"]
    return counts["total"] - finished


def execution_of(results: dict[str, str], stats: Stats | None) -> str:
    """Return ``success`` only when every job result is success and the stats exist."""
    if any(results[key] != "success" for key in RESULT_KEYS):
        return "failure"
    return "success" if stats is not None else "failure"


def decide(stats: Stats | None, execution: str, baseline: int) -> str:
    """Return the status for one run.

    An interrupted run, a failed execution, or a mutant that did not finish is never clean,
    even when its survivor count looks fine, because the counts may be partial.

    mutmut's total also holds the statuses that the stats file leaves out: not checked, and
    caught by type check (type-checker mode is off in this repo). Any mutant outside the
    finished outcomes fails the run, so a status added later cannot pass silently.
    """
    if execution != "success" or stats is None or stats.interrupted:
        return STATUS_FAILED
    if unfinished(stats.counts) != 0:
        return STATUS_FAILED
    if stats.counts["survived"] > baseline:
        return STATUS_REGRESSION
    return STATUS_CLEAN


def stats_causes(stats: Stats | None) -> list[str]:
    """Return the causes that the stats file states about the run."""
    if stats is None:
        return ["the run did not write the stats file"]
    found: list[str] = []
    if stats.interrupted:
        found.append("the run was interrupted")
    short = unfinished(stats.counts)
    if short > 0:
        found.append(f"{short} mutant(s) did not finish")
    elif short < 0:
        found.append(f"the outcome counts exceed the total by {-short}")
    return found


def result_cause(key: str, outcome: str) -> str:
    """Return the cause for a run part that did not finish with success."""
    noun = RESULT_NOUNS.get(key, "job")
    return f"the {key} {noun} did not finish ({outcome})"


def causes(
    stats: Stats | None, status: str, given: Sequence[str], results: dict[str, str]
) -> list[str]:
    """Return every reason the run does not count as clean, most specific first.

    A caller reason and a job result can each name a cause that the counts do not show, so a
    failed run never reads as a clean table. A clean or regressed run returns no cause.
    """
    if status == STATUS_CLEAN:
        return []
    found = stats_causes(stats)
    for key in RESULT_KEYS:
        if results[key] != "success":
            found.append(result_cause(key, results[key]))
    found.extend(given)
    if status == STATUS_FAILED and not found:
        found.append("the run did not finish")
    return found


def build_body(
    status: str,
    stats: Stats | None,
    baseline: int,
    run_url: str,
    reasons: Sequence[str] = (),
) -> str:
    """Return the Markdown body for the tracking issue.

    The body starts with the marker, so a later run finds this issue again. When there are
    valid counts they are a table, and any cause sits under the table, so a partial run never
    shows as a clean one.
    """
    lines = [MARKER, "", f"Status: `{status}`", f"Run: {run_url}", ""]
    if stats is not None:
        counts = stats.counts
        difference = counts["survived"] - baseline
        lines += [
            "| Killed | Survived | Timed out | Total | Baseline | Difference |",
            "| --- | --- | --- | --- | --- | --- |",
            f"| {counts['killed']} | {counts['survived']} | {counts['timeout']} | "
            f"{counts['total']} | {baseline} | {difference:+d} |",
        ]
    if reasons:
        lines += ["", "The run reported these causes:", ""]
        lines += [f"- {reason}" for reason in reasons]
    return "\n".join(lines) + "\n"


def _is_bot(author: object) -> bool:
    """Return True when gh reports the author as a bot account."""
    return isinstance(author, dict) and author.get("is_bot") is True


def find_open_report(issues_json: str, cap: int | None = None) -> int | None:
    """Return the number of the first open bot-authored issue that carries the marker.

    Raise ValueError when the input is not a JSON list of issue objects, or when an
    entry has a number that is not an integer, or a body that is not text. When ``cap`` is
    given and the list holds that many or more, raise: an entry past the gh limit is missing
    from the list, so a marked report could be invisible and a second one would open.
    """
    issues = json.loads(issues_json)
    if not isinstance(issues, list):
        raise ValueError("gh issue list must print a JSON array; check the gh version, then rerun")
    if cap is not None and len(issues) >= cap:
        raise ValueError(
            f"the list holds {len(issues)} open issues, which reaches the {cap} limit; "
            "an open report past the limit cannot be found. Close some issues, then rerun"
        )
    for issue in issues:
        number = issue.get("number") if isinstance(issue, dict) else None
        if not isinstance(number, int) or isinstance(number, bool):
            raise ValueError(f"issue entry needs an integer number, got {issue!r}")
        body = issue.get("body") or ""
        if not isinstance(body, str):
            raise ValueError(f"issue #{number} body must be text, got {type(body).__name__}")
        if MARKER in body and _is_bot(issue.get("author")):
            return number
    return None


def _load_stats(path: Path) -> tuple[Stats | None, str | None]:
    """Read the stats. On failure, print the reason to stderr and return it with None."""
    try:
        return read_stats(path), None
    except MalformedStats as error:
        print(f"mutation-report: {error}", file=sys.stderr)
        return None, str(error)


def _find(cap: int | None) -> int:
    """Print the open report number from the gh JSON on stdin. Return the exit code."""
    try:
        number = find_open_report(sys.stdin.read(), cap)
    except ValueError as error:
        print(f"mutation-report: {error}", file=sys.stderr)
        return 1
    if number is not None:
        print(number)
    return 0


def _parser() -> argparse.ArgumentParser:
    """Build the argument parser for the three subcommands."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    status.add_argument("--stats", type=Path, required=True)
    status.add_argument("--execution", choices=("success", "failure"), default=None)
    for key in RESULT_KEYS:
        status.add_argument(f"--{key}-result", choices=RESULT_VALUES, default=None)
    status.add_argument("--out", type=Path)
    status.add_argument("--run-url")
    status.add_argument(
        "--reason",
        action="append",
        default=[],
        help="a cause the counts do not show; repeat it for each cause",
    )
    find = sub.add_parser("find")
    find.add_argument("--cap", type=int, default=None, help="the gh list limit")
    sub.add_parser("title")
    return parser


def _status(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Print the status word, and write the body when ``--out`` names a file."""
    results = {key: getattr(args, f"{key}_result") for key in RESULT_KEYS}
    given_results = {key: value for key, value in results.items() if value is not None}
    if given_results and len(given_results) != len(RESULT_KEYS):
        parser.error("give every job result flag, or none of them")
    stats, read_reason = _load_stats(args.stats)
    if given_results:
        execution = execution_of(given_results, stats)
    elif args.execution is not None:
        execution = args.execution
    else:
        parser.error("give --execution, or the four job result flags")
    status = decide(stats, execution, PROVISIONAL_BASELINE)
    # A malformed stats file names itself, so the body says what stopped the run.
    given = list(args.reason) + ([read_reason] if read_reason is not None else [])
    if args.out is not None:
        body = build_body(
            status,
            stats,
            PROVISIONAL_BASELINE,
            args.run_url,
            causes(stats, status, given, given_results or {k: "success" for k in RESULT_KEYS}),
        )
        args.out.write_text(body, encoding="utf-8")
    print(status)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand and return its exit code."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "title":
        print(TITLE)
        return 0
    if args.command == "find":
        return _find(args.cap)
    if args.out is not None and args.run_url is None:
        parser.error("--out needs --run-url for the run link in the body")
    return _status(args, parser)


if __name__ == "__main__":
    sys.exit(main())
