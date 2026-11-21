"""JSON command-line wrapper over the public Python API."""

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Never

from hockey_model import progress
from hockey_model.identity import Catalog
from hockey_model.matchup import matchup, playoff_probability
from hockey_model.models import JointModel, MatchupModel, StandingsModel, StatModel
from hockey_model.projections import project_player, project_team
from hockey_model.store.db import os_error_fix, sqlite_failure_fix
from hockey_model.store_cli import STORE_COMMANDS, run_store_command
from hockey_model.store_cli_parsers import add_store_commands
from hockey_model.types import (
    AmbiguousPlayerError,
    HockeyModelError,
    ModelNotConfiguredError,
    Player,
    Roster,
    Slot,
    Standing,
    UnknownPlayerError,
)


@dataclass(frozen=True, slots=True)
class CliRuntime:
    """Models and catalog supplied by an embedding application to the CLI."""

    catalog: Catalog
    stat_model: StatModel
    joint_model: JointModel
    matchup_model: MatchupModel
    standings_model: StandingsModel
    standings: tuple[Standing, ...]


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        self.print_usage(sys.stderr)
        raise ValueError(message)


def _add_projection_options(parser: argparse.ArgumentParser, *, horizon: bool) -> None:
    if horizon:
        parser.add_argument("--horizon-games", type=int, default=1)
    parser.add_argument("--level", type=float, default=0.8)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="hockey-model", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    player = commands.add_parser("player", help="project one player statistic")
    player.add_argument("stat")
    player.add_argument("ref")
    _add_projection_options(player, horizon=True)

    team = commands.add_parser("team", help="project one roster statistic")
    team.add_argument("stat")
    team.add_argument("roster_file", type=Path)
    _add_projection_options(team, horizon=True)

    game = commands.add_parser("matchup", help="project a matchup between roster files")
    game.add_argument("home_file", type=Path)
    game.add_argument("away_file", type=Path)
    _add_projection_options(game, horizon=False)

    playoffs = commands.add_parser("playoffs", help="project a team's playoff probability")
    playoffs.add_argument("team_code")
    _add_projection_options(playoffs, horizon=False)
    add_store_commands(commands.add_parser)
    return parser


def _required_string(data: dict[str, object], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def _optional_string(data: dict[str, object], field: str) -> str | None:
    value = data.get(field)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    return value


def _optional_int(data: dict[str, object], field: str) -> int | None:
    value = data.get(field)
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise ValueError(f"{field} must be an integer or null")
    return value


def _player(data: object) -> Player:
    if not isinstance(data, dict):
        raise ValueError("each roster player must be a JSON object")
    raw_eligible = data.get("eligible")
    if not isinstance(raw_eligible, list) or not all(isinstance(v, str) for v in raw_eligible):
        who = data.get("canonical_id")
        raise ValueError(
            f"player {who!r}: eligible must be a list of slot strings, got {raw_eligible!r}"
        )
    eligible = frozenset(Slot(value) for value in raw_eligible)
    return Player(
        canonical_id=_required_string(data, "canonical_id"),
        yahoo_id=_optional_string(data, "yahoo_id"),
        nhl_id=_optional_int(data, "nhl_id"),
        full_name=_required_string(data, "full_name"),
        eligible=eligible,
        team_code=_optional_string(data, "team_code"),
    )


def _roster(path: Path) -> Roster:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"read roster {path}: {exc}; provide a readable JSON roster") from exc
    if not isinstance(data, dict):
        raise ValueError(f"read roster {path}: root must be a JSON object")
    try:
        raw_players = data.get("players")
        if not isinstance(raw_players, list):
            raise ValueError("players must be an array")
        players = tuple(_player(item) for item in raw_players)
        raw_slots = data.get("slots")
        if raw_slots is not None and not isinstance(raw_slots, dict):
            raise ValueError("slots must be an object or null")
        slots = (
            None if raw_slots is None else {key: Slot(value) for key, value in raw_slots.items()}
        )
        return Roster(_required_string(data, "name"), players, slots)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"read roster {path}: invalid roster fields: {exc}") from exc


def _resolve_player(value: str, catalog: Catalog) -> Player:
    if not value:
        raise ValueError("player reference must not be empty")
    try:
        return catalog.resolve(value)
    except UnknownPlayerError:
        if value.isascii() and value.isdigit():
            return catalog.resolve(int(value))
        raise
    except AmbiguousPlayerError:
        if value.isascii() and value.isdigit():
            return catalog.resolve(int(value))
        raise


def _run(args: argparse.Namespace, runtime: CliRuntime) -> object:
    if args.command == "player":
        player = _resolve_player(args.ref, runtime.catalog)
        return project_player(
            player,
            args.stat,
            runtime.stat_model,
            horizon_games=args.horizon_games,
            level=args.level,
        )
    if args.command == "team":
        return project_team(
            _roster(args.roster_file),
            args.stat,
            runtime.stat_model,
            runtime.joint_model,
            horizon_games=args.horizon_games,
            level=args.level,
        )
    if args.command == "matchup":
        return matchup(
            _roster(args.home_file),
            _roster(args.away_file),
            runtime.matchup_model,
            args.level,
        )
    return playoff_probability(
        args.team_code, runtime.standings, runtime.standings_model, args.level
    )


def _json_default(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def main(argv: Sequence[str] | None = None, runtime: CliRuntime | None = None) -> int:
    """Run the JSON CLI, returning zero on success and two for caller errors.

    ``refresh`` returns one when a source failed, after it prints the report.
    """
    try:
        args = _parser().parse_args(argv)
        if args.command in STORE_COMMANDS:
            # The lines go to stderr, so the JSON on stdout stays valid.
            with progress.to_stderr(sys.stderr):
                payload, code = run_store_command(args)
            print(json.dumps(payload, default=_json_default, sort_keys=True))
            return code
        if runtime is None:
            raise ModelNotConfiguredError(
                "run CLI command: no models are configured; "
                "embed CliRuntime with data-backed models"
            )
        # Run the command outside the encoder's try block, so a TypeError from a model is an
        # unexpected failure and only a JSON encoder TypeError is a serialization failure.
        payload = _run(args, runtime)
        try:
            text = json.dumps(payload, default=_json_default, sort_keys=True)
        except TypeError as exc:
            print(f"error: serialization failed: {exc}", file=sys.stderr)
            return 2
        print(text)
    except (HockeyModelError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        # The store layer names every cause it knows. One that escapes it needs its own cause.
        print(f"error: {sqlite_failure_fix(exc)}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {os_error_fix(exc)}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001  # the last guard turns any bug into exit 2
        print(f"error: unexpected failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0
