"""Capture the bytes of each request, and name the key of the resource that a URL reads."""

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING

from hockey_model.sources.errors import SourceError
from hockey_model.sources.http import HttpClient
from hockey_model.sources.sanitize import strip_market_fields
from hockey_model.sources.shape_errors import SourceParseError, SourceShapeError
from hockey_model.store.archive import (
    HTTP_ERROR,
    OK,
    SHAPE_ERROR,
    STORE_ERROR,
    Fetch,
    canonical_bytes,
    is_market_source,
)
from hockey_model.store.source_reads import BOXSCORES, SCORES

if TYPE_CHECKING:
    from collections.abc import Callable

_SCORE_DAY = re.compile(r"/([0-9]{4}-[0-9]{2}-[0-9]{2})$")
_BOXSCORE = re.compile(r"/gamecenter/([0-9]{1,10})/boxscore$")


@dataclass(frozen=True, slots=True)
class RecordedFetch:
    """One request that a unit of work made, with its outcome."""

    url: str
    status: str
    body: bytes | None
    error: str | None


class RecordingClient(HttpClient):
    """Serve every request through an inner client, and keep each request's outcome.

    ``records`` holds one entry for each request that the unit made, in call order, so a
    request that failed still reaches the archive with the stage that failed.

    A payload of a market source loses its market fields before it reaches the unit and the
    archive, so no price data reaches the store (#82). The source name decides that, so a
    caller cannot forget it.
    """

    def __init__(self, inner: HttpClient, source: str) -> None:
        super().__init__()
        self._inner = inner
        self._sanitize = is_market_source(source)
        self.records: list[RecordedFetch] = []

    def get(self, source: str, url: str) -> bytes:
        """Fetch ``url`` through the inner client, and keep the outcome."""
        try:
            body = self._inner.get(source, url)
        except SourceError as exc:
            self.records.append(RecordedFetch(url, HTTP_ERROR, None, str(exc)))
            raise
        body = self._cleaned(body)
        self.records.append(RecordedFetch(url, OK, body, None))
        return body

    def _cleaned(self, body: bytes) -> bytes:
        """Return ``body`` without its market fields. A body that is not JSON is unchanged."""
        if not self._sanitize:
            return body
        try:
            payload = json.loads(body)
        except ValueError, RecursionError:
            # The parser owns the error for a body that is not JSON, and names the URL there.
            return body
        return canonical_bytes(strip_market_fields(payload))


@dataclass(frozen=True, slots=True)
class Reading:
    """How one unit of work is read and archived.

    ``parser`` names the code that reads the payload, as ``<name>:<version>``. A parser change
    that can change a parsed row gets a new version. ``keep_body`` is False for a file that the
    store keeps at the row level only, so its observation says that the fetch ran and no bytes
    of it are kept. ``expected_absence`` turns a failure into a note when the resource does not
    exist yet.
    """

    parser: str
    expected_absence: Callable[[Exception], str | None] | None = None
    keep_body: bool = True


# The reading of a unit that names no parser. ``attempt`` uses it so an existing caller keeps
# its behavior, and the observation says that no parser read the payload.
DEFAULT_READING = Reading(parser="unnamed:0")


@dataclass(frozen=True, slots=True)
class Capture:
    """The facts that every request of one unit shares: its source, key, time, and reading."""

    source: str
    key: str | None
    stamp: datetime
    reading: Reading

    def fetches(self, records: list[RecordedFetch], failure: Exception | None) -> list[Fetch]:
        """Return one ``Fetch`` for each request that the unit made.

        A request that failed keeps its own status and error, so a good response beside a
        failed one stays ``ok``. ``failure`` names a unit that raised after its requests, and
        ``_outcome`` says which record it belongs to.
        """
        return [self._fetch(record, failure) for record in records]

    def _fetch(self, record: RecordedFetch, failure: Exception | None) -> Fetch:
        """Return the ``Fetch`` to store for ``record``, with the failure that belongs to it."""
        status, error = self._outcome(record, failure)
        return Fetch(
            self.source,
            record.url,
            self.stamp,
            status,
            self.reading.parser,
            key=self.key,
            body=record.body if self.reading.keep_body else None,
            error=error,
        )

    def _outcome(self, record: RecordedFetch, failure: Exception | None) -> tuple[str, str | None]:
        """Return the status and error to store for ``record``.

        A parser failure rejected the bytes that arrived, so it marks each kept body. A source
        failure came from one request, and ``SourceError`` names it, so only that request takes
        the failure. A request that already failed keeps its own outcome.
        """
        if record.status != OK or failure is None:
            return record.status, record.error
        status = status_of(failure)
        if status in (SHAPE_ERROR, STORE_ERROR):
            return status, str(failure)
        if isinstance(failure, SourceError) and failure.url == record.url:
            return status, str(failure)
        return record.status, record.error


def status_of(failure: Exception) -> str:
    """Return the archive status that names the stage of ``failure``."""
    if isinstance(failure, SourceShapeError | SourceParseError):
        return SHAPE_ERROR
    return HTTP_ERROR if isinstance(failure, SourceError) else STORE_ERROR


def key_of(source: str, url: str) -> str | None:
    """Return the key of the resource that ``url`` reads, or ``None`` when it has none.

    A boxscore is keyed on its game and a score day on its date. A live feed is one URL with
    one current value, so its observations carry no key.
    """
    if source == SCORES:
        found = _SCORE_DAY.search(url)
        # A date that the pattern accepts can still name no day, such as 2026-13-45.
        return None if found is None or not _is_date(found[1]) else found[1]
    if source == BOXSCORES:
        found = _BOXSCORE.search(url)
        return None if found is None else found[1]
    return None


def _is_date(text: str) -> bool:
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True
