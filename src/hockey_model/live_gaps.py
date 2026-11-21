"""List the live feeds that are known gaps, and say which feeds nobody reads any more.

A refresh reads a live feed on every run and hides the failure of a feed that is already a gap
(#276), so the run exits 0 and the report publishes with the feed missing. The daily check reads
this list to turn a feed that stays a gap for too long into a red job (#278). It also reads the
newest read of each feed, because a store that no run writes any more makes no new failure, and
no gap would age (#294).
"""

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from hockey_model.json_schema import validate_json
from hockey_model.report_schema import packaged_schema
from hockey_model.store.source_reads import (
    LIVE_FEED_MAX_AGE,
    LIVE_FEEDS,
    known_gaps,
    last_ok_read,
    newest_read_of_source,
)
from hockey_model.store.timestamps import iso_utc
from hockey_model.types import HockeyModelError

SCHEMA_FILE = "live_gaps.schema.json"
_SECONDS_PER_HOUR = 3600
# Two runners can disagree on the time by a little. A read dated further ahead than this
# is bad data, and it would hide a feed that nobody reads.
FUTURE_READ_TOLERANCE = timedelta(hours=1)


def live_gap_report(conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    """Return every known gap of a live feed, with its age at ``now``.

    ``over_limit`` is True when the gap is older than ``LIVE_FEED_MAX_AGE``. A gap that is
    exactly as old as the limit is not over it. The list is in source and URL order.
    ``feeds`` names the newest read of each live feed, and a feed with no read, or with a
    newest read older than the limit, is over it.
    """
    gaps: list[dict[str, Any]] = []
    for source in LIVE_FEEDS:
        try:
            found = known_gaps(conn, source)
            good = {url: last_ok_read(conn, source, url) for url in found}
        except ValueError as exc:
            raise HockeyModelError(
                f"read the gaps of {source}: a fetched_at value is not a time: {exc}; "
                "inspect the source_reads rows of that source"
            ) from exc
        for url, since in sorted(found.items()):
            # A start after ``now`` comes from clock skew between two runners, so it has no age.
            age = max(now - since, timedelta(0))
            last_ok = good[url]
            gaps.append(
                {
                    "source": source,
                    "url": url,
                    "gap_since": iso_utc(since),
                    "age_hours": round(age.total_seconds() / _SECONDS_PER_HOUR, 2),
                    "last_ok": None if last_ok is None else iso_utc(last_ok),
                    "over_limit": age > LIVE_FEED_MAX_AGE,
                }
            )
    payload = {
        "limit_hours": LIVE_FEED_MAX_AGE.total_seconds() / _SECONDS_PER_HOUR,
        "gaps": gaps,
        "feeds": _feeds(conn, now),
    }
    validate_json(payload, packaged_schema(SCHEMA_FILE), SCHEMA_FILE)
    return payload


def _feeds(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    """Return the newest read of each live feed, and whether it is older than the limit."""
    feeds: list[dict[str, Any]] = []
    for source in LIVE_FEEDS:
        try:
            newest = newest_read_of_source(conn, source)
        except ValueError as exc:
            raise HockeyModelError(
                f"read the newest read of {source}: a fetched_at value is not a time: {exc}; "
                "inspect the source_reads rows of that source"
            ) from exc
        if newest is None:
            feeds.append(
                {"source": source, "newest_read": None, "age_hours": None, "over_limit": True}
            )
            continue
        if newest - now > FUTURE_READ_TOLERANCE:
            raise HockeyModelError(
                f"read the newest read of {source}: it is dated {iso_utc(newest)}, after the "
                f"time of the check, {iso_utc(now)}; check the clocks and the fetched_at "
                "values of that source"
            )
        age = max(now - newest, timedelta(0))
        feeds.append(
            {
                "source": source,
                "newest_read": iso_utc(newest),
                "age_hours": round(age.total_seconds() / _SECONDS_PER_HOUR, 2),
                "over_limit": age > LIVE_FEED_MAX_AGE,
            }
        )
    return feeds
