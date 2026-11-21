"""Write MoneyPuck reads to the store.

A read adds a row only when the game is new or MoneyPuck changed a value, so a weekly
read of the whole file does not copy every row again. Data from MoneyPuck.com.
"""

import sqlite3
from collections.abc import Sequence
from datetime import datetime

from hockey_model.sources.moneypuck import GoalieGameRow, TeamGameRow
from hockey_model.store.timestamps import iso_utc


def write_team_games(
    conn: sqlite3.Connection, rows: Sequence[TeamGameRow], fetched_at: datetime
) -> int:
    """Append the new or changed team-game rows. Return how many were written."""
    newest = {
        (game_id, team): values
        for game_id, team, *values in conn.execute(
            "SELECT game_id, team, game_date, xg_for, xg_against, shot_attempts_for,"
            " shot_attempts_against FROM latest_mp_team_games"
        )
    }
    fresh = [row for row in rows if newest.get((row.game_id, row.team)) != list(row[2:])]
    stamp = iso_utc(fetched_at)
    conn.executemany(
        "INSERT INTO mp_team_games (game_id, team, game_date, xg_for, xg_against,"
        " shot_attempts_for, shot_attempts_against, fetched_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [(*row, stamp) for row in fresh],
    )
    return len(fresh)


def write_goalie_games(
    conn: sqlite3.Connection, rows: Sequence[GoalieGameRow], fetched_at: datetime
) -> int:
    """Append the new or changed goalie-game rows. Return how many were written."""
    newest = {
        (game_id, nhl_id): values
        for game_id, nhl_id, *values in conn.execute(
            "SELECT game_id, nhl_id, team, xg_against, goals_against FROM latest_mp_goalie_games"
        )
    }
    fresh = [row for row in rows if newest.get((row.game_id, row.nhl_id)) != list(row[2:])]
    stamp = iso_utc(fetched_at)
    conn.executemany(
        "INSERT INTO mp_goalie_games (game_id, nhl_id, team, xg_against, goals_against,"
        " fetched_at) VALUES (?,?,?,?,?,?)",
        [(*row, stamp) for row in fresh],
    )
    return len(fresh)
