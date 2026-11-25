"""Record which source reads worked and which failed."""

import sqlite3
from datetime import datetime, timedelta

from hockey_model.sources.errors import SourceError
from hockey_model.store.timestamps import iso_utc, parse_iso_utc

# The source name of each feed that a refresh records. ``store/archive.py`` and the refresh
# steps read these names, so this module is the one definition of each.
SCORES = "nhl_scores"
BOXSCORES = "nhl_boxscores"
MONEYPUCK = "moneypuck"
ESPN_STARTERS = "espn_starters"
ESPN_INJURIES = "espn_injuries"
DAILY_FACEOFF = "daily_faceoff"
# Every source name that a refresh records in ``source_reads``.
KNOWN_SOURCES = frozenset(
    {SCORES, BOXSCORES, MONEYPUCK, ESPN_STARTERS, ESPN_INJURIES, DAILY_FACEOFF}
)
# A URL whose last this-many reads all failed is a known gap. A refresh skips it and says so.
ABANDON_AFTER_FAILED_READS = 2
# The failed reads must span at least this long, so one outage does not make a gap.
# A streak of permanent failures, such as HTTP 403, needs no span: a retry cannot end it.
ABANDON_MIN_SPAN_DAYS = 0.5
# The feeds that a refresh reads on every run, because each has no dated alternative.
LIVE_FEEDS = (ESPN_STARTERS, ESPN_INJURIES, DAILY_FACEOFF)
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


def record_failed_read(
    conn: sqlite3.Connection, source: str, url: str, fetched_at: datetime, failure: Exception
) -> None:
    """Append a row for a read that failed, with the cause of the failure.

    A ``SourceError`` that says its status is permanent stores 1. Any other failure stores 0.
    A row from an older store holds NULL, which the reader counts as transient.
    """
    permanent = isinstance(failure, SourceError) and failure.permanent
    conn.execute(
        "INSERT INTO source_reads (source, url, fetched_at, ok, error, permanent)"
        " VALUES (?,?,?,0,?,?)",
        (source, url, iso_utc(fetched_at), str(failure), int(permanent)),
    )


def last_ok_read(conn: sqlite3.Connection, source: str, url: str) -> datetime | None:
    """Return the time of the newest read of ``url`` that worked, or ``None``."""
    row = conn.execute(
        "SELECT MAX(fetched_at) FROM source_reads WHERE source = ? AND url = ? AND ok = 1",
        (source, url),
    ).fetchone()
    return None if row[0] is None else parse_iso_utc(row[0])


def last_ok_reads(conn: sqlite3.Connection, source: str) -> dict[str, datetime]:
    """Return the time of the newest read that worked, for each URL of ``source``.

    One query serves a walk over many URLs, which would cost one round trip each with
    ``last_ok_read``.
    """
    rows = conn.execute(
        "SELECT url, MAX(fetched_at) FROM source_reads WHERE source = ? AND ok = 1 GROUP BY url",
        (source,),
    ).fetchall()
    return {url: parse_iso_utc(fetched) for url, fetched in rows}


def newest_read(conn: sqlite3.Connection, source: str, url: str) -> datetime | None:
    """Return the time of the newest read of ``url``, whether it worked or not, or ``None``."""
    row = conn.execute(
        "SELECT MAX(fetched_at) FROM source_reads WHERE source = ? AND url = ?", (source, url)
    ).fetchone()
    return None if row[0] is None else parse_iso_utc(row[0])


def newest_read_of_source(conn: sqlite3.Connection, source: str) -> datetime | None:
    """Return the time of the newest read of any URL of ``source``, or ``None``.

    A failed read counts, because the question is whether a run still reads the source.
    """
    row = conn.execute(
        "SELECT MAX(fetched_at) FROM source_reads WHERE source = ?", (source,)
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


def known_gaps(
    conn: sqlite3.Connection, source: str, before: datetime | None = None
) -> dict[str, datetime]:
    """Return each known gap of ``source`` with the time of the first failure of its streak.

    A gap is a streak of failed reads of one URL with no good read after them. The streak is
    a gap when it has ``ABANDON_AFTER_FAILED_READS`` failures that span
    ``ABANDON_MIN_SPAN_DAYS``, or when it has that many permanent failures. A row with no
    stored cause counts as transient.

    With ``before``, only the reads fetched before that instant count, so the answer is the
    one that held at that cutoff. ``None`` reads the whole log.
    """
    # A permanent count, not "every failure is permanent": one transient read must not end a gap.
    # The good-read test uses (fetched_at, rowid) order, as failed_since does.
    cutoff = None if before is None else iso_utc(before)
    cursor = conn.execute(
        "SELECT r.url, MIN(r.fetched_at) FROM source_reads r WHERE r.source = ? AND r.ok = 0"
        " AND (? IS NULL OR r.fetched_at < ?)"
        " AND NOT EXISTS ("
        "   SELECT 1 FROM source_reads o WHERE o.source = r.source AND o.url = r.url"
        "   AND o.ok = 1 AND (? IS NULL OR o.fetched_at < ?)"
        "   AND (o.fetched_at > r.fetched_at"
        "        OR (o.fetched_at = r.fetched_at AND o.rowid > r.rowid)))"
        " GROUP BY r.url HAVING"
        " (COUNT(*) >= ? AND julianday(MAX(r.fetched_at)) - julianday(MIN(r.fetched_at)) >= ?)"
        " OR SUM(COALESCE(r.permanent, 0)) >= ?",
        (
            source,
            cutoff,
            cutoff,
            cutoff,
            cutoff,
            ABANDON_AFTER_FAILED_READS,
            ABANDON_MIN_SPAN_DAYS,
            ABANDON_AFTER_FAILED_READS,
        ),
    )
    return {url: parse_iso_utc(first) for url, first in cursor}


def abandoned_urls(
    conn: sqlite3.Connection, source: str, before: datetime | None = None
) -> frozenset[str]:
    """Return the URLs of ``source`` that are known gaps. ``known_gaps`` holds the rule."""
    return frozenset(known_gaps(conn, source, before))
