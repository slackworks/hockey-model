"""Judge which source reads are stale at a prediction cutoff, by URL and by age.

One good read of a URL never clears a failed read of another URL. A live feed that has one
current URL must also be read recently. A dated resource, such as one score day, matters only
while its failure is recent.
"""

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from hockey_model.game_model.seasons import SCORED, season_of, season_start_year
from hockey_model.sources.moneypuck import TEAM_GAMES_URL, goalie_games_url
from hockey_model.store.source_reads import (
    ACTIVE_FAILURE_WINDOW,
    ESPN_INJURIES,
    KNOWN_SOURCES,
    LIVE_FEED_MAX_AGE,
    MONEYPUCK_MAX_AGE,
    abandoned_urls,
)
from hockey_model.store.timestamps import iso_utc, parse_iso_utc

_MONEYPUCK = "moneypuck"
# Sources with one current URL: it must be read recently, and a failure counts at any age.
_SINGLE_FEEDS = {ESPN_INJURIES: LIVE_FEED_MAX_AGE, "daily_faceoff": LIVE_FEED_MAX_AGE}
# Sources read once per day or game. At least one URL of each must have been read recently.
_DAILY_FEEDS = {"nhl_scores": LIVE_FEED_MAX_AGE, "espn_starters": LIVE_FEED_MAX_AGE}


@dataclass(frozen=True, slots=True)
class _Latest:
    """The newest attempt at one URL, and the newest good read of it, at a cutoff."""

    attempt_ok: bool
    attempt_at: datetime
    good_at: datetime | None


# One pass groups the newest good read of each URL, then one join attaches it. A subquery per
# URL would scan the whole table for each URL, and the table has a row for every dated feed.
_LATEST_READS_SQL = (
    "WITH ranked AS ("
    " SELECT source, url, fetched_at, ok, ROW_NUMBER() OVER ("
    "  PARTITION BY source, url ORDER BY fetched_at DESC, rowid DESC) AS rank"
    " FROM source_reads WHERE fetched_at < :cutoff),"
    " good AS ("
    " SELECT source, url, MAX(fetched_at) AS good_at FROM source_reads"
    " WHERE ok = 1 AND fetched_at < :cutoff GROUP BY source, url)"
    " SELECT l.source, l.url, l.fetched_at, l.ok, g.good_at"
    " FROM ranked l LEFT JOIN good g ON g.source = l.source AND g.url = l.url"
    " WHERE l.rank = 1"
)


def _latest_reads(conn: sqlite3.Connection, as_of: datetime) -> dict[tuple[str, str], _Latest]:
    """Return the newest attempt of each (source, URL) at or before ``as_of``.

    Two reads at one instant are ordered by row order, so the one written last wins.
    """
    cursor = conn.execute(_LATEST_READS_SQL, {"cutoff": iso_utc(as_of)})
    return {
        (source, url): _Latest(
            bool(ok), parse_iso_utc(stamp), None if good is None else parse_iso_utc(good)
        )
        for source, url, stamp, ok, good in cursor
    }


def _current_moneypuck_urls(as_of: datetime) -> frozenset[str]:
    """Return the MoneyPuck URLs that are expected to be current at ``as_of``."""
    return frozenset({TEAM_GAMES_URL, goalie_games_url(season_start_year(as_of.date()))})


def _age_limit(source: str, url: str, as_of: datetime) -> timedelta | None:
    """Return how old the newest good read of a current URL may be, or ``None`` if no limit."""
    if source in _SINGLE_FEEDS:
        return _SINGLE_FEEDS[source]
    if source == _MONEYPUCK and url in _current_moneypuck_urls(as_of):
        return MONEYPUCK_MAX_AGE
    return None


_DATED_URL = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


