#!/usr/bin/env python3
# /// script
# requires-python = ">=3.14"
# ///
"""Split the mutation run into shards, and merge the shard results.

Each shard is a group of modules. A CI job runs ``mutmut run`` on the mutants of one
group. The groups are sized from measured run time, not from file count, because
one module can cost many times as much as another.

Every module named in ``[tool.mutmut] only_mutate`` must sit in exactly one shard.
The ``check`` subcommand, and a test, fail when the two lists drift apart.

Subcommands:

- ``patterns --shard N``: print one mutant-name glob per line for shard N.
- ``check``: exit 1 when a covered module is missing from the shards or listed twice.
- ``merge --from DIR --into DIR``: join the shards' ``.meta`` files into one tree.
    Exit 1 when no shard wrote results, or when a module in the shards has no result.

Diagnostics go to stderr. Exit 0 on success, 1 on a failed check or merge, and 2 on a
bad argument (argparse).

Usage: uv run python3 scripts/mutation_shards.py patterns --shard 0
"""

import argparse
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

PYPROJECT = Path("pyproject.toml")
SOURCE_PREFIX = "src/"
PACKAGE = "hockey_model"
# mutmut names every mutant with the package prefix, for example
# hockey_model.sources.nhl.parse__mutmut_3.
MUTANT_PREFIX = f"{PACKAGE}."
# The per-key fields of a .meta file that shards fill in one mutant at a time.
RESULT_FIELDS = ("exit_code_by_key", "durations_by_key", "type_check_error_by_key")
# Balanced from the per-mutant durations that CI records in each .meta file, not from the
# file count. Count is a poor guide: sources.http costs 357s for 190 mutants, and
# experiments.models costs 3902s for 332 (#155). A surviving mutant runs every selected
# test to the end, so the slow modules are the ones whose tests do real work.
#
# Seconds of mutant work per shard, summed from run 42372737510: 3902, 2916, 1718, 1517.
# The four shards of that run sum to 10054s, so a perfect split is 2513s each.
# experiments.models alone is 3902s, so that shard is the floor unless #176 splits a module.
#
# A fit over the two heaviest shards of that run gives ``wall = work / 3.87 + 105``
# seconds: about 3.87 effective workers, and 105s of startup and stats before the first
# mutant. It predicts 24.8 minutes for shard 3, which timed out at its 25-minute limit.
# Applying it to the table below gives 18.6, 14.3, 9.2, and 8.3 minutes for the four
# shards before the backtest modules joined.
#
# The backtest modules joined in #288, from local runs on one machine: runner 618s,
# units 740s, shards 260s, golden 140s. Shard 2 gained units and golden, which brings it to
# about 2600s of work. Shard 3 gained runner and shards, which brings it to about 2400s.
# Read the new work as an estimate until a CI run records the real durations.
#
# The fit under-reads a shard of cheap mutants. Old shard 0 held 190 mutants of
# sources.http, summed to 357s, and took 385s: 1.95 times its prediction. Each mutant is
# a subprocess, so a cheap one spends most of its time in startup that its recorded
# duration does not hold. Shards 2 and 3 below have the same shape, at 1.8s and 1.3s per
# mutant, so read their minutes as a floor and keep the margin at the step limit.
SHARDS: tuple[tuple[str, ...], ...] = (
    ("experiments.models", "json_schema"),
    ("backtest.metrics",),
    ("sources.nhl", "sources.http", "sources.payload", "backtest.units", "backtest.golden"),
    (
        "backtest.runner",
        "backtest.shards",
        "sources.espn",
        "sources.daily_faceoff",
        "sources.moneypuck",
        "game_model.elo",
        "game_model.logistic",
        "sources.errors",
        "sources.policy",
        "sources.shape_errors",
        "sources.starters",
        "sources.teams",
    ),
)


class ShardError(ValueError):
    """A shard request or a shard check is invalid. The message names the fix."""


def shard_patterns(index: int) -> tuple[str, ...]:
    """Return the mutant-name globs for shard ``index``.

    Raise ShardError when the index is outside the shard table.
    """
    if not 0 <= index < len(SHARDS):
        raise ShardError(f"shard {index} does not exist; use 0 to {len(SHARDS) - 1}")
    return tuple(f"{MUTANT_PREFIX}{module}.*" for module in SHARDS[index])


