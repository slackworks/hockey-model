"""Build the feature rows of a backtest one season at a time, in worker processes.

A full build replays the Elo from the first game, so it runs in one process for close to an
hour on the real store. A season shard starts from the ratings before its first game, which one
cheap pass over the results gives (``season_boundaries``), and reads the same ratings as the
replay. The shards of all seasons and of both goalie modes then run side by side, and joining
them in season order gives the rows of the full build. A season whose rows are in the season
row cache under its current key is not built again. Design: docs/design/backtest-speed.md
"""

import sqlite3
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from hockey_model.backtest.row_cache import SeasonFile, cache_dir
from hockey_model.backtest.season_key import season_keys
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
    # Where the worker writes the rows of this shard.
    cache: SeasonFile


def build_season_rows(path: Path, shard: Shard) -> list[TrainingRow]:
    """Build the rows of one shard in a worker, on its own read-only connection."""
    conn = open_read_only(path)
    try:
        _check_unchanged(conn, shard)
        rows = training_rows(
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
    # A worker writes its own season, so the seasons that finished survive a failed run.
    shard.cache.save(rows)
    return rows


def _check_unchanged(conn: sqlite3.Connection, shard: Shard) -> None:
    """Raise when the store of the worker differs from the store that the parent read.

    The key of the season covers the data of the rows, so a worker never saves rows that the
    key of the parent does not name.
    """
    now = len(_final_games(conn, shard.burn_in))
    if now != shard.final_games:
        raise HockeyModelError(
            f"backtest: the store changed during the run: the parent read "
            f"{shard.final_games} final games and a worker read {now}; do not refresh "
            "the store while a backtest or a fit runs, then run it again"
        )
    key = season_keys(conn, shard.burn_in, shard.names).get(shard.season)
    if key != shard.cache.key:
        raise HockeyModelError(
            f"backtest: the store changed during the run: the data of season {shard.season} "
            "differs from what the parent read; do not refresh the store while a backtest or "
            "a fit runs, then run it again"
        )


def _final_games(conn: sqlite3.Connection, burn_in: int) -> list[GameRow]:
    return [g for g in final_games_before(conn, LATEST_GAME) if g.season >= burn_in]


_Pair = tuple[GoalieMode, int]


def _read_cached(
    files: Mapping[_Pair, SeasonFile], games: Sequence[GameRow]
) -> tuple[dict[_Pair, list[TrainingRow]], list[_Pair]]:
    """Return the rows that the cache holds, and the pairs that it does not hold.

    A file counts only when it holds the final games of its season in the store.
    """
    found: dict[_Pair, list[TrainingRow]] = {}
    missing: list[_Pair] = []
    for pair, file in files.items():
        cached = file.load([g.game_id for g in games if g.season == file.season])
        if cached is None:
            missing.append(pair)
        else:
            found[pair] = cached
    return found, missing


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
    key = season_keys(conn, burn_in, names)
    files = {
        (mode, season): SeasonFile(cache_dir(path), mode, season, tuple(names), key[season])
        for mode in modes
        for season in seasons
    }
    parts, missing = _read_cached(files, games)
    print(
        f"backtest: season rows: {len(parts)} read from the cache, {len(missing)} to build",
        file=sys.stderr,
    )
    units = [
        Unit(
            f"build {mode.value} rows {season}",
            build_season_rows,
            (
                path,
                Shard(
                    season,
                    mode,
                    burn_in,
                    tuple(names),
                    boundaries[season],
                    len(games),
                    files[(mode, season)],
                ),
            ),
        )
        for mode, season in missing
    ]
    parts.update(zip(missing, run_units(units, workers, "build-rows"), strict=True))
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

    One worker runs the full build and reads no cache. More workers build one season at a time,
    read the season row cache, and give the same rows. The fit command uses this.
    """
    if check_workers(workers) == 1:
        return training_rows(conn, first, last, GoalieMode.ACTUAL, burn_in, names=names)
    return _build(conn, (first, last), burn_in, names, (GoalieMode.ACTUAL,), workers)[
        GoalieMode.ACTUAL
    ]
