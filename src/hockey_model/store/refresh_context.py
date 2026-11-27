"""The state that one refresh shares across its sources."""

import sqlite3
from collections import Counter
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import date, datetime

from hockey_model import progress
from hockey_model.sources.errors import SourceError
from hockey_model.sources.http import HttpClient
from hockey_model.sources.shape_errors import SourceParseError, SourceShapeError
from hockey_model.store.archive import record_observation
from hockey_model.store.db import StoreError
from hockey_model.store.recording import (
    Capture,
    Reading,
    RecordedFetch,
    RecordingClient,
    key_of,
)
from hockey_model.store.source_reads import abandoned_urls, record_failed_read, record_read
from hockey_model.types import HockeyModelError

# Errors that a bad source response or a bad stored schedule cause. Other errors are bugs.
SOURCE_FAILURES = (SourceError, SourceShapeError, SourceParseError, StoreError)
# A source that fails this many units in a row stops, so a dead host does not cost hours.
MAX_UNIT_FAILURES = 3


@dataclass(frozen=True, slots=True)
class RefreshReport:
    """What one refresh wrote, what failed, and what the reader of the data must credit."""

    written: dict[str, int]
    errors: tuple[HockeyModelError, ...]
    notes: tuple[str, ...]
    credits: tuple[str, ...]
    # True when a backfill read every window, False when it stopped early, None without one.
    backfill_complete: bool | None = None
    # True when the time budget stopped a step. It says why the backfill is not complete, so a
    # caller does not blame the budget for a source failure that stopped the run first.
    timed_out: bool = False


@dataclass
class RefreshRun:
    """One refresh in progress. A source runs its work in units through ``attempt``."""

    conn: sqlite3.Connection
    client: HttpClient
    now: datetime
    backfill_from: date | None = None
    clock: Callable[[], datetime] | None = None
    deadline: datetime | None = None
    backfill_complete: bool | None = None
    written: Counter[str] = field(default_factory=Counter)
    errors: list[HockeyModelError] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Read the MoneyPuck files even when they are fresh, instead of after the weekly refresh.
    reread_moneypuck: bool = False
    # Read a unit again that earlier reads left as a known gap. One good read clears the gap.
    retry_skipped: bool = False
    credits: list[str] = field(default_factory=list)
    # True once a step stopped because the time budget ran out. It is the only reason that
    # makes a stopped backfill a budget stall, so a source failure is never blamed on the budget.
    timed_out: bool = False
    # The boxscores this refresh already read, so one game costs one request per refresh.
    boxscores_read: set[int] = field(default_factory=set)
    # The time of the newest read of the clock, for the progress lines.
    last_stamp: datetime | None = None

    def stamp(self) -> datetime:
        """Return the time to store with a read: the clock when one is set, else ``now``."""
        self.last_stamp = self.now if self.clock is None else self.clock()
        return self.last_stamp

    def stop(self, note: str) -> None:
        """Record why a read stopped, in the notes of the refresh and in the progress step."""
        self.notes.append(note)
        progress.stopped(note)

    def step(self, kind: str, label: str, total: int) -> AbstractContextManager[progress.Step]:
        """Return the progress step of one read: ``kind`` names the work, ``label`` its range."""
        return progress.step("refresh", f"{kind}[{label}]", total=total, budget=self.seconds_left())

    def seconds_left(self) -> float | None:
        """Return the seconds of the time budget that are left, or None when there is none."""
        if self.deadline is None:
            return None
        # The last stamp is the time of the latest read. The clock is not read again here, so a
        # progress line does not change the times that a refresh stores.
        return max(0.0, (self.deadline - (self.last_stamp or self.now)).total_seconds())

    def out_of_time(self) -> bool:
        """Return True once the time budget of this refresh is spent."""
        return self.deadline is not None and self.stamp() >= self.deadline

    def skipped(self, source: str) -> frozenset[str]:
        """Return the URLs of ``source`` that this refresh skips as known gaps."""
        return frozenset() if self.retry_skipped else abandoned_urls(self.conn, source)

    def attempt(
        self,
        source: str,
        url: str,
        unit: Callable[[datetime], int],
        reading: Reading,
    ) -> bool:
        """Run one unit of work in one transaction. Return False when it wrote nothing.

        A failed unit writes nothing, adds its error to the report, and leaves a
        ``source_reads`` row with ``ok = 0``. The unit returns the rows it wrote.

        Each request that the unit makes writes one ``observations`` row with its bytes, in
        the same transaction as the rows that the unit wrote. ``reading`` names the parser, and
        says whether the bytes are kept.

        ``reading.expected_absence`` takes the failure and returns a note when the failure is a
        resource that does not exist yet. The note replaces the error, and no failed read is
        stored, so the absence never becomes a known gap. The request still writes its
        observation, with the status of the failure. The next refresh tries the unit again.
        The return value is False for an expected absence too, so a caller that counts failures
        counts it, and the callback must name one file only.
        """
        # RecordingClient refuses a source that a refresh does not record, before any request.
        stamp = self.stamp()
        capture = Capture(source, key_of(source, url), stamp, reading)
        inner = self.client
        recorded = RecordingClient(inner, source)
        self.client = recorded
        try:
            with self.conn:
                written = unit(stamp)
                record_read(self.conn, source, url, stamp)
                observed = self._observe(capture, recorded.records, None)
                if reading.derive is not None and observed:
                    reading.derive(self.conn, observed[-1])
        except SOURCE_FAILURES as exc:
            absent = reading.expected_absence(exc) if reading.expected_absence else None
            if absent is not None:
                # The request ran, so the archive keeps the row that says what it returned. Only
                # the failed read and the error are absent, so the absence never becomes a gap.
                with self.conn:
                    self._observe(capture, recorded.records, exc)
                self.notes.append(absent)
                return False
            with self.conn:
                record_failed_read(self.conn, source, url, stamp, exc)
                self._observe(capture, recorded.records, exc)
            self.errors.append(exc)
            return False
        finally:
            self.client = inner
        self.written[source] += written
        return True

    def _observe(
        self, capture: Capture, records: list[RecordedFetch], failure: Exception | None
    ) -> list[int]:
        """Write one observation for each request that the unit made, and return their ids.

        A failed unit writes them after its rollback, with the failure status and the bytes
        that did arrive, so a shape failure keeps the payload that caused it (#82).
        """
        return [record_observation(self.conn, fetch) for fetch in capture.fetches(records, failure)]

    def report(self) -> RefreshReport:
        """Return the final report of this run."""
        return RefreshReport(
            dict(self.written),
            tuple(self.errors),
            tuple(self.notes),
            tuple(self.credits),
            self.backfill_complete,
            self.timed_out,
        )