def module_from_source(path: str) -> str:
    """Return the dotted module name that mutmut uses for a source path.

    ``src/hockey_model/sources/nhl.py`` becomes ``sources.nhl``. A mutant key adds
    ``MUTANT_PREFIX`` in front of that name. Raise ShardError for a path outside the
    package, because such a path has no module name to shard.
    """
    relative = path[len(SOURCE_PREFIX) : -len(".py")] if path.startswith(SOURCE_PREFIX) else ""
    if not path.endswith(".py") or not relative:
        raise ShardError(f"{path!r} is not a src/ Python file; fix only_mutate in pyproject.toml")
    parts = relative.split("/")
    if len(parts) < 2 or parts[0] != PACKAGE:
        raise ShardError(f"{path!r} is outside src/{PACKAGE}/; fix only_mutate in pyproject.toml")
    return ".".join(parts[1:])


def covered_modules(pyproject: Path) -> set[str]:
    """Return the modules named by ``[tool.mutmut] only_mutate``.

    A pattern that matches no file raises ShardError, because a stale pattern would
    otherwise drop a module from the check without a sign. A package ``__init__`` is
    skipped, because mutmut generates no mutants for a file with no functions.
    """
    try:
        config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ShardError(f"cannot read {pyproject}: {error}") from error
    patterns = config.get("tool", {}).get("mutmut", {}).get("only_mutate", [])
    root = pyproject.parent
    modules: set[str] = set()
    for pattern in patterns:
        matches = sorted(root.glob(pattern))
        if not matches:
            raise ShardError(f"only_mutate pattern {pattern!r} matches no file; fix pyproject.toml")
        for match in matches:
            if match.name != "__init__.py":
                modules.add(module_from_source(match.relative_to(root).as_posix()))
    return modules


def check_partition(covered: set[str]) -> list[str]:
    """Return one problem per module that is missing from or listed twice in SHARDS."""
    listed: list[str] = [module for shard in SHARDS for module in shard]
    repeated = {module for module in listed if listed.count(module) > 1}
    problems = [f"{m} is covered but in no shard" for m in sorted(covered - set(listed))]
    problems += [f"{m} is in a shard but not covered" for m in sorted(set(listed) - covered)]
    problems += [f"{m} is in more than one shard" for m in sorted(repeated)]
    return problems


def _union_results(relative: Path, field: str, parts: list[dict[str, Any]]) -> dict[str, Any]:
    """Join the per-key results of one field. A key holds a result in at most one shard.

    A shard writes ``null`` for every mutant it did not run, so a null never wins.
    Two different results for one key raise ShardError, because the shards overlap.
    """
    union: dict[str, Any] = {}
    for part in parts:
        for key, value in part.items():
            previous = union.get(key)
            if value is None:
                union.setdefault(key, None)
            elif previous is not None and previous != value:
                raise ShardError(
                    f"{relative} {field}[{key!r}] has two results: {previous} and {value}"
                )
            else:
                union[key] = value
    return union


def _merge_meta_docs(relative: Path, docs: list[dict[str, Any]]) -> dict[str, Any]:
    """Return one ``.meta`` document from the copies that each shard wrote.

    Every shard parses the same source, so the source hashes must match. A mismatch
    means the shards ran different code, and the merge refuses it.
    """
    hashes = [doc.get("hash_by_function_name") for doc in docs]
    if any(found != hashes[0] for found in hashes):
        raise ShardError(f"{relative} has different source hashes across shards; rerun all shards")
    merged = dict(docs[0])
    for field in RESULT_FIELDS:
        merged[field] = _union_results(relative, field, [doc.get(field, {}) for doc in docs])
    return merged


