"""Build the feature rows of a backtest one season at a time, in worker processes.

A full build replays the Elo from the first game, so it runs in one process for close to an
hour on the real store. A season shard starts from the ratings before its first game, which one
cheap pass over the results gives (``season_boundaries``), and reads the same ratings as the
replay. The shards of all seasons and of both goalie modes then run side by side, and joining
them in season order gives the rows of the full build. Design: docs/design/backtest-speed.md
"""

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from hockey_model.backtest.units import (
    Unit,
    check_workers,
    database_path,
    open_read_only,
    run_units,
)
from hockey_model.game_model.elo import season_boundaries
from hockey_model.game_model.features import GoalieMode
from hockey_model.game_model.seasons import ELO_BURN_IN, SeasonPlan
from hockey_model.game_model.training import TrainingRow, training_rows
from hockey_model.sources.nhl import GameRow
from hockey_model.store.reads import LATEST_GAME, final_games_before
from hockey_model.types import HockeyModelError

_MODES = (GoalieMode.GUESS, GoalieMode.ACTUAL)


@dataclass(frozen=True, slots=True)
class Shard:
    """One season of one goalie mode, with the Elo ratings before its first game."""

    season: int
    mode: GoalieMode
    burn_in: int
    names: tuple[str, ...]
    elo_start: Mapping[str, float]
    # The number of final games that the parent read, so a worker can see a store that changed.
    final_games: int


def build_season_rows(path: Path, shard: Shard) -> list[TrainingRow]:
    """Build the rows of one shard in a worker, on its own read-only connection."""
    conn = open_read_only(path)
    try:
        now = len(_final_games(conn, shard.burn_in))
        if now != shard.final_games:
            raise HockeyModelError(
                f"backtest: the store changed during the run: the parent read "
                f"{shard.final_games} final games and a worker read {now}; do not refresh "
                "the store while a backtest or a fit runs, then run it again"
            )
        return training_rows(
            conn,
            shard.season,
            shard.season,
            shard.mode,
            shard.burn_in,
            names=shard.names,
            elo_start=shard.elo_start,
        )
    finally:
        conn.close()


def _final_games(conn: sqlite3.Connection, burn_in: int) -> list[GameRow]:
    return [g for g in final_games_before(conn, LATEST_GAME) if g.season >= burn_in]


def _build(
    conn: sqlite3.Connection,
    seasons_range: tuple[int, int],
    burn_in: int,
    names: Sequence[str],
    modes: Sequence[GoalieMode],
    workers: int,
) -> dict[GoalieMode, list[TrainingRow]]:
    """Build the rows of each mode, one season at a time, and join them in season order."""
    path = database_path(conn)
    games = _final_games(conn, burn_in)
    boundaries = season_boundaries(games)
    seasons = sorted(s for s in boundaries if seasons_range[0] <= s <= seasons_range[1])
    keys = [(mode, season) for mode in modes for season in seasons]
    units = [
        Unit(
            f"build {mode.value} rows {season}",
            build_season_rows,
            (path, Shard(season, mode, burn_in, tuple(names), boundaries[season], len(games))),
        )
        for mode, season in keys
    ]
    parts = dict(zip(keys, run_units(units, workers, "build-rows"), strict=True))
    return {mode: [row for season in seasons for row in parts[(mode, season)]] for mode in modes}


def build_sharded(
    conn: sqlite3.Connection, plan: SeasonPlan, names: Sequence[str], workers: int
) -> tuple[list[TrainingRow], list[TrainingRow]]:
    """Return the actual rows and the guess rows from the first fit season to the last scored.

    The result is the same as two full builds, and it is empty when the store holds no game
    in that range. The guess shards come first in the queue, because the guess build is the
    longer one.
    """
    rows = _build(conn, (plan.fit_first, plan.scored[1]), plan.burn_in, names, _MODES, workers)
    return rows[GoalieMode.ACTUAL], rows[GoalieMode.GUESS]


def actual_rows(
    conn: sqlite3.Connection,
    first: int,
    last: int,
    names: Sequence[str],
    workers: int,
    burn_in: int = ELO_BURN_IN,
) -> list[TrainingRow]:
    """Return the rows of the games from ``first`` through ``last`` with the goalie who started.

    One worker runs the full build. More workers build one season at a time, and the rows are
    the same. The fit command uses this.
    """
    if check_workers(workers) == 1:
        return training_rows(conn, first, last, GoalieMode.ACTUAL, burn_in, names=names)
    return _build(conn, (first, last), burn_in, names, (GoalieMode.ACTUAL,), workers)[
        GoalieMode.ACTUAL
    ]
