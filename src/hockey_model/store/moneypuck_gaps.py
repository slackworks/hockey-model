"""What the MoneyPuck data does not cover: rows with no stored game, and form that lags.

The form readers join a MoneyPuck row to a final game, so a row whose game the store does not
hold is dropped without a word. These counts and the lag report make that drop visible. A
MoneyPuck file is weekly, so a recent download can still stop several games short.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from hockey_model.store.reads import franchise_codes, result_cutoff

# The MoneyPuck files name a game by its NHL id. A row is useful only when the store holds
# that game and the game is over, because the form reader joins the final games.
# `games` keeps one row per read, so the count asks whether a row exists. A join would count a
# game once for each read of it, and a live game is read again at each state change.
_COUNTS = """
SELECT
    COUNT(*) FILTER (
        WHERE NOT EXISTS (SELECT 1 FROM latest_games g WHERE g.game_id = m.game_id)),
    COUNT(*) FILTER (
        WHERE EXISTS (SELECT 1 FROM latest_games g WHERE g.game_id = m.game_id)
        AND NOT EXISTS (SELECT 1 FROM latest_final_games f WHERE f.game_id = m.game_id))
FROM {table} m
"""


@dataclass(frozen=True, slots=True)
class UnmatchedRows:
    """The MoneyPuck rows that the form readers skip, by kind.

    ``unknown`` rows name a game that the store does not hold. ``not_final`` rows name a game
    that the store holds but that is not over yet, so a later read can still match them.
    """

    team_unknown: int
    goalie_unknown: int
    team_not_final: int
    goalie_not_final: int

    def unknown_note(self) -> str:
        """Return the note that names the rows of a game that the store does not hold."""
        return (
            f"moneypuck: {self.team_unknown} team-game rows and {self.goalie_unknown} "
            "goalie-game rows match no stored game"
        )

    def pending_note(self) -> str:
        """Return the note that names the rows of a game that is not over yet."""
        return (
            f"moneypuck: {self.team_not_final} team-game rows and {self.goalie_not_final} "
            "goalie-game rows are for a game that is not final"
        )


@dataclass(frozen=True, slots=True)
class FormLag:
    """How far the newest usable MoneyPuck game of a team lags its newest stored final game.

    ``games_behind`` counts the final games of the franchise that start after the newest
    MoneyPuck game, up to the cutoff. ``newest_moneypuck`` and ``newest_final`` are the two
    game ids, so a reader can tell a one-week gap from a gap of the whole season.
    """

    games_behind: int
    newest_moneypuck: int
    newest_final: int

    def note(self) -> str:
        """Return the note that a prediction carries for this lag."""
        games = "game" if self.games_behind == 1 else "games"
        return (
            f"MoneyPuck form is {self.games_behind} stored {games} behind; newest MoneyPuck "
            f"game {self.newest_moneypuck}, newest final game {self.newest_final}"
        )


def unmatched_moneypuck(conn: sqlite3.Connection) -> UnmatchedRows:
    """Return the counts of the newest MoneyPuck rows that the form readers cannot use.

    The count reads the newest row of each identity, so a repeated read of one file does not
    raise it. A row that a later game read matches leaves the count.
    """
    team_unknown, team_not_final = _counts(conn, "latest_mp_team_games")
    goalie_unknown, goalie_not_final = _counts(conn, "latest_mp_goalie_games")
    return UnmatchedRows(team_unknown, goalie_unknown, team_not_final, goalie_not_final)


def _counts(conn: sqlite3.Connection, table: str) -> tuple[int, int]:
    """Return the unknown and the not-final counts of one MoneyPuck view."""
    unknown, not_final = conn.execute(_COUNTS.format(table=table)).fetchone()
    return (unknown or 0, not_final or 0)


def mp_form_lag(conn: sqlite3.Connection, team: str, before: datetime) -> FormLag | None:
    """Return the lag of a team's MoneyPuck form at ``before``, or None when there is none.

    A team with no usable MoneyPuck game gives None, because the form reader already reports
    that case. Only games that were over at ``before`` count on either side, so a prediction
    never compares its form against a game that it cannot know.
    """
    codes = franchise_codes(team)
    marks = ",".join("?" for _ in codes)
    cutoff = result_cutoff(before)
    side = f"(g.home IN ({marks}) OR g.away IN ({marks}))"
    newest_final = conn.execute(
        f"SELECT g.game_id, g.start_utc FROM latest_final_games g WHERE {side}"
        " AND g.start_utc <= ? ORDER BY g.start_utc DESC, g.game_id DESC LIMIT 1",
        (*codes, *codes, cutoff),
    ).fetchone()
    # The form reader takes a row by the code in the row, so a row of the opposing team does
    # not cover this team, even though the two rows name one game.
    newest_mp = conn.execute(
        "SELECT m.game_id, g.start_utc FROM latest_mp_team_games m"
        f" JOIN latest_final_games g ON g.game_id = m.game_id WHERE m.team IN ({marks})"
        " AND g.start_utc <= ? AND m.xg_for IS NOT NULL AND m.xg_against IS NOT NULL"
        " ORDER BY g.start_utc DESC, g.game_id DESC LIMIT 1",
        (*codes, cutoff),
    ).fetchone()
    if newest_mp is None or newest_final is None:
        return None
    mp_game, mp_start = newest_mp
    final_game, final_start = newest_final
    if final_start <= mp_start:
        return None
    (behind,) = conn.execute(
        f"SELECT COUNT(*) FROM latest_final_games g WHERE {side}"
        " AND g.start_utc <= ? AND g.start_utc > ?",
        (*codes, *codes, cutoff, mp_start),
    ).fetchone()
    return FormLag(behind, mp_game, final_game)