def _read_meta(path: Path) -> dict[str, Any]:
    """Read one shard ``.meta`` file. Raise ShardError with the path and the fix."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ShardError(
            f"cannot read {path}: {getattr(error, 'strerror', None) or error}; "
            "delete that shard's results and rerun the shard"
        ) from error
    if not isinstance(doc, dict):
        raise ShardError(f"{path} must hold a JSON object; rerun the shard that wrote it")
    return doc


def merge_meta(source_dirs: list[Path], dest: Path) -> int:
    """Write one merged ``.meta`` file into ``dest`` for every file any shard wrote.

    Return the number of files written. Raise ShardError when the shards disagree, or
    when no shard wrote a ``.meta`` file.
    """
    copies: dict[Path, list[dict[str, Any]]] = {}
    for source in source_dirs:
        for meta in sorted(source.rglob("*.meta")):
            copies.setdefault(meta.relative_to(source), []).append(_read_meta(meta))
    if not copies:
        raise ShardError("the shard results hold no .meta files; the shards mutated nothing")
    for relative, docs in copies.items():
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        merged = _merge_meta_docs(relative, docs)
        target.write_text(json.dumps(merged, indent=4), encoding="utf-8")
    return len(copies)


def _meta_path(root: Path, module: str) -> Path:
    """Return where mutmut keeps the ``.meta`` file of one dotted module."""
    return root / SOURCE_PREFIX.rstrip("/") / PACKAGE / f"{'/'.join(module.split('.'))}.py.meta"


def missing_results(root: Path) -> list[str]:
    """Return each module in SHARDS that has no merged ``.meta`` file under ``root``."""
    modules = sorted({module for shard in SHARDS for module in shard})
    return [module for module in modules if not _meta_path(root, module).exists()]


def unrun_by_shard(root: Path) -> dict[int, list[str]]:
    """Return the shard index to the modules it owns that carry no result under ``root``.

    A shard that a CI job killed uploads the ``.meta`` files it wrote, which hold a null for
    each mutant it never reached. Those modules look present to ``missing_results``, so the
    shard that ran out of time must be named from the mutant counts instead (#155).
    """
    unrun: dict[int, list[str]] = {}
    for index, modules in enumerate(SHARDS):
        found = [module for module in modules if _mutants_without_result(root, module)]
        if found:
            unrun[index] = found
    return unrun


def _mutants_without_result(root: Path, module: str) -> int:
    """Return how many mutants of ``module`` have no exit code in the merged ``.meta``."""
    path = _meta_path(root, module)
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        return 0
    exits = data.get("exit_code_by_key") if isinstance(data, dict) else None
    if not isinstance(exits, dict):
        return 0
    return sum(1 for value in exits.values() if value is None)


def _run_merge(source_root: Path, dest: Path) -> int:
    """Merge every shard directory under ``source_root`` into ``dest``. Return the exit code."""
    found = source_root.iterdir() if source_root.is_dir() else []
    source_dirs = sorted(p for p in found if p.is_dir())
    if not source_dirs:
        raise ShardError(
            f"no shard results under {source_root}; download the mutation-shard artifacts first"
        )
    dest.mkdir(parents=True, exist_ok=True)
    copied = merge_meta(source_dirs, dest)
    print(f"merged {copied} meta files into {dest}")
    missing = missing_results(dest)
    for module in missing:
        print(f"mutation-shards: no merged results for {module}", file=sys.stderr)
    # A shard killed by its step timeout still uploads its .meta files, with a null for each
    # mutant it never reached. Name that shard in this job's log, so the reader knows which
    # shard job to open.
    for index, modules in unrun_by_shard(dest).items():
        print(
            f"mutation-shards: shard {index} did not finish; modules {', '.join(modules)}",
            file=sys.stderr,
        )
    return 1 if missing else 0


def _parser() -> argparse.ArgumentParser:
    """Build the argument parser for the three subcommands."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    patterns = sub.add_parser("patterns")
    patterns.add_argument("--shard", type=int, required=True)
    sub.add_parser("check")
    merge = sub.add_parser("merge")
    merge.add_argument("--from", dest="source_root", type=Path, required=True)
    merge.add_argument("--into", dest="dest", type=Path, required=True)
    return parser


def _run(args: argparse.Namespace) -> int:
    """Run one parsed subcommand and return its exit code."""
    if args.command == "patterns":
        for pattern in shard_patterns(args.shard):
            print(pattern)
        return 0
    if args.command == "check":
        problems = check_partition(covered_modules(PYPROJECT))
        for problem in problems:
            print(f"mutation-shards: {problem}", file=sys.stderr)
        return 1 if problems else 0
    return _run_merge(args.source_root, args.dest)


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand and return its exit code. Map ShardError to exit 1."""
    args = _parser().parse_args(argv)
    try:
        return _run(args)
    except ShardError as error:
        print(f"mutation-shards: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
