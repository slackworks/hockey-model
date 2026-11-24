#!/usr/bin/env python3
# /// script
# requires-python = ">=3.14"
# ///
"""Read the TLC shard map, and pick the shards that a change reaches.

The map is specs/shards.json. Each shard runs one spec with one config. The script
scripts/tlc.sh and the workflow .github/workflows/tla.yml both read the map through this
script, so they cannot disagree about which paths start which shard.

Subcommands:

- ``list``: print one JSON object per line for each shard, with these fields:
    ``name``, ``spec``, ``config``, and ``paths``, the patterns that start the shard.
- ``rows [--shard NAME]``: print one line for each shard, or for one shard, with the name,
    the spec, and the config, separated by tabs. Exit 2 when NAME is not a shard.
- ``plan [--all]``: read changed paths from stdin, one per line, and print the JSON list of
    the shards to run. A path that matches an ``always`` pattern selects every shard, and a
    path of a spec or a config selects the shards that run it. ``--all``
    selects every shard without reading stdin.

A pattern that ends in ``/**`` matches every path under that directory. Any other pattern is
a shell glob on the whole path.

Example: ``printf 'specs/a.cfg\\n' | python3 scripts/tlc_shards.py plan`` prints
``["stall", "blocked", "cutoff", "recent", "main"]``.

Diagnostics go to stderr. Exit 0 on success, 1 on a bad map, and 2 on a bad argument.

Usage: python3 scripts/tlc_shards.py plan < changed-paths.txt
"""

import argparse
import fnmatch
import json
import sys
from pathlib import Path
from typing import Any

MAP = Path(__file__).resolve().parents[1] / "specs" / "shards.json"


def _strings(value: object, where: str, *, empty_ok: bool = False) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{where} must be a list of non-empty strings")
    if not value and not empty_ok:
        raise ValueError(f"{where} must be a list with one string or more")
    return value


def _check_shard(path: Path, shard: object, names: set[str]) -> None:
    if not isinstance(shard, dict):
        raise ValueError(f"{path}: each shard must be an object")
    for key in ("name", "spec", "config"):
        if not isinstance(shard.get(key), str) or not shard[key]:
            raise ValueError(f"{path}: a shard needs a non-empty {key}")
    if shard["name"] in names:
        raise ValueError(f"{path}: the shard {shard['name']} is listed twice")
    names.add(shard["name"])
    _strings(shard.get("paths", []), f"{path}: paths of {shard['name']}", empty_ok=True)
    if "covers" in shard:
        _strings(shard["covers"], f"{path}: covers of {shard['name']}", empty_ok=True)


def load_map(path: Path = MAP) -> dict[str, Any]:
    """Return the shard map, or raise ``ValueError`` that names the file and the fault."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{path}: cannot read the shard map: {error}") from error
    if not isinstance(data, dict):
        raise ValueError(f"{path}: the shard map must be an object")
    for key in ("always", "covers"):
        _strings(data.get(key), f"{path}: {key}")
    shards = data.get("shards")
    if not isinstance(shards, list) or not shards:
        raise ValueError(f"{path}: shards must be a list with one shard or more")
    names: set[str] = set()
    for shard in shards:
        _check_shard(path, shard, names)
    return data


def matches(path: str, patterns: list[str]) -> bool:
    """Return True when ``path`` matches one of ``patterns``.

    The fnmatch ``*`` also matches ``/``, so ``dir/**`` matches every path under ``dir/``.
    """
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def shard_paths(data: dict[str, Any], shard: dict[str, Any]) -> list[str]:
    """Return the patterns that start ``shard``.

    These are the always paths, the covers of the shard or the shared covers when it names none,
    its own paths, and its spec and config files. A change of a spec starts the shards of that
    spec only.
    """
    covers = shard["covers"] if "covers" in shard else data["covers"]
    return [*data["always"], *covers, shard["spec"], shard["config"], *shard.get("paths", [])]


def plan(data: dict[str, Any], changed: list[str]) -> list[str]:
    """Return the names of the shards that a change of ``changed`` reaches, in run order.

    Every shard lists the ``always`` paths, so a change of one starts every shard.
    """
    return [
        shard["name"]
        for shard in data["shards"]
        if any(matches(path, shard_paths(data, shard)) for path in changed)
    ]


def _find(data: dict[str, Any], name: str) -> dict[str, Any]:
    for shard in data["shards"]:
        if shard["name"] == name:
            return shard
    known = ", ".join(shard["name"] for shard in data["shards"])
    print(f"tlc_shards: {name!r} is not a shard; the shards are: {known}", file=sys.stderr)
    raise SystemExit(2)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read the TLC shard map, and pick the shards that a change reaches."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="print each shard as one JSON object per line")
    rows = commands.add_parser("rows", help="print the name, spec, and config of each shard")
    rows.add_argument("--shard", help="print this shard only")
    planned = commands.add_parser("plan", help="print the shards that the changed paths reach")
    planned.add_argument("--all", action="store_true", help="select every shard")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand. Return the exit code."""
    args = _parser().parse_args(argv)
    try:
        data = load_map()
    except ValueError as error:
        print(f"tlc_shards: {error}", file=sys.stderr)
        return 1
    if args.command == "list":
        for shard in data["shards"]:
            row = {k: shard[k] for k in ("name", "spec", "config")}
            print(json.dumps({**row, "paths": shard_paths(data, shard)}))
    elif args.command == "rows":
        chosen = [_find(data, args.shard)] if args.shard else data["shards"]
        for shard in chosen:
            print(f"{shard['name']}\t{shard['spec']}\t{shard['config']}")
    else:
        names = [s["name"] for s in data["shards"]]
        if not args.all:
            changed = [line.strip() for line in sys.stdin if line.strip()]
            names = plan(data, changed)
        print(json.dumps(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
