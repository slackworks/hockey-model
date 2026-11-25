"""Keep the bytes of each fetch, and the status of every fetch.

One ``observations`` row records one fetch: which source, which URL, its key, when it ran, and
whether it worked. The bytes go to ``payloads`` once per distinct body, keyed by their hash.
Design: docs/design/payload-archive.md
"""

import hashlib
import json
import sqlite3
import zlib
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from hockey_model.store.db import StoreError
from hockey_model.store.source_reads import (
    BOXSCORES,
    DAILY_FACEOFF,
    ESPN_INJURIES,
    ESPN_STARTERS,
    MONEYPUCK,
    SCORES,
)
from hockey_model.store.timestamps import iso_utc, parse_iso_utc

# The status of an observation. A failed fetch names the stage that failed. ``store_error``
# is a unit that the store refused, so the bytes arrived and the store rejected the rows.
OK = "ok"
HTTP_ERROR = "http_error"
SHAPE_ERROR = "shape_error"
STORE_ERROR = "store_error"
STATUSES = frozenset({OK, HTTP_ERROR, SHAPE_ERROR, STORE_ERROR})
# Level 6 is the zlib default. A body of a few hundred kilobytes costs little time at that
# level, and the store keeps every byte that the source sent.
COMPRESS_LEVEL: Final = 6
# The sources that a refresh records an observation for. Every one keeps its bytes, except the
# MoneyPuck team file: it changes daily and it is large, so its tables keep each changed row
# instead. Design: docs/design/payload-archive.md
ARCHIVED_SOURCES = frozenset(
    {SCORES, BOXSCORES, ESPN_STARTERS, ESPN_INJURIES, DAILY_FACEOFF, MONEYPUCK}
)
# The sources whose payload holds a betting market. The capture removes those fields before the
# bytes reach the parser or the archive, so the source name decides that, not the caller (#82).
MARKET_SOURCES = frozenset({ESPN_STARTERS, ESPN_INJURIES})


def is_market_source(source: str) -> bool:
    """Return True when the payload of ``source`` holds betting-market fields.

    Raises ``StoreError`` for a source that the archive does not list, so a new source cannot
    reach the store without a decision on its market fields.
    """
    if source not in ARCHIVED_SOURCES:
        raise StoreError(
            f"read {source}: this source is not in ARCHIVED_SOURCES, so its bytes would never "
            "reach the archive; add the source to store/archive.py with its archive rule from "
            "docs/design/payload-archive.md"
        )
    return source in MARKET_SOURCES


@dataclass(frozen=True, slots=True)
class Fetch:
    """One fetch to record: what was asked for, when, how it ended, and the bytes it gave."""

    source: str
    url: str
    fetched_at: datetime
    status: str
    parser: str
    key: str | None = None
    body: bytes | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Observation:
    """One recorded fetch, as ``observations_for`` returns it."""

    obs_id: int
    source: str
    url: str
    key: str | None
    fetched_at: datetime
    status: str
    sha256: str | None
    parser: str
    error: str | None


def canonical_bytes(payload: object) -> bytes:
    """Return the bytes that the archive stores for a JSON payload.

    The key order of the source does not reach the hash, so a payload that differs only in
    key order shares one ``payloads`` row.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def record_observation(conn: sqlite3.Connection, fetch: Fetch) -> int:
    """Write one observation, with its body when one arrived, and return its ``obs_id``.

    A body is stored once per hash. A status that is not ``ok`` keeps the bytes that did
    arrive, so a shape failure can be parsed again after a parser fix.
    """
    if fetch.status not in STATUSES:
        raise StoreError(
            f"record observation {fetch.source} {fetch.url}: status {fetch.status!r} is not one "
            f"of {', '.join(sorted(STATUSES))}; pass a status from store/archive.py"
        )
    sha256 = None
    if fetch.body is not None:
        sha256 = hashlib.sha256(fetch.body).hexdigest()
        conn.execute(
            "INSERT OR IGNORE INTO payloads (sha256, body, size) VALUES (?, ?, ?)",
            (sha256, zlib.compress(fetch.body, COMPRESS_LEVEL), len(fetch.body)),
        )
    cursor = conn.execute(
        "INSERT INTO observations (source, url, key, fetched_at, status, sha256, parser, error)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (
            fetch.source,
            fetch.url,
            fetch.key,
            iso_utc(fetch.fetched_at),
            fetch.status,
            sha256,
            fetch.parser,
            fetch.error,
        ),
    )
    if cursor.lastrowid is None:
        raise StoreError(
            f"record observation {fetch.source} {fetch.url}: the store returned no row id, so "
            "the observation cannot be named; run `hockey-model store upgrade` on this store"
        )
    return cursor.lastrowid


def payload_bytes(conn: sqlite3.Connection, sha256: str) -> bytes:
    """Return the uncompressed bytes of ``sha256``, or raise naming the hash.

    The stored size bounds the output, so a corrupt or tampered row cannot expand without a
    limit.
    """
    row = conn.execute("SELECT body, size FROM payloads WHERE sha256 = ?", (sha256,)).fetchone()
    if row is None:
        raise StoreError(
            f"read payload {sha256}: the store holds no payload with that hash; "
            "the store may be from before the archive, or the hash is wrong"
        )
    body, size = row
    # One byte past the recorded size is enough to prove the row is corrupt, so a crafted body
    # cannot expand into a larger buffer than the row claims.
    limit = int(size) + 1
    try:
        found = zlib.decompressobj().decompress(body, limit)
    except zlib.error as exc:
        raise StoreError(
            f"read payload {sha256}: the stored body is not valid zlib data ({exc}); "
            "the payload is corrupt and the source must be read again"
        ) from exc
    if len(found) != size:
        raise StoreError(
            f"read payload {sha256}: the stored body decompressed to more than the {size} bytes "
            "that the row records; the payload is corrupt and the source must be read again"
        )
    return found


def observations_for(
    conn: sqlite3.Connection, source: str, key: str | None, *, before: datetime | None = None
) -> list[Observation]:
    """Return the observations of ``source`` and ``key`` in fetch order.

    ``before`` keeps the observations that came before one instant, so a caller can read the
    record as it stood at that time.
    """
    query = (
        "SELECT obs_id, url, key, fetched_at, status, sha256, parser, error"
        " FROM observations WHERE source = ? AND key IS ?"
    )
    params: list[object] = [source, key]
    if before is not None:
        query += " AND fetched_at < ?"
        params.append(iso_utc(before))
    query += " ORDER BY obs_id"
    return [
        Observation(
            int(obs_id),
            source,
            url,
            None if row_key is None else str(row_key),
            parse_iso_utc(fetched_at),
            status,
            sha256,
            parser,
            error,
        )
        for obs_id, url, row_key, fetched_at, status, sha256, parser, error in conn.execute(
            query, params
        )
    ]
