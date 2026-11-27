"""Keep each revision of a final boxscore, and read one revision by a named policy (#82).

A source can correct a final boxscore after the first read. ``skater_games`` and
``goalie_games`` keep the first final rows, so the live model cannot change by accident. The
revision tables keep every distinct state that a source gave.
Design: docs/design/payload-archive.md
"""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from hockey_model.sources.nhl import BoxscoreRows, GoalieRow, SkaterRow
from hockey_model.store.archive import Observation, observation_of
from hockey_model.store.db import StoreError
from hockey_model.store.timestamps import iso_utc, parse_iso_utc

COMPLETE = "complete"
INCOMPLETE = "incomplete"
MATCHED = "matched"
# The parser of a revision that an upgrade wrote from rows that kept no bytes.
LEGACY_PARSER = "legacy:unavailable"


class Policy(Enum):
    """Which revision a reader wants."""

    FIRST = "first"
    LATEST = "latest"


FIRST = Policy.FIRST
LATEST = Policy.LATEST


@dataclass(frozen=True, slots=True)
class AsOf:
    """The newest revision that the store held at ``instant``, itself included."""

    instant: datetime


@dataclass(frozen=True, slots=True)
class BoxscoreRevision:
    """One revision of a final boxscore, with its rows and where it came from."""

    game_id: int
    derivation: int
    revision: int
    obs_id: int
    parser: str
    fetched_at: datetime
    completeness: str
    identity: str
    skaters: tuple[SkaterRow, ...]
    goalies: tuple[GoalieRow, ...]


def completeness_of(rows: BoxscoreRows) -> str:
    """Return ``complete`` when both teams have a skater line and a goalie line."""
    teams = (rows.home, rows.away)
    skaters = {line.team for line in rows.skaters}
    goalies = {line.team for line in rows.goalies}
    return COMPLETE if all(t in skaters and t in goalies for t in teams) else INCOMPLETE


def _sorted_lines(
    skaters: Sequence[SkaterRow], goalies: Sequence[GoalieRow]
) -> tuple[tuple[SkaterRow, ...], tuple[GoalieRow, ...]]:
    return tuple(sorted(skaters, key=lambda s: s.nhl_id)), tuple(
        sorted(goalies, key=lambda g: g.nhl_id)
    )


def _lines(
    conn: sqlite3.Connection, game_id: int, derivation: int, revision: int
) -> tuple[tuple[SkaterRow, ...], tuple[GoalieRow, ...]]:
    key = (game_id, derivation, revision)
    skaters = [
        SkaterRow(team, nhl_id, toi, goals, assists)
        for team, nhl_id, toi, goals, assists in conn.execute(
            "SELECT team, nhl_id, toi_seconds, goals, assists FROM skater_game_revisions"
            " WHERE game_id = ? AND derivation = ? AND revision = ?",
            key,
        )
    ]
    goalies = [
        GoalieRow(team, nhl_id, toi, bool(started))
        for team, nhl_id, toi, started in conn.execute(
            "SELECT team, nhl_id, toi_seconds, started FROM goalie_game_revisions"
            " WHERE game_id = ? AND derivation = ? AND revision = ?",
            key,
        )
    ]
    return _sorted_lines(skaters, goalies)


def newest_derivation(conn: sqlite3.Connection, game_id: int) -> int | None:
    """Return the newest derivation of the game, or None when it has no revision."""
    (found,) = conn.execute(
        "SELECT MAX(derivation) FROM boxscore_revisions WHERE game_id = ?", (game_id,)
    ).fetchone()
    return None if found is None else int(found)


def _select(policy: Policy | AsOf) -> tuple[str, list[object]]:
    """Return the SQL tail that picks one revision of a derivation, and its parameters."""
    if isinstance(policy, AsOf):
        return " AND fetched_at <= ? ORDER BY revision DESC", [iso_utc(policy.instant)]
    if policy is Policy.LATEST:
        return " ORDER BY revision DESC", []
    if policy is Policy.FIRST:
        return " ORDER BY revision", []
    raise StoreError(
        f"read boxscore revision: policy {policy!r} is not FIRST, LATEST, or AsOf(instant); "
        "pass one of those, so a forecast never reads a revision by accident"
    )


def _load(
    conn: sqlite3.Connection, game_id: int, derivation: int, tail: str, params: list[object]
) -> list[BoxscoreRevision]:
    """Return the revisions of one derivation that ``tail`` selects, each with its own lines."""
    cursor = conn.execute(
        "SELECT revision, obs_id, parser, fetched_at, completeness, identity"
        " FROM boxscore_revisions WHERE game_id = ? AND derivation = ?" + tail,
        [game_id, derivation, *params],
    ).fetchall()
    return [
        BoxscoreRevision(
            game_id,
            derivation,
            revision,
            obs_id,
            parser,
            parse_iso_utc(fetched_at),
            completeness,
            identity,
            *_lines(conn, game_id, derivation, revision),
        )
        for revision, obs_id, parser, fetched_at, completeness, identity in cursor
    ]


