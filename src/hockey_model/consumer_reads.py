"""The data reads behind the consumer API (#80): MoneyPuck form, injuries, and store info.

Each function takes the open read-only connection, validates its own arguments, and returns
the frozen types of ``consumer_types``. ``consumer.ConsumerStore`` is the thin public wrapper.
"""

import sqlite3
from datetime import date, datetime
from pathlib import Path

from hockey_model.consumer_types import GoalieXgLine, InjuryLine, StoreInfo, TeamXgLine
from hockey_model.store.db import SCHEMA_VERSION
from hockey_model.store.reads import franchise_codes, injury_snapshot_before, result_cutoff
from hockey_model.store.source_reads import LIVE_FEED_MAX_AGE
from hockey_model.store.sql_text import placeholders
from hockey_model.store.timestamps import iso_utc, parse_iso_utc
from hockey_model.utc import require_utc


def team_xg_games(
    conn: sqlite3.Connection, team: str, *, before: datetime, count: int
) -> tuple[TeamXgLine, ...]:
    """Return the newest MoneyPuck team games of a franchise that were over before ``before``.

    The team codes of the whole franchise are read, so a team that moved keeps its history.
    A row with a missing expected-goals value is skipped, as the model skips it.
    """
    require_utc(before, "before")
    _require_count(count, "team MoneyPuck games")
    codes = franchise_codes(team)
    marks = placeholders(codes)
    cursor = conn.execute(
        "SELECT m.game_id, m.team, m.game_date, m.xg_for, m.xg_against"
        " FROM latest_mp_team_games m JOIN latest_final_games g ON g.game_id = m.game_id"
        f" WHERE m.team IN ({marks}) AND g.start_utc <= ?"
        " AND m.xg_for IS NOT NULL AND m.xg_against IS NOT NULL"
        " ORDER BY g.start_utc DESC, g.game_id DESC LIMIT ?",
        (*codes, result_cutoff(before), count),
    )
    return tuple(
        TeamXgLine(game_id, code, date.fromisoformat(day), xg_for, xg_against)
        for game_id, code, day, xg_for, xg_against in cursor
    )


def goalie_xg_games(
    conn: sqlite3.Connection, nhl_id: int, *, before: datetime, count: int
) -> tuple[GoalieXgLine, ...]:
    """Return the newest MoneyPuck goalie games of a goalie that were over before ``before``.

    A row with a missing expected-goals or goals-against value is skipped, because the result
    type carries a number for each one.
    """
    require_utc(before, "before")
    _require_count(count, "goalie MoneyPuck games")
    cursor = conn.execute(
        "SELECT m.game_id, m.nhl_id, m.xg_against, m.goals_against"
        " FROM latest_mp_goalie_games m JOIN latest_final_games g ON g.game_id = m.game_id"
        " WHERE m.nhl_id = ? AND g.start_utc <= ?"
        " AND m.xg_against IS NOT NULL AND m.goals_against IS NOT NULL"
        " ORDER BY g.start_utc DESC, g.game_id DESC LIMIT ?",
        (nhl_id, result_cutoff(before), count),
    )
    return tuple(
        GoalieXgLine(game_id, player, xg_against, goals_against)
        for game_id, player, xg_against, goals_against in cursor
    )


def injuries(conn: sqlite3.Connection, *, as_of: datetime) -> tuple[InjuryLine, ...]:
    """Return the newest injury list that the producer read before ``as_of``.

    The age rule is the rule of the live model: a list older than ``LIVE_FEED_MAX_AGE`` is not
    used, because it can still name a player who has healed. An empty result therefore means
    that no read happened, that the newest list is too old, or that it named no player.
    ``StoreInfo.newest_reads`` names the newest good read of ``espn_injuries``, so a caller
    can tell the three cases apart.
    """
    require_utc(as_of, "as_of")
    stamp = injury_snapshot_before(conn, as_of)
    if stamp is None or as_of - stamp > LIVE_FEED_MAX_AGE:
        return ()
    rows = conn.execute(
        "SELECT team, player_name, status, return_date FROM injury_reads WHERE fetched_at = ?"
        " ORDER BY team, player_name",
        (iso_utc(stamp),),
    )
    return tuple(
        InjuryLine(team, name, status, None if day is None else date.fromisoformat(day), stamp)
        for team, name, status, day in rows
    )


def store_info(conn: sqlite3.Connection, path: Path) -> StoreInfo:
    """Return the path, the schema version, and the newest good read of each source.

    A source that every read of has failed is listed with a time of None, so a caller can see
    it and tell it apart from a source that the store never read at all.
    """
    rows = conn.execute(
        "SELECT source, MAX(CASE WHEN ok = 1 THEN fetched_at END) FROM source_reads"
        " GROUP BY source ORDER BY source"
    )
    return StoreInfo(
        str(path),
        SCHEMA_VERSION,
        tuple((source, None if stamp is None else parse_iso_utc(stamp)) for source, stamp in rows),
    )


def _require_count(count: int, what: str) -> None:
    """Refuse a count that SQLite would read as "no limit"."""
    if count < 0:
        raise ValueError(f"read {what}: count must be 0 or more, got {count}")
