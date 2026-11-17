"""Find which goalies had left a franchise at a cutoff, from the games they played."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from hockey_model.sources.teams import franchise
from hockey_model.store.reads import result_cutoff


@dataclass(frozen=True, slots=True)
class Membership:
    """Goalies that the games show to be gone from a franchise, or to have conflicting teams."""

    departed: frozenset[int]
    conflicting: frozenset[int]


def _newest_teams(conn: sqlite3.Connection, nhl_id: int, cutoff: str) -> set[str]:
    """Return the franchises of the goalie in the newest final game started by ``cutoff``."""
    cursor = conn.execute(
        "SELECT w.team FROM goalie_games w JOIN latest_final_games g ON g.game_id = w.game_id"
        " WHERE w.nhl_id = ? AND g.start_utc = ("
        "  SELECT MAX(g2.start_utc) FROM goalie_games w2"
        "  JOIN latest_final_games g2 ON g2.game_id = w2.game_id"
        "  WHERE w2.nhl_id = ? AND g2.start_utc <= ?)",
        (nhl_id, nhl_id, cutoff),
    )
    return {franchise(team) for (team,) in cursor}


def goalie_membership_before(
    conn: sqlite3.Connection, team: str, nhl_ids: set[int], before: datetime
) -> Membership:
    """Return which of ``nhl_ids`` had left the franchise of ``team`` at ``before``.

    The newest final game of the goalie that started by the cutoff decides, so a later game
    is not known yet and a trade back counts as a return. Two franchises at one start time
    conflict, and the goalie is not called departed. The cutoff uses the same result delay as
    ``goalie_starts_before``.
    """
    home = franchise(team)
    cutoff = result_cutoff(before)
    departed: set[int] = set()
    conflicting: set[int] = set()
    for nhl_id in sorted(nhl_ids):
        teams = _newest_teams(conn, nhl_id, cutoff)
        if len(teams) > 1:
            conflicting.add(nhl_id)
        elif teams and home not in teams:
            departed.add(nhl_id)
    return Membership(frozenset(departed), frozenset(conflicting))