def derivation_revisions(
    conn: sqlite3.Connection, game_id: int, derivation: int
) -> list[BoxscoreRevision]:
    """Return every revision of one derivation of the game, oldest first."""
    return _load(conn, game_id, derivation, " ORDER BY revision", [])


def boxscore_rows(
    conn: sqlite3.Connection,
    game_id: int,
    *,
    policy: Policy | AsOf,
    derivation: int | None = None,
) -> BoxscoreRevision | None:
    """Return one revision of the boxscore of ``game_id``, or None when the policy finds none.

    ``FIRST`` and ``LATEST`` pick the oldest and the newest revision. ``AsOf(instant)`` picks the
    newest revision that was fetched at or before ``instant``: the information that the store held
    then. The rows are the rows of that one revision, so two revisions never mix. ``derivation``
    pins one parse of the payloads. The default is the newest parse, which can be a rebuild that
    ran after ``instant``, so an exact replay of an earlier forecast records ``(derivation,
    revision)`` and pins the derivation.
    """
    tail, params = _select(policy)
    newest = newest_derivation(conn, game_id)
    chosen = newest if derivation is None else derivation
    if chosen is None:
        return None
    if derivation is not None and not (newest and 1 <= derivation <= newest):
        raise StoreError(
            f"read boxscore revision: game {game_id} has no derivation {derivation}; "
            "pass a derivation that the store holds for the game, or none for the newest"
        )
    found = _load(conn, game_id, chosen, tail + " LIMIT 1", params)
    return found[0] if found else None


def insert_revision(conn: sqlite3.Connection, revision: BoxscoreRevision) -> None:
    """Write one revision row with its skater and goalie lines."""
    conn.execute(
        "INSERT INTO boxscore_revisions (game_id, derivation, revision, obs_id, parser,"
        " fetched_at, completeness, identity) VALUES (?,?,?,?,?,?,?,?)",
        (
            revision.game_id,
            revision.derivation,
            revision.revision,
            revision.obs_id,
            revision.parser,
            iso_utc(revision.fetched_at),
            revision.completeness,
            revision.identity,
        ),
    )
    key = (revision.game_id, revision.derivation, revision.revision)
    conn.executemany(
        "INSERT INTO skater_game_revisions (game_id, derivation, revision, team, nhl_id,"
        " toi_seconds, goals, assists) VALUES (?,?,?,?,?,?,?,?)",
        [(*key, *line) for line in revision.skaters],
    )
    conn.executemany(
        "INSERT INTO goalie_game_revisions (game_id, derivation, revision, team, nhl_id,"
        " toi_seconds, started) VALUES (?,?,?,?,?,?,?)",
        [(*key, g.team, g.nhl_id, g.toi_seconds, int(g.started)) for g in revision.goalies],
    )


def same_content(a: BoxscoreRevision, b: BoxscoreRevision) -> bool:
    """Return True when two revisions hold the same lines and the same status."""
    return (a.skaters, a.goalies, a.completeness, a.identity) == (
        b.skaters,
        b.goalies,
        b.completeness,
        b.identity,
    )


def revision_of(
    rows: BoxscoreRows,
    obs: Observation,
    derivation: int,
    revision: int,
    parser: str | None = None,
) -> BoxscoreRevision:
    """Return the revision that ``rows`` make, for a boxscore that passed the identity check.

    ``obs`` is the observation that gave the bytes. The parser is the one that the observation
    names, unless a rebuild passes the parser that made the rows now.
    """
    skaters, goalies = _sorted_lines(rows.skaters, rows.goalies)
    return BoxscoreRevision(
        rows.game_id,
        derivation,
        revision,
        obs.obs_id,
        obs.parser if parser is None else parser,
        obs.fetched_at,
        completeness_of(rows),
        MATCHED,
        skaters,
        goalies,
    )


def record_revision(
    conn: sqlite3.Connection, game_id: int, rows: BoxscoreRows, obs_id: int
) -> int | None:
    """Write ``rows`` as the next revision of the game, and return its number.

    The rows come from the observation ``obs_id``. A read whose rows equal the newest revision
    adds no revision and returns None: the observation alone records that the source repeated
    itself. The caller checks the identity of the boxscore before it calls this.
    """
    newest = boxscore_rows(conn, game_id, policy=Policy.LATEST)
    candidate = revision_of(
        rows,
        observation_of(conn, obs_id),
        1 if newest is None else newest.derivation,
        1 if newest is None else newest.revision + 1,
    )
    if newest is not None and same_content(newest, candidate):
        return None
    insert_revision(conn, candidate)
    return candidate.revision
