"""The read-only consumer API (#80): one snapshot of the store, and the model over it.

A consumer opens the store once, reads it through one transaction, and never writes,
migrates, fits, or refreshes. ``refresh_snapshot`` is the only way to see a newer state.
The design is `docs/design/consumer-api.md`.
"""

import sqlite3
import urllib.parse
from collections.abc import Sequence
from datetime import date, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from hockey_model.consumer_errors import StoreMissingError, StoreVersionError, UnknownGameError
from hockey_model.consumer_reads import (
    goalie_xg_games,
    injuries,
    store_info,
    team_xg_games,
)
from hockey_model.consumer_types import (
    FULL_GAME_WINNER,
    RESTATEMENT,
    ConsumerPrediction,
    FitInfo,
    GameInfo,
    GoalieLine,
    GoalieXgLine,
    InjuryLine,
    SkaterLine,
    StarterInfo,
    StoreInfo,
    TeamXgLine,
)
from hockey_model.daily_report import model_identity
from hockey_model.game import Game, StarterStatus, game_win_probability
from hockey_model.game_day import game_day_window
from hockey_model.game_model.goalies import resolve_goalie
from hockey_model.game_model.nhl_model import NhlGameModel
from hockey_model.sources.nhl import FINAL_STATES
from hockey_model.store.db import SCHEMA_VERSION
from hockey_model.store.fits import FitArtifact, FitRef, load_fit
from hockey_model.store.timestamps import iso_utc, parse_iso_utc
from hockey_model.utc import require_utc

# Every view that `views.sql` creates. A consumer checks that each one exists, because it
# reads them and must not create them: a read-only connection cannot run the DDL.
REQUIRED_VIEWS = frozenset(
    {
        "latest_games",
        "latest_final_games",
        "latest_mp_team_games",
        "latest_mp_goalie_games",
        "latest_players",
    }
)

# A stored game with the time of the read that stored it. `GameRow` is the feed row and
# carries no read time, so the consumer reads the view's own row.
_GAME_SELECT = (
    "SELECT game_id, season, game_type, start_utc, home, away, state, home_score,"
    " away_score, last_period, fetched_at FROM latest_games"
)


def open_store(path: Path, *, fit: FitRef) -> ConsumerStore:
    """Open ``path`` for reading and pin the fit that ``fit`` names.

    The file must already exist. The store is never created, migrated, or changed. A missing
    file raises ``StoreMissingError``, and a schema this package cannot read raises
    ``StoreVersionError``. Use the result as a context manager, or call ``close()``.
    """
    if not path.is_file():
        raise StoreMissingError(
            f"open store {str(path)!r}: no store file there; the consumer never creates one. "
            "Point the path at a store that `hockey-model refresh` wrote"
        )
    conn = _connect_read_only(path)
    try:
        _check_schema(conn, path)
        _begin_snapshot(conn)
        artifact = load_fit(conn, fit)
        return ConsumerStore(conn, path, artifact)
    except BaseException:
        conn.close()
        raise


def _connect_read_only(path: Path) -> sqlite3.Connection:
    """Open ``path`` for reading only. Never create the file."""
    target = f"file:{urllib.parse.quote(str(path))}?mode=ro"
    try:
        return sqlite3.connect(target, uri=True, autocommit=False)
    except sqlite3.Error as exc:
        raise StoreVersionError(_open_failure_fix(path, exc)) from exc


def _open_failure_fix(path: Path, exc: sqlite3.Error) -> str:
    """Name an open failure and its fix. A lock and a damaged file need different fixes."""
    name = getattr(exc, "sqlite_errorname", "")
    if name == "SQLITE_NOTADB":
        fix = "the file is not a SQLite store, so use the path of a hockey-model store"
    elif name in {"SQLITE_BUSY", "SQLITE_LOCKED"}:
        fix = "another process holds the store; stop it, then open the store again"
    elif name in {
        "SQLITE_CANTOPEN",
        "SQLITE_READONLY",
        "SQLITE_READONLY_DIRECTORY",
        "SQLITE_PERM",
    }:
        # SQLite reports a WAL store in a read-only directory this way, because the -wal and
        # -shm files cannot be created beside it. The file itself may be readable.
        fix = (
            "the store and its directory must be readable, and SQLite writes -wal and -shm "
            "beside a WAL store; give the directory write permission, or run "
            "`PRAGMA journal_mode=DELETE` on the store as the producer"
        )
    else:
        fix = "check the file permissions, then open the store again"
    return f"open store {str(path)!r}: {exc}; {fix}"


