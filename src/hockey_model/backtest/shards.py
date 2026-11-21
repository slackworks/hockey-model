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

from hockey_model.backtest.units import Unit, database_path, open_read_only, run_units
from hockey_model.game_model.elo import season_boundaries
from hockey_model.game_model.features import GoalieMode
from hockey_model.game_model.seasons import SeasonPlan
from hockey_model.game_model.training import TrainingRow, training_rows
from hockey_model.store.reads import LATEST_GAME, final_games_before

_MODES = (GoalieMode.GUESS, GoalieMode.ACTUAL)


@dataclass(frozen=True, slots=True)
class Shard:
    """One season of one goalie mode, with the Elo ratings before its first game."""

    season: int
    mode: GoalieMode
    burn_in: int
    names: tuple[str, ...]
    elo_start: Mapping[str, float]


def build_season_rows(path: Path, shard: Shard) -> list[TrainingRow]:
    """Build the rows of one shard in a worker, on its own read-only connection."""
    conn = open_read_only(path)
    try:
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


def _boundaries(conn: sqlite3.Connection, burn_in: int) -> dict[int, dict[str, float]]:
    games = [g for g in final_games_before(conn, LATEST_GAME) if g.season >= burn_in]
    return season_boundaries(games)


def build_sharded(
    conn: sqlite3.Connection, plan: SeasonPlan, names: Sequence[str], workers: int
) -> tuple[list[TrainingRow], list[TrainingRow]]:
    """Return the actual rows and the guess rows from the first fit season to the last scored.

    The result is the same as two full builds, and it is empty when the store holds no game
    in that range. The guess shards come first in the queue, because the guess build is the
    longer one.
    """
    first, last = plan.fit_first, plan.scored[1]
    path = database_path(conn)
    boundaries = _boundaries(conn, plan.burn_in)
    seasons = sorted(season for season in boundaries if first <= season <= last)
    keys = [(mode, season) for mode in _MODES for season in seasons]
    units = [
        Unit(
            f"build {mode.value} rows {season}",
            build_season_rows,
            (path, Shard(season, mode, plan.burn_in, tuple(names), boundaries[season])),
        )
        for mode, season in keys
    ]
    parts = dict(zip(keys, run_units(units, workers), strict=True))
    return (
        [row for season in seasons for row in parts[(GoalieMode.ACTUAL, season)]],
        [row for season in seasons for row in parts[(GoalieMode.GUESS, season)]],
    )
