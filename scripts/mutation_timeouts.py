#!/usr/bin/env python3
# /// script
# requires-python = ">=3.14"
# ///
"""List the mutants that timed out, each with its estimated test time and its CPU limit (#274).

mutmut stops a mutant at a CPU limit of ``ceil((estimated + timeout_constant) *
timeout_multiplier * 2)`` seconds, where ``estimated`` is the sum of the per-test durations of
the tests that reach the mutant. Each run measures those durations again, so the limit moves
from run to run. The list lets a reader tell a mutant that hangs from a mutant that sits near
a limit that moved. The ``.meta`` files that mutmut writes hold the exit code and the
estimated time of each mutant.

Usage: uv run python3 scripts/mutation_timeouts.py --out mutants/mutmut-timeouts.json \
    [--mutants mutants] [--pyproject pyproject.toml]

The output file is JSON with one key, ``timeouts``: a list of ``{"mutant", "estimated_s",
"limit_s"}`` sorted by mutant name. Diagnostics go to
stderr. Exit 0 on success, 1 for input that cannot be read, 2 for a bad argument.
"""

import argparse
import json
import math
import sys
import tomllib
from pathlib import Path

# The exit codes that mutmut counts as a timeout. A test compares them with mutmut's own table.
TIMEOUT_EXIT_CODES = frozenset({36, 24, -24, 152, 255})
# The defaults of mutmut when [tool.mutmut] names no value.
DEFAULT_MULTIPLIER = 15.0
DEFAULT_CONSTANT = 1.0


class InputError(ValueError):
    """A file that the list needs is missing, unreadable, or has the wrong shape."""


def read_limits(pyproject: Path) -> tuple[float, float]:
    """Return ``(timeout_multiplier, timeout_constant)`` from ``[tool.mutmut]``."""
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise InputError(f"cannot read {pyproject}: {error}") from error
    table = data.get("tool", {}).get("mutmut", {})
    values = []
    for key, default in (
        ("timeout_multiplier", DEFAULT_MULTIPLIER),
        ("timeout_constant", DEFAULT_CONSTANT),
    ):
        value = table.get(key, default)
        number = isinstance(value, int | float) and not isinstance(value, bool)
        if not number or not math.isfinite(value) or value < 0:
            raise InputError(
                f"{pyproject}: [tool.mutmut] {key} must be a finite number of 0 or more"
            )
        values.append(float(value))
    return values[0], values[1]


def cpu_limit(estimated: float, multiplier: float, constant: float) -> int:
    """Return the CPU limit in seconds that mutmut sets for a mutant."""
    return math.ceil((estimated + constant) * multiplier * 2)


def _read_meta(path: Path) -> tuple[dict[str, object], dict[str, object]]:
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
        codes = meta["exit_code_by_key"]
        estimated = meta.get("estimated_durations_by_key", {})
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise InputError(f"cannot read the mutmut meta file {path}: {error!r}") from error
    if not isinstance(codes, dict) or not isinstance(estimated, dict):
        raise InputError(f"the mutmut meta file {path} has no object for its results")
    return codes, estimated


def collect(mutants: Path, multiplier: float, constant: float) -> list[dict[str, object]]:
    """Return the timed-out mutants under ``mutants``, sorted by name."""
    if not mutants.is_dir():
        raise InputError(f"{mutants} is not a directory; run mutmut first or pass --mutants")
    found: list[dict[str, object]] = []
    for path in sorted(mutants.rglob("*.meta")):
        codes, estimated = _read_meta(path)
        for name, code in codes.items():
            if not isinstance(code, int) or code not in TIMEOUT_EXIT_CODES:
                continue
            seconds = estimated.get(name)
            valid = isinstance(seconds, int | float) and not isinstance(seconds, bool)
            if not valid or not math.isfinite(seconds) or seconds < 0:
                raise InputError(
                    f"{path}: the timed-out mutant {name} has no estimated time; "
                    "run mutmut again so the stats are written"
                )
            found.append(
                {
                    "mutant": name,
                    "estimated_s": round(float(seconds), 3),
                    "limit_s": cpu_limit(float(seconds), multiplier, constant),
                }
            )
    return sorted(found, key=lambda item: str(item["mutant"]))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--mutants", type=Path, default=Path("mutants"))
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Write the list of timed-out mutants and return the exit code."""
    args = _parser().parse_args(argv)
    try:
        multiplier, constant = read_limits(args.pyproject)
        timeouts = collect(args.mutants, multiplier, constant)
    except InputError as error:
        print(f"mutation-timeouts: {error}", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps({"timeouts": timeouts}, indent=2) + "\n", encoding="utf-8")
    print(f"mutation-timeouts: {len(timeouts)} timed-out mutant(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
