"""Parse the archived boxscores again with the current parser, as a new derivation (#82).

A rebuild reads stored payloads only. It makes no request, and it invents no fetch time: each
revision keeps the time of the observation that gave its bytes. An earlier derivation stays, so
a forecast that read it can still name its inputs.
Design: docs/design/payload-archive.md
"""

import json
import sqlite3
from dataclasses import dataclass, replace

from hockey_model.sources.nhl import BOXSCORE_PARSER, BoxscoreRows, parse_boxscore
from hockey_model.sources.shape_errors import SourceParseError, SourceShapeError
from hockey_model.store.archive import (
    OK,
    SHAPE_ERROR,
    Observation,
    observations_for,
    payload_bytes,
)
from hockey_model.store.boxscore_revisions import (
    LEGACY_PARSER,
    BoxscoreRevision,
    derivation_revisions,
    insert_revision,
    newest_derivation,
    revision_of,
    same_content,
)
from hockey_model.store.db import StoreError
from hockey_model.store.refresh_nhl import check_boxscore_identity
from hockey_model.store.source_reads import BOXSCORES


@dataclass(frozen=True, slots=True)
class RebuildReport:
    """What a rebuild did: the games it read, the new derivations, and the payloads it refused."""

    source: str
    parser: str
    games: int
    derived: int
    unchanged: int
    rejected: int


def _parsed(conn: sqlite3.Connection, game_id: int, obs: Observation) -> BoxscoreRows | None:
    """Return the rows of one archived payload, or None when the parser or the check refuses it."""
    if obs.sha256 is None or obs.status not in (OK, SHAPE_ERROR):
        return None
    try:
        rows = parse_boxscore(json.loads(payload_bytes(conn, obs.sha256)))
        if rows.game_id != game_id:
            return None
        check_boxscore_identity(conn, game_id, rows)
    except ValueError, SourceParseError, SourceShapeError:
        return None
    return rows


def _chain(
    conn: sqlite3.Connection,
    game_id: int,
    derivation: int,
    previous: list[BoxscoreRevision],
) -> tuple[list[BoxscoreRevision], int]:
    """Return the revisions that the archive gives under the current parser, and the refusals.

    A revision with no bytes cannot be parsed again, so it carries over as it stands.
    """
    chain = [replace(r, derivation=derivation) for r in previous if r.parser == LEGACY_PARSER]
    rejected = 0
    for obs in observations_for(conn, BOXSCORES, str(game_id)):
        if obs.sha256 is None:
            continue
        rows = _parsed(conn, game_id, obs)
        if rows is None:
            rejected += 1
            continue
        candidate = revision_of(rows, obs, derivation, len(chain) + 1, BOXSCORE_PARSER)
        if not chain or not same_content(chain[-1], candidate):
            chain.append(candidate)
    return chain, rejected


def _same_chain(a: list[BoxscoreRevision], b: list[BoxscoreRevision]) -> bool:
    return len(a) == len(b) and all(
        (x.obs_id, x.parser) == (y.obs_id, y.parser) and same_content(x, y)
        for x, y in zip(a, b, strict=True)
    )


def rebuild_boxscores(conn: sqlite3.Connection, parser: str) -> RebuildReport:
    """Write a new derivation for each game whose archived payloads parse differently now.

    ``parser`` must name the parser that the code holds, so a stale command line fails loud.
    The whole rebuild is one transaction. A game whose derivation would equal its newest one
    is left alone, so a second run with the same parser changes nothing.
    """
    if parser != BOXSCORE_PARSER:
        raise StoreError(
            f"rebuild {BOXSCORES}: parser {parser!r} is not the parser of this code; "
            f"pass --parser {BOXSCORE_PARSER}"
        )
    keys = [
        int(key)
        for (key,) in conn.execute(
            "SELECT DISTINCT key FROM observations WHERE source = ? AND key IS NOT NULL"
            " ORDER BY CAST(key AS INTEGER)",
            (BOXSCORES,),
        )
    ]
    derived = unchanged = rejected = 0
    with conn:
        for game_id in keys:
            newest = newest_derivation(conn, game_id)
            previous = [] if newest is None else derivation_revisions(conn, game_id, newest)
            chain, refused = _chain(conn, game_id, (newest or 0) + 1, previous)
            rejected += refused
            if not chain or _same_chain(chain, previous):
                unchanged += 1
                continue
            for revision in chain:
                insert_revision(conn, revision)
            derived += 1
    return RebuildReport(BOXSCORES, parser, len(keys), derived, unchanged, rejected)