def _changes_prediction(url: str, as_of: datetime) -> bool:
    """Return whether a gap in the dated ``url`` is in a season the report scores.

    A dated URL is in scope when its day is in a scored season or in the current season.
    A URL with no date has no day to test, so it always counts.
    """
    # Training-day games also feed the Elo replay, so such a gap can move a rating. This rule
    # follows the owner decision in #194. It does not measure that effect.
    found = _DATED_URL.search(url)
    if found is None:
        return True
    try:
        day = date.fromisoformat(found.group())
    except ValueError as error:
        raise ValueError(f"cannot read the day in the source URL {url}: {error}") from error
    season = season_of(day)
    return SCORED[0] <= season <= SCORED[1] or season == season_of(as_of.date())


def _hours(span: timedelta) -> str:
    return f"{span.total_seconds() / 3600:.0f} hours"


def _url_problem(
    source: str, url: str, last: _Latest, as_of: datetime, abandoned: frozenset[str]
) -> str | None:
    """Return why one URL is stale at ``as_of``, or ``None``."""
    limit = _age_limit(source, url, as_of)
    if not last.attempt_ok:
        # A current feed always counts. A dated resource that a refresh gave up on counts only
        # in a scored or current season; older gaps go in the footer (see training_gaps). Any
        # other failed dated resource counts while its failure is recent.
        if limit is not None:
            counts = True
        elif url in abandoned:
            counts = _changes_prediction(url, as_of)
        else:
            counts = as_of - last.attempt_at <= ACTIVE_FAILURE_WINDOW
        return f"the newest read of {url} failed" if counts else None
    if limit is not None and last.good_at is not None and as_of - last.good_at > limit:
        return (
            f"the newest good read of {url} is {_hours(as_of - last.good_at)} old; "
            f"the limit is {_hours(limit)}"
        )
    return None


def _daily_problem(source: str, urls: list[_Latest], as_of: datetime) -> str | None:
    """Return why a once-a-day source has no recent good read, or ``None``."""
    limit = _DAILY_FEEDS.get(source)
    good = [last.good_at for last in urls if last.good_at is not None]
    if limit is None or not good or as_of - max(good) <= limit:
        return None
    return (
        f"the newest good read of it is {_hours(as_of - max(good))} old; "
        f"the limit is {_hours(limit)}"
    )


def stale_sources(conn: sqlite3.Connection, as_of: datetime) -> list[tuple[str, str]]:
    """Return ``(source, reason)`` for each reason a known source is not current at ``as_of``.

    A source with no good read at all has one reason. Otherwise each URL with a failed newest
    read, or a live URL read too long ago, has its own reason, and a daily source with no
    recent good read has one more. The result is in source then URL order.
    """
    latest = _latest_reads(conn, as_of)
    abandoned = {source: abandoned_urls(conn, source) for source in KNOWN_SOURCES}
    stale: list[tuple[str, str]] = []
    for source in sorted(KNOWN_SOURCES):
        urls = {url: last for (name, url), last in latest.items() if name == source}
        if not any(last.good_at is not None for last in urls.values()):
            stale.append((source, "no read of it has worked yet"))
            continue
        if source == _MONEYPUCK:
            stale.extend(
                (source, f"no read of {url} has worked yet")
                for url in sorted(_current_moneypuck_urls(as_of) - urls.keys())
            )
        for url in sorted(urls):
            problem = _url_problem(source, url, urls[url], as_of, abandoned[source])
            if problem is not None:
                stale.append((source, problem))
        daily = _daily_problem(source, list(urls.values()), as_of)
        if daily is not None:
            stale.append((source, daily))
    return stale


def training_gaps(conn: sqlite3.Connection, as_of: datetime) -> list[str]:
    """Return one line per abandoned URL whose day is outside the scored and current seasons.

    These gaps are not on any game row. The daily report lists them once, in its footer.
    """
    gaps: list[str] = []
    for source in sorted(KNOWN_SOURCES):
        for url in sorted(abandoned_urls(conn, source)):
            if not _changes_prediction(url, as_of):
                gaps.append(
                    f"{source}: {url} failed in every read; "
                    "its day is outside the scored and current seasons"
                )
    return gaps
