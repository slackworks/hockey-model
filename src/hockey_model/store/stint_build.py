"""Build and store the stints of a game from its newest play-by-play and shift chart (#35).

The inputs are the rows that the views of the newest observations give. The result names both
observations, so a newer pair of observations adds its own stints and leaves the old ones.
Design: docs/design/pbp-shift-dataset.md
"""

import json
import sqlite3
from dataclasses import dataclass

from hockey_model.stints import Interval, Stint, Stints, build_stints
from hockey_model.store.db import StoreError
from hockey_model.store.source_reads import NHL_PBP, NHL_SHIFTS
from hockey_model.types import HockeyModelError


@dataclass(frozen=True, slots=True)
class GameInputs:
    """The rows that the stints of one game come from."""

    game_id: int
    pbp_obs: int
    shift_obs: int
    home: str
    away: str
    intervals: tuple[Interval, ...]
    positions: dict[int, str]


@dataclass(frozen=True, slots=True)
class GameStints:
    """The inputs of one game and the stints that they gave."""

    inputs: GameInputs
    stints: Stints

    @property
    def pbp_obs(self) -> int:
        return self.inputs.pbp_obs

    @property
    def shift_obs(self) -> int:
        return self.inputs.shift_obs


def _latest_observation(conn: sqlite3.Connection, game_id: int, source: str) -> int | None:
    found = conn.execute(
        "SELECT o.obs_id FROM observations o"
        " JOIN latest_event_observations l ON l.obs_id = o.obs_id"
        " WHERE o.source = ? AND o.key = ?",
        (source, str(game_id)),
    ).fetchone()
    return None if found is None else found[0]


def _teams(conn: sqlite3.Connection, game_id: int) -> tuple[str, str]:
    found = conn.execute(
        "SELECT home, away FROM games WHERE game_id = ? ORDER BY fetched_at DESC LIMIT 1",
        (game_id,),
    ).fetchone()
    if found is None:
        raise StoreError(
            f"game {game_id}: no row in games, so the home team is unknown; "
            "refresh the score day of the game first"
        )
    return found[0], found[1]


def game_inputs(conn: sqlite3.Connection, game_id: int) -> GameInputs | None:
    """Return the rows of the newest pair of observations of a game, or None.

    None means that the game has no newest play-by-play, no newest shift chart, or that the
    newest one has no rows because the parser refused it.
    """
    pbp = _latest_observation(conn, game_id, NHL_PBP)
    shift = _latest_observation(conn, game_id, NHL_SHIFTS)
    if pbp is None or shift is None:
        return None
    shifts = conn.execute(
        "SELECT player_id, team, period, start_second, end_second FROM shifts"
        " WHERE obs_id = ? ORDER BY row_no",
        (shift,),
    ).fetchall()
    roster = dict(
        conn.execute("SELECT player_id, position FROM pbp_roster WHERE obs_id = ?", (pbp,))
    )
    if not shifts or not roster:
        return None
    home, away = _teams(conn, game_id)
    intervals = tuple(Interval(*row) for row in shifts)
    return GameInputs(game_id, pbp, shift, home, away, intervals, roster)


def _ids(players: tuple[int, ...]) -> str:
    return json.dumps(players, separators=(",", ":"))


def _row(inputs: GameInputs, number: int, stint: Stint) -> tuple:
    return (
        inputs.game_id,
        inputs.pbp_obs,
        inputs.shift_obs,
        number,
        stint.period,
        stint.start,
        stint.end,
        _ids(stint.home_skaters),
        _ids(stint.away_skaters),
        _ids(stint.home_goalies),
        _ids(stint.away_goalies),
    )


def write_stints(conn: sqlite3.Connection, inputs: GameInputs, stints: Stints) -> None:
    """Replace the stints of one pair of observations. The caller owns the transaction."""
    conn.execute(
        "DELETE FROM stints WHERE game_id = ? AND pbp_obs = ? AND shift_obs = ?",
        (inputs.game_id, inputs.pbp_obs, inputs.shift_obs),
    )
    conn.executemany(
        "INSERT INTO stints (game_id, pbp_obs, shift_obs, stint, period, start_second,"
        " end_second, home_skaters, away_skaters, home_goalies, away_goalies)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [_row(inputs, number, s) for number, s in enumerate(stints.stints, start=1)],
    )


def build_game_stints(conn: sqlite3.Connection, game_id: int) -> GameStints | None:
    """Build and store the stints of one game, or return None when it has no inputs."""
    inputs = game_inputs(conn, game_id)
    if inputs is None:
        return None
    try:
        stints = build_stints(inputs.intervals, inputs.positions, inputs.home, inputs.away)
    except HockeyModelError as exc:
        raise StoreError(f"game {game_id}: {exc}") from exc
    write_stints(conn, inputs, stints)
    return GameStints(inputs, stints)
