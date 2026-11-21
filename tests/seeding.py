"""Helpers that put small, known rows into a test store."""

import sqlite3
from datetime import UTC, datetime

from hockey_model.game_model.logistic import LogisticFit
from hockey_model.sources.moneypuck import GoalieGameRow, TeamGameRow
from hockey_model.sources.nhl import BoxscoreRows, GameRow, GoalieRow, PlayerRow
from hockey_model.store.fits import FitArtifact
from hockey_model.store.moneypuck_writes import write_goalie_games, write_team_games
from hockey_model.store.nhl_writes import write_boxscore, write_games
from hockey_model.store.source_reads import ESPN_INJURIES, record_read
from hockey_model.store.timestamps import iso_utc

FETCHED = datetime(2026, 11, 1, tzinfo=UTC)
ARTIFACT_TIME = datetime(2026, 1, 1, tzinfo=UTC)
# A game cutoff later than any test game, so a fit that names a season read every game of it.
LATEST = datetime(9000, 1, 1, tzinfo=UTC)


def artifact(
    fit: LogisticFit, through: int | None = 20252026, trained_through: datetime | None = LATEST
) -> FitArtifact:
    """Return the saved-fit record that a model needs, for a fit that a test builds.

    The game cutoff defaults to ``LATEST``, so a test that names a season gets a fit that read
    every game of it, as the season bound alone used to mean. Pass ``trained_through`` to bind
    the fit to a game, or ``None`` to let ``created_at`` be the bound.
    """
    return FitArtifact(1, fit, through, ARTIFACT_TIME, trained_through=trained_through)


def utc(text: str) -> datetime:
    """Parse ``2026-01-10T03:30`` as UTC."""
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def add_game(
    conn: sqlite3.Connection,
    game_id: int,
    start: str,
    home: str = "TOR",
    away: str = "BOS",
    *,
    state: str = "OFF",
    fetched_at: datetime = FETCHED,
    season: int = 20252026,
    game_type: int = 2,
    score: tuple[int, int] = (3, 2),
) -> GameRow:
    """Store one game. A final game gets a 3-2 score unless ``score`` says otherwise."""
    final = state in {"OFF", "FINAL"}
    row = GameRow(
        game_id,
        season,
        game_type,
        utc(start),
        home,
        away,
        state,
        score[0] if final else None,
        score[1] if final else None,
        "REG" if final else None,
    )
    write_games(conn, [row], fetched_at)
    return row


def add_goalie(
    conn: sqlite3.Connection,
    game_id: int,
    team: str,
    nhl_id: int,
    name: str,
    *,
    started: bool = True,
    fetched_at: datetime = FETCHED,
) -> None:
    """Store a goalie line of a final game, and the goalie as a player."""
    # One goalie line, not a whole boxscore, so only the goalie's own team is named.
    rows = BoxscoreRows(
        game_id,
        team,
        "",
        [PlayerRow(nhl_id, name, "G", team)],
        [],
        [GoalieRow(team, nhl_id, 3600 if started else 0, started)],
    )
    write_boxscore(conn, game_id, rows, fetched_at)


def add_team_form(
    conn: sqlite3.Connection,
    game_id: int,
    team: str,
    xg_for: float,
    xg_against: float,
    *,
    attempts: tuple[int, int] = (50, 50),
) -> None:
    """Store one MoneyPuck team-game row. The unblocked counts are fixed at 40 and 40."""
    row = TeamGameRow(game_id, team, "2026-01-01", xg_for, xg_against, *attempts, 40, 40)
    write_team_games(conn, [row], FETCHED)


def add_goalie_form(
    conn: sqlite3.Connection, game_id: int, nhl_id: int, xg_against: float, goals_against: int
) -> None:
    """Store one MoneyPuck goalie-game row."""
    write_goalie_games(
        conn, [GoalieGameRow(game_id, nhl_id, "TOR", xg_against, goals_against)], FETCHED
    )


def add_starter_read(
    conn: sqlite3.Connection,
    game_id: int,
    team: str,
    source: str,
    name: str,
    status: str,
    *,
    fetched_at: str,
) -> None:
    """Store one goalie read."""
    conn.execute(
        "INSERT INTO starter_reads (game_id, team, source, goalie_name, status, fetched_at)"
        " VALUES (?,?,?,?,?,?)",
        (game_id, team, source, name, status, iso_utc(utc(fetched_at))),
    )


def add_injury(
    conn: sqlite3.Connection, team: str, name: str, status: str, fetched_at: str
) -> None:
    """Store one injury read, and the good read of the list that it came from."""
    stamp = utc(fetched_at)
    conn.execute(
        "INSERT INTO injury_reads (team, player_name, status, return_date, fetched_at)"
        " VALUES (?,?,?,NULL,?)",
        (team, name, status, iso_utc(stamp)),
    )
    record_injury_snapshot(conn, fetched_at)


def record_injury_snapshot(conn: sqlite3.Connection, fetched_at: str) -> None:
    """Record a good read of the injury list at ``fetched_at``, even when the list is empty."""
    record_read(conn, ESPN_INJURIES, "u", utc(fetched_at))


def add_retired_columns(conn: sqlite3.Connection) -> None:
    """Give a fresh store the two columns that the 6->7 step dropped."""
    for table in ("starter_reads", "injury_reads"):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN nhl_id INTEGER")


def stamp_old_version(conn: sqlite3.Connection, version: int) -> None:
    """Make a fresh store look like ``version``, which is below 7."""
    add_retired_columns(conn)
    conn.execute("UPDATE schema_version SET version = ?", (version,))