def _check_schema(conn: sqlite3.Connection, path: Path) -> None:
    """Refuse a store that this package cannot read. Name the fix in every case."""
    conn.execute("PRAGMA query_only = ON")
    try:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
    except sqlite3.Error as exc:
        raise StoreVersionError(_open_failure_fix(path, exc)) from exc
    if row is None:
        raise StoreVersionError(
            f"open store {str(path)!r}: schema_version is empty; "
            "the file is not a hockey-model store"
        )
    found = int(row[0])
    if found != SCHEMA_VERSION:
        raise StoreVersionError(
            f"open store {str(path)!r}: schema version {found} is not version "
            f"{SCHEMA_VERSION}; the producer runs `hockey-model store upgrade`, and the "
            "consumer reads the store only after that run"
        )
    views = {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    missing = sorted(REQUIRED_VIEWS - views)
    if missing:
        raise StoreVersionError(
            f"open store {str(path)!r}: the view {missing[0]!r} is missing; the producer runs "
            "`hockey-model store upgrade`, which creates every view"
        )


def _begin_snapshot(conn: sqlite3.Connection) -> None:
    """Start the read transaction, so every later read sees one state of the store."""
    conn.execute("SELECT 1 FROM schema_version").fetchone()


class ConsumerStore:
    """A read-only view of one store, pinned to one fit and one snapshot of the data."""

    def __init__(self, conn: sqlite3.Connection, path: Path, artifact: FitArtifact) -> None:
        self._conn = conn
        self._path = path
        self._artifact = artifact
        self._fit = _fit_info(artifact)
        self._store = store_info(conn, path)

    def __enter__(self) -> ConsumerStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def fit(self) -> FitInfo:
        """The fit that this consumer serves. It never changes for this object."""
        return self._fit

    @property
    def store(self) -> StoreInfo:
        """The store this consumer reads, and the newest good read of each source."""
        return self._store

    def close(self) -> None:
        """End the read transaction and close the file."""
        self._conn.close()

    def refresh_snapshot(self) -> None:
        """End the read transaction and start a new one, so later reads see newer rows.

        The fit does not change: it is pinned when the store opens.
        """
        self._conn.rollback()
        _begin_snapshot(self._conn)
        self._store = store_info(self._conn, self._path)

    def game(self, game_id: int) -> GameInfo:
        """Return one game as the newest read of the feed describes it. An unknown game raises."""
        row = self._conn.execute(
            f"{_GAME_SELECT} WHERE game_id = ? ORDER BY fetched_at DESC LIMIT 1", (game_id,)
        ).fetchone()
        if row is None:
            raise UnknownGameError(
                f"read game {game_id}: the game is not in the store; "
                "the producer runs `hockey-model refresh`"
            )
        return _game_info(row)

    def games_on_day(self, day: date) -> tuple[GameInfo, ...]:
        """Return the games of one Eastern game day, earliest first."""
        opens, closes = game_day_window(day)
        rows = self._conn.execute(
            f"{_GAME_SELECT} WHERE start_utc >= ? AND start_utc < ? ORDER BY start_utc, game_id",
            (iso_utc(opens), iso_utc(closes)),
        )
        return tuple(_game_info(row) for row in rows)

    def skater_games(self, game_id: int) -> tuple[SkaterLine, ...]:
        """Return every skater line of one stored game. An unknown game raises."""
        self.game(game_id)
        rows = self._conn.execute(
            "SELECT team, nhl_id, toi_seconds, goals, assists FROM skater_games"
            " WHERE game_id = ? ORDER BY team, nhl_id",
            (game_id,),
        )
        return tuple(
            SkaterLine(game_id, team, nhl_id, toi, goals, assists)
            for team, nhl_id, toi, goals, assists in rows
        )

    def goalie_games(self, game_id: int) -> tuple[GoalieLine, ...]:
        """Return every goalie line of one stored game. An unknown game raises."""
        self.game(game_id)
        rows = self._conn.execute(
            "SELECT team, nhl_id, toi_seconds, started FROM goalie_games"
            " WHERE game_id = ? ORDER BY team, nhl_id",
            (game_id,),
        )
        return tuple(
            GoalieLine(game_id, team, nhl_id, toi, bool(started))
            for team, nhl_id, toi, started in rows
        )

    def team_xg_games(self, team: str, *, before: datetime, count: int) -> tuple[TeamXgLine, ...]:
        """Return the newest MoneyPuck team games of a franchise, over before ``before``."""
        return team_xg_games(self._conn, team, before=before, count=count)

    def goalie_xg_games(
        self, nhl_id: int, *, before: datetime, count: int
    ) -> tuple[GoalieXgLine, ...]:
        """Return the newest MoneyPuck goalie games of a goalie that were over before ``before``."""
        return goalie_xg_games(self._conn, nhl_id, before=before, count=count)

    def injuries(self, *, as_of: datetime) -> tuple[InjuryLine, ...]:
        """Return the newest injury list that the producer read before ``as_of``."""
        return injuries(self._conn, as_of=as_of)

    def starter(self, game_id: int, team: str, *, as_of: datetime) -> StarterInfo:
        """Return the goalie expected to start for ``team``, and how sure the store is.

        The order is the order of the live model: a confirmed read, then the boxscore starter
        of a game that has started, then a likely read, then the goalie with the most starts.
        A team that is not in the game raises ``ValueError``.
        """
        require_utc(as_of, "as_of")
        info = self.game(game_id)
        if team not in (info.home, info.away):
            raise ValueError(
                f"read starter of game {game_id}: team {team!r} is not in the game; "
                f"use {info.home!r} or {info.away!r}"
            )
        game = _game_of(info)
        goalie, notes = resolve_goalie(self._conn, game, team, as_of, use_reads=True)
        confirmed = goalie is not None and goalie.status is StarterStatus.CONFIRMED
        return StarterInfo(game_id, team, goalie, confirmed, notes)

    def predict(self, game_id: int, *, as_of: datetime, level: float = 0.8) -> ConsumerPrediction:
        """Return the prediction of one game, with the fit and the store that made it.

        The goal projections in the result are marginal intervals for one team's goals. They
        are not a joint score distribution: they give no chance of a total or of a margin.
        """
        require_utc(as_of, "as_of")
        info = self.game(game_id)
        model = NhlGameModel(self._conn, self._artifact)
        return ConsumerPrediction(
            prediction=game_win_probability(_game_of(info), model, as_of=as_of, level=level),
            game=info,
            fit=self._fit,
            store=self._store,
            as_of=as_of,
            outcome=FULL_GAME_WINNER,
            reconstruction=RESTATEMENT,
            package_version=version("hockey-model"),
        )


def _game_of(info: GameInfo) -> Game:
    """Return the model view of one stored game."""
    return Game(info.game_id, info.home, info.away, info.start)


def _game_info(row: Sequence[Any]) -> GameInfo:
    """Return the consumer view of one stored game, in ``_GAME_SELECT`` order."""
    game_id, season, game_type, start, home, away, state, home_score, away_score, last, read = row
    return GameInfo(
        game_id,
        season,
        game_type,
        parse_iso_utc(start),
        home,
        away,
        state,
        state in FINAL_STATES,
        home_score,
        away_score,
        last,
        parse_iso_utc(read),
    )


def _fit_info(artifact: FitArtifact) -> FitInfo:
    """Return the values that name the pinned fit."""
    identity = model_identity(artifact.fit, None)
    return FitInfo(
        artifact.fit_id,
        artifact.labels,
        artifact.fit_through_season,
        identity.features,
        identity.fit_sha256,
        artifact.created_at,
    )
