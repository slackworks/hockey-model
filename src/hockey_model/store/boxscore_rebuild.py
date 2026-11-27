"""Parse the archived boxscores again with the current parser, as a new derivation (#82).

A rebuild reads stored payloads only. It makes no request, and it invents no fetch time: each
revision keeps the time of the observation that gave its bytes. An earlier derivation stays, so
a forecast that read it can still name its inputs.
Design: docs/design/payload-archive.md
"""

import sqlite3
from dataclasses import dataclass, replace

from hockey_model.sources.nhl import BOXSCORE_PARSER, BoxscoreRows, parse_boxscore
from hockey_model.sources.nhl import SOURCE as NHL_SOURCE
from hockey_model.sources.payload import get_json_text
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
    """What a rebuild did, and which payloads the parser still refuses.

    ``rejected`` counts the payloads that were a ``shape_error`` and stay one. ``refused`` names
    each of them with its game, its observation, and the reason.
    """

    source: str
    parser: str
    games: int
    derived: int
    unchanged: int
    rejected: int
    refused: tuple[str, ...] = ()


def _body(conn: sqlite3.Connection, game_id: int, obs: Observation, sha256: str) -> object:
    """Return the JSON of one archived payload, with the game and the observation named on error."""
    where = f"game {game_id} observation {obs.obs_id}"
    try:
        raw = payload_bytes(conn, sha256)
    except StoreError as exc:
        raise StoreError(f"rebuild {BOXSCORES} {where}: {exc}") from exc
    return get_json_text(NHL_SOURCE, raw, f"archived boxscore of {where}")


def _parsed(conn: sqlite3.Connection, game_id: int, obs: Observation, sha256: str) -> BoxscoreRows:
    """Return the rows of one archived payload, or raise the named parse error."""
    rows = parse_boxscore(_body(conn, game_id, obs, sha256))
    if rows.game_id != game_id:
        raise SourceParseError(NHL_SOURCE, f"boxscore names game {rows.game_id}, not {game_id}")
    check_boxscore_identity(conn, game_id, rows)
    return rows


def _chain(
    conn: sqlite3.Connection,
    game_id: int,
    derivation: int,
    previous: list[BoxscoreRevision],
) -> tuple[list[BoxscoreRevision], list[str]]:
    """Return the revisions that the archive gives under the current parser, and the refusals.

    A revision with no bytes cannot be parsed again, so it carries over as it stands. A payload
    that the first read accepted and the parser refuses now is a parser regression: the rebuild
    stops and names it, so a bad parser never replaces a good derivation. A payload that was a
    ``shape_error`` and stays one is a refusal in the report.
    """
    chain = [replace(r, derivation=derivation) for r in previous if r.parser == LEGACY_PARSER]
    refused: list[str] = []
    for obs in observations_for(conn, BOXSCORES, str(game_id)):
        if obs.sha256 is None or obs.status not in (OK, SHAPE_ERROR):
            continue
        try:
            rows = _parsed(conn, game_id, obs, obs.sha256)
        except (SourceParseError, SourceShapeError) as exc:
            if obs.status == OK:
                raise StoreError(
                    f"rebuild {BOXSCORES} game {game_id} observation {obs.obs_id}: the parser "
                    f"{BOXSCORE_PARSER} refuses a payload that the first read accepted ({exc}); "
                    "fix the parser before the rebuild"
                ) from exc
            refused.append(f"game {game_id} observation {obs.obs_id}: {exc}")
            continue
        candidate = revision_of(rows, obs, derivation, len(chain) + 1, BOXSCORE_PARSER)
        if not chain or not same_content(chain[-1], candidate):
            chain.append(candidate)
    return chain, refused


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
    derived = unchanged = 0
    refusals: list[str] = []
    with conn:
        for game_id in keys:
            newest = newest_derivation(conn, game_id)
            previous = [] if newest is None else derivation_revisions(conn, game_id, newest)
            chain, refused = _chain(conn, game_id, (newest or 0) + 1, previous)
            refusals.extend(refused)
            if not chain or _same_chain(chain, previous):
                unchanged += 1
                continue
            for revision in chain:
                insert_revision(conn, revision)
            derived += 1
    return RebuildReport(
        BOXSCORES, parser, len(keys), derived, unchanged, len(refusals), tuple(refusals)
    )
