"""Polite HTTP fetches: one request at a time per host, with bounded retries."""

import logging
import re
import time
import urllib.request
from collections.abc import Callable
from http.client import HTTPException, HTTPMessage
from importlib.metadata import PackageNotFoundError, version
from io import BufferedIOBase
from typing import IO
from urllib.error import HTTPError
from urllib.parse import urlsplit

from hockey_model.sources.errors import SourceError
from hockey_model.sources.policy import HOST_POLICIES, HostPolicy
from hockey_model.types import HockeyModelError

Opener = Callable[[urllib.request.Request, float], bytes]

_LOG = logging.getLogger(__name__)
TIMEOUT_S = 30.0
_CHUNK_BYTES = 64 * 1024
MAX_BACKOFF_S = 30.0
# ASCII digits only. str.isdigit() also accepts characters that float() rejects.
_RETRY_AFTER = re.compile(r"[0-9]{1,6}")


# The statuses that a retry cannot change. A 404 may turn into a 200 once the resource is
# published, and a 408 or a 425 asks the client to try again, so none of the three is here.
PERMANENT_STATUSES = frozenset({400, 401, 403, 410, 451})


class _Refused(Exception):
    """A response the client will not accept: a redirect off the allowlist, or too large.

    ``permanent`` is False when a later read can succeed, such as a body that was too slow.
    """

    def __init__(self, message: str, *, permanent: bool = True) -> None:
        super().__init__(message)
        self.permanent = permanent


class _PolicyRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to a host on the allowlist, over https."""

    def redirect_request(  # noqa: PLR0913  # the base class fixes this signature
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        try:
            _policy_for(newurl)
        except (ValueError, HockeyModelError) as exc:
            raise _Refused(f"redirect to {newurl!r} is not allowed: {exc}") from exc
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_PolicyRedirect)


def _default_opener(
    request: urllib.request.Request,
    timeout: float,
    clock: Callable[[], float] = time.monotonic,
) -> bytes:
    policy = _policy_for(request.full_url)
    deadline = clock() + policy.total_timeout_s
    with _OPENER.open(request, timeout=timeout) as response:
        return _read_bounded(response, deadline, clock, policy)


def _read_bounded(
    response: BufferedIOBase, deadline: float, clock: Callable[[], float], policy: HostPolicy
) -> bytes:
    """Read the body in chunks.

    The socket timeout bounds one read, so the deadline bounds the total. ``read1`` returns
    after one socket read, where ``read`` keeps reading until it has a whole chunk, so a
    host that sends one byte at a time cannot hold the loop past the deadline.
    """
    chunks: list[bytes] = []
    size = 0
    while clock() < deadline:
        chunk = response.read1(_CHUNK_BYTES)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > policy.max_body_bytes:
            raise _Refused(
                f"response is larger than {policy.max_body_bytes} bytes; "
                "raise max_body_bytes in HOST_POLICIES"
            )
        chunks.append(chunk)
    raise _Refused(
        f"response body not finished within {policy.total_timeout_s:g} s; "
        "raise total_timeout_s in HOST_POLICIES",
        permanent=False,
    )


def _policy_for(url: str) -> HostPolicy:
    """Return the host policy for ``url``, or raise ``ValueError`` naming the fix."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError(f"fetch {url!r}: scheme {parts.scheme!r} is not https; use https")
    host = parts.hostname or ""
    policy = HOST_POLICIES.get(host)
    if policy is None:
        raise ValueError(f"fetch {url!r}: no host policy for {host!r}; add it to HOST_POLICIES")
    return policy


def _user_agent() -> str:
    try:
        package_version = version("hockey-model")
    except PackageNotFoundError as exc:
        raise HockeyModelError(
            "read package version: hockey-model is not installed; run uv sync"
        ) from exc
    return f"hockey-model/{package_version}"


def _is_retryable_status(code: int) -> bool:
    return code == 429 or code >= 500


def _backoff_s(attempt: int, retry_after: str | None) -> float:
    if retry_after is not None and _RETRY_AFTER.fullmatch(retry_after.strip()):
        return min(float(retry_after.strip()), MAX_BACKOFF_S)
    return min(2.0**attempt, MAX_BACKOFF_S)


class HttpClient:
    """Fetch URLs from known hosts, spacing requests and retrying transient failures."""

    def __init__(
        self,
        opener: Opener | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._opener: Opener = _default_opener if opener is None else opener
        self._clock = clock
        self._sleep = sleep
        self._last_start: dict[str, float] = {}

    def get(self, source: str, url: str) -> bytes:
        """Return the response body, or raise ``SourceError`` once retries run out.

        Raises ``ValueError`` for a URL that is not https or whose host has no policy.
        """
        policy = _policy_for(url)
        host = urlsplit(url).hostname or ""
        request = urllib.request.Request(url, headers={"User-Agent": _user_agent()})
        attempts = policy.max_retries + 1
        reason = "no attempt made"
        for attempt in range(attempts):
            self._wait_for_host(host, policy.min_interval_s)
            try:
                return self._opener(request, TIMEOUT_S)
            except HTTPError as exc:
                exc.close()
                if not _is_retryable_status(exc.code):
                    raise SourceError(
                        source, url, f"HTTP {exc.code}", permanent=exc.code in PERMANENT_STATUSES
                    ) from exc
                reason = f"HTTP {exc.code}"
                delay = _backoff_s(attempt, exc.headers.get("Retry-After"))
            except _Refused as exc:
                raise SourceError(source, url, str(exc), permanent=exc.permanent) from exc
            except (OSError, HTTPException) as exc:
                reason = f"network error {exc}"
                delay = _backoff_s(attempt, None)
            if attempt < attempts - 1:
                _LOG.debug("retry %s %s after attempt %d: %s", source, url, attempt + 1, reason)
                self._sleep(delay)
        raise SourceError(source, url, f"{reason} after {attempts} attempts")

    def _wait_for_host(self, host: str, min_interval_s: float) -> None:
        last = self._last_start.get(host)
        if last is not None:
            remaining = min_interval_s - (self._clock() - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last_start[host] = self._clock()
