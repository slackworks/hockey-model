"""Record which source reads worked and which failed."""

import sqlite3
from datetime import datetime, timedelta

from hockey_model.store.timestamps import iso_utc, parse_iso_utc

# Every source name that a refresh records in ``source_reads``.
KNOWN_SOURCES = frozenset(
    {"nhl_scores", "nhl_boxscores", "moneypuck", "espn_starters", "espn_injuries", "daily_faceoff"}
)
# A URL whose last this-many reads all failed is a known gap. A refresh skips it and says so.
ABANDON_AFTER_FAILED_READS = 2
# The failed reads must span at least this long, so one outage does not make a gap.
ABANDON_MIN_SPAN_DAYS = 0.5
# The source name of the ESPN injury list. Each good read of the list is one snapshot.
ESPN_INJURIES = "espn_injuries"
# How old the newest good read of a live feed may be at a prediction cutoff. The hourly prepare
# and report runs read the feeds, so a read older than a day means a long outage.
LIVE_FEED_MAX_AGE = timedelta(days=1)
# The MoneyPuck files change about once a week (REFRESH_AFTER in sources/moneypuck.py), so the
# limit is two refresh periods.
MONEYPUCK_MAX_AGE = timedelta(days=14)
# A failed read of a dated resource, such as one score day, counts against a prediction for this
# long. A failure older than that is retired: it stays in the audit table, not in the notes.
ACTIVE_FAILURE_WINDOW = timedelta(days=7)


def record_read(
    conn: sqlite3.Connection,
    source: str,
    url: str,
    fetched_at: datetime,
    error: str | None = None,
) -> None:
    """Append a row to ``source_reads``. ``error`` is ``None`` for a read that worked."""
    conn.execute(
        "INSERT INTO source_reads (source, url, fetched_at, ok, error) VALUES (?,?,?,?,?)",
        (source, url, iso_utc(fetched_at), int(error is None), error),
    )


def last_ok_read(conn: sqlite3.Connection, source: str, url: str) -> datetime | None:
    """Return the time of the newest read of ``url`` that worked, or ``None``."""
    row = conn.execute(
        "SELECT MAX(fetched_at) FROM source_reads WHERE source = ? AND url = ? AND ok = 1",
        (source, url),
    ).fetchone()
    return None if row[0] is None else parse_iso_utc(row[0])


def newest_read(conn: sqlite3.Connection, source: str, url: str) -> datetime | None:
    """Return the time of the newest read of ``url``, whether it worked or not, or ``None``."""
    row = conn.execute(
        "SELECT MAX(fetched_at) FROM source_reads WHERE source = ? AND url = ?", (source, url)
    ).fetchone()
    return None if row[0] is None else parse_iso_utc(row[0])


def failed_since(conn: sqlite3.Connection, source: str, url: str, since: datetime) -> bool:
    """Return True when the newest read of ``url`` failed and came after ``since``.

    Two reads of one URL can share a fetch time, because a refresh takes its stamp from one
    clock tick. The row id breaks the tie, so a good read that came after a failed one wins.
    """
    row = conn.execute(
        "SELECT ok, fetched_at FROM source_reads WHERE source = ? AND url = ?"
        " ORDER BY fetched_at DESC, rowid DESC LIMIT 1",
        (source, url),
    ).fetchone()
    return row is not None and not row[0] and parse_iso_utc(row[1]) > since


def failed_urls(conn: sqlite3.Connection, source: str) -> list[str]:
    """Return the URLs of ``source`` whose newest read failed, in name order.

    The row id breaks a tie on the fetch time, as ``failed_since`` does, so a good read that
    came after a failed one at the same time does not list the URL.
    """
    cursor = conn.execute(
        "SELECT r.url FROM source_reads r WHERE r.source = ? AND r.rowid ="
        " (SELECT n.rowid FROM source_reads n WHERE n.source = r.source AND n.url = r.url"
        "  ORDER BY n.fetched_at DESC, n.rowid DESC LIMIT 1)"
        " AND r.ok = 0 ORDER BY r.url",
        (source,),
    )
    return [url for (url,) in cursor]


def abandoned_urls(conn: sqlite3.Connection, source: str) -> frozenset[str]:
    """Return the URLs of ``source`` whose reads failed ``ABANDON_AFTER_FAILED_READS`` times
    in a row, with no good read between them, over at least ``ABANDON_MIN_SPAN_DAYS``."""
    cursor = conn.execute(
        "SELECT r.url FROM source_reads r WHERE r.source = ? AND r.ok = 0 AND r.fetched_at >"
        " COALESCE((SELECT MAX(o.fetched_at) FROM source_reads o"
        "           WHERE o.source = r.source AND o.url = r.url AND o.ok = 1), '')"
        " GROUP BY r.url HAVING COUNT(*) >= ?"
        " AND julianday(MAX(r.fetched_at)) - julianday(MIN(r.fetched_at)) >= ?",
        (source, ABANDON_AFTER_FAILED_READS, ABANDON_MIN_SPAN_DAYS),
    )
    return frozenset(url for (url,) in cursor)
