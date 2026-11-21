"""List the live feeds that are known gaps, and say which gaps are older than the limit.

A refresh reads a live feed on every run and hides the failure of a feed that is already a gap
(#276), so the run exits 0 and the report publishes with the feed missing. The daily check reads
this list to turn a feed that stays a gap for too long into a red job (#278).
"""

import json
import sqlite3
from datetime import datetime
from importlib import resources
from typing import Any

from hockey_model.json_schema import validate_json
from hockey_model.store.source_reads import (
    LIVE_FEED_MAX_AGE,
    LIVE_FEEDS,
    known_gaps,
    last_ok_read,
)
from hockey_model.store.timestamps import iso_utc
from hockey_model.types import HockeyModelError

SCHEMA_FILE = "live_gaps.schema.json"
_SECONDS_PER_HOUR = 3600


def _check(payload: dict[str, Any]) -> None:
    """Raise ``HockeyModelError`` unless ``payload`` matches the schema beside this module."""
    try:
        text = resources.files("hockey_model").joinpath(SCHEMA_FILE).read_text(encoding="utf-8")
    except OSError as exc:
        raise HockeyModelError(
            f"cannot read the schema file {SCHEMA_FILE} from the installed package: {exc}; "
            "reinstall the hockey-model wheel"
        ) from exc
    validate_json(payload, json.loads(text), SCHEMA_FILE)


def live_gap_report(conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    """Return every known gap of a live feed, with its age at ``now``.

    ``over_limit`` is True when the gap is older than ``LIVE_FEED_MAX_AGE``. A gap that is
    exactly as old as the limit is not over it. The list is in source and URL order.
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
            age = now - since
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
    }
    _check(payload)
    return payload
