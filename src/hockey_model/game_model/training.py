"""Build the feature rows of past games, in start order, with the Elo before each game."""

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from hockey_model.game import Game
from hockey_model.game_model.elo import EloBook
from hockey_model.game_model.features import FeatureRow, GoalieMode, build_features
from hockey_model.game_model.seasons import ELO_BURN_IN, FIT_FIRST
from hockey_model.sources.nhl import GameRow
from hockey_model.store.reads import LATEST_GAME, final_games_before, player_catalog
from hockey_model.store.timestamps import parse_iso_utc


@dataclass(frozen=True, slots=True)
class TrainingRow:
    """A past game, its features as known at the start, and the home result."""

    game: GameRow
    features: FeatureRow
    home_won: bool


def game_input(row: GameRow) -> Game:
    """Return the public ``Game`` for a stored game."""
    return Game(row.game_id, row.home, row.away, row.start)


def _home_won(game: GameRow) -> bool:
    if game.home_score is None or game.away_score is None:
        raise ValueError(f"train on game {game.game_id}: a final game has no score; refresh again")
    return game.home_score > game.away_score


def training_rows(
    conn: sqlite3.Connection,
    first_season: int,
    last_season: int,
    mode: GoalieMode,
    burn_in: int = ELO_BURN_IN,
    *,
    names: Sequence[str],
    elo_start: Mapping[str, float] | None = None,
) -> list[TrainingRow]:
    """Return a row for each final game from ``first_season`` through ``last_season``.

    The Elo replays from the ``burn_in`` season. Each row uses the ratings before its own game
    and no data from after its start. With ``elo_start``, the replay begins at the first game of
    ``first_season`` with those ratings, which MUST be the ratings before that game
    (``season_boundaries``), and the rows are the same as those of a full replay.
    """
    start_season = burn_in if elo_start is None else first_season
    games = [g for g in final_games_before(conn, LATEST_GAME) if g.season >= start_season]
    catalog = player_catalog(conn)
    book = EloBook(elo_start)
    rows: list[TrainingRow] = []
    for game in games:
        if game.season > last_season:
            break
        if game.season >= first_season:
            features = build_features(
                conn,
                game_input(game),
                game.start,
                book,
                goalie_mode=mode,
                catalog=catalog,
                names=names,
            )
            rows.append(TrainingRow(game, features, _home_won(game)))
        book.update(game)
    return rows


def last_trained_start(conn: sqlite3.Connection, through: int) -> datetime | None:
    """Return the start of the newest final game that a fit through ``through`` reads.

    A fit records this start as its game-level cutoff, because the season cutoff names every
    game of the cutoff season. A game that starts after this moment was not final when the fit
    ran, so it cannot be in sample (#238).
    """
    row = conn.execute(
        "SELECT MAX(start_utc) FROM latest_final_games g WHERE g.season <= ?", (through,)
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return parse_iso_utc(str(row[0]))


def last_trained_season(conn: sqlite3.Connection) -> int | None:
    """Return the newest season that holds a final game, or None when no fit can train.

    A fit records this season as its cutoff, so the cutoff is the newest season whose results
    the fit read. The Elo replays the burn-in season too, but no feature row of that season
    enters a fit, and ``FitArtifact.covers_game`` reports it out of sample.
    """
    row = conn.execute("SELECT MAX(season) FROM latest_final_games").fetchone()
    if row is None or row[0] is None:
        return None
    newest = int(row[0])
    return None if newest < FIT_FIRST else newest
