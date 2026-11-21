"""Tests for the polite HTTP client. No test touches the network."""

import http.client
import io
import socketserver
import threading
import time
import urllib.request
from collections.abc import Callable
from email.message import Message
from http.client import HTTPMessage, IncompleteRead
from urllib.error import HTTPError, URLError

import pytest
from hypothesis import given
from hypothesis import strategies as st

import hockey_model.sources.http as http_module
from hockey_model.sources.errors import SourceError
from hockey_model.sources.http import (
    PERMANENT_STATUSES,
    HttpClient,
    _is_retryable_status,
    _PolicyRedirect,
    _Refused,
)
from hockey_model.sources.policy import HOST_POLICIES, HostPolicy

NHL_URL = "https://api-web.nhle.com/v1/schedule/now"


class FakeClock:
    """A clock that advances only when the fake sleep runs."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ScriptedOpener:
    """Return or raise the scripted outcomes in order, and record each request."""

    def __init__(self, outcomes: list[bytes | BaseException]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> bytes:
        del timeout
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _http_error(code: int, retry_after: str | None = None) -> HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError(NHL_URL, code, f"status {code}", headers, io.BytesIO())


def _client(opener: ScriptedOpener, clock: FakeClock) -> HttpClient:
    return HttpClient(opener=opener, clock=clock, sleep=clock.sleep)


def test_get_sends_user_agent() -> None:
    opener = ScriptedOpener([b"ok"])
    _client(opener, FakeClock()).get("nhl", NHL_URL)
    user_agent = opener.requests[0].get_header("User-agent")
    assert user_agent is not None
    assert user_agent.startswith("hockey-model/")


def test_get_returns_body() -> None:
    opener = ScriptedOpener([b"payload"])
    assert _client(opener, FakeClock()).get("nhl", NHL_URL) == b"payload"


def test_get_spaces_requests_per_host() -> None:
    clock = FakeClock(now=0.0)
    opener = ScriptedOpener([b"a", b"b"])
    client = _client(opener, clock)
    client.get("nhl", NHL_URL)
    clock.now = 0.1
    client.get("nhl", NHL_URL)
    assert clock.sleeps == [pytest.approx(0.4)]


def test_get_retries_429_with_retry_after() -> None:
    clock = FakeClock()
    opener = ScriptedOpener([_http_error(429, retry_after="7"), b"body"])
    assert _client(opener, clock).get("nhl", NHL_URL) == b"body"
    assert 7.0 in clock.sleeps


@pytest.mark.parametrize("retry_after", ["86400", "999999"])
def test_retry_after_is_capped(retry_after: str) -> None:
    clock = FakeClock()
    opener = ScriptedOpener([_http_error(429, retry_after=retry_after), b"body"])
    assert _client(opener, clock).get("nhl", NHL_URL) == b"body"
    assert clock.sleeps == [http_module.MAX_BACKOFF_S]


@pytest.mark.parametrize("retry_after", ["²", "soon", "-3", "9" * 400])
def test_malformed_retry_after_falls_back_to_backoff(retry_after: str) -> None:
    clock = FakeClock()
    opener = ScriptedOpener([_http_error(503, retry_after=retry_after), b"body"])
    assert _client(opener, clock).get("nhl", NHL_URL) == b"body"
    assert clock.sleeps == [1.0]


def test_get_raises_after_last_retry() -> None:
    clock = FakeClock()
    opener = ScriptedOpener([_http_error(503)] * 4)
    with pytest.raises(SourceError, match="503") as info:
        _client(opener, clock).get("nhl", NHL_URL)
    assert info.value.source == "nhl"
    assert info.value.url == NHL_URL
    assert len(opener.requests) == 4


def test_get_retries_network_error_then_succeeds() -> None:
    clock = FakeClock()
    opener = ScriptedOpener([URLError("reset"), b"body"])
    assert _client(opener, clock).get("nhl", NHL_URL) == b"body"
    assert 1.0 in clock.sleeps


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("read timed out"), ConnectionResetError("reset"), IncompleteRead(b"")],
)
def test_get_retries_failure_while_reading_body(failure: BaseException) -> None:
    clock = FakeClock()
    opener = ScriptedOpener([failure, b"body"])
    assert _client(opener, clock).get("nhl", NHL_URL) == b"body"


def test_get_wraps_read_failure_after_last_retry() -> None:
    clock = FakeClock()
    opener = ScriptedOpener([TimeoutError("slow")] * 4)
    with pytest.raises(SourceError, match="network error"):
        _client(opener, clock).get("nhl", NHL_URL)


def test_get_raises_on_404_without_retry() -> None:
    clock = FakeClock()
    opener = ScriptedOpener([_http_error(404)])
    with pytest.raises(SourceError, match="404"):
        _client(opener, clock).get("nhl", NHL_URL)
    assert len(opener.requests) == 1


@pytest.mark.parametrize("code", sorted(PERMANENT_STATUSES))
def test_an_error_status_that_a_retry_cannot_change_is_permanent(code: int) -> None:
    opener = ScriptedOpener([_http_error(code)])
    with pytest.raises(SourceError) as info:
        _client(opener, FakeClock()).get("nhl", NHL_URL)
    assert info.value.permanent is True
    assert len(opener.requests) == 1


@pytest.mark.parametrize("code", [404, 408, 425])
def test_a_status_that_a_later_read_may_change_is_not_permanent(code: int) -> None:
    opener = ScriptedOpener([_http_error(code)])
    with pytest.raises(SourceError) as info:
        _client(opener, FakeClock()).get("nhl", NHL_URL)
    assert info.value.permanent is False


def test_the_permanent_statuses_are_client_errors_that_the_client_does_not_retry() -> None:
    assert all(400 <= code < 500 and not _is_retryable_status(code) for code in PERMANENT_STATUSES)


@pytest.mark.parametrize("permanent", [True, False])
def test_a_refusal_keeps_its_permanence_in_the_source_error(permanent: bool) -> None:
    opener = ScriptedOpener([_Refused("refused", permanent=permanent)])
    with pytest.raises(SourceError) as info:
        _client(opener, FakeClock()).get("nhl", NHL_URL)
    assert info.value.permanent is permanent


def test_a_refusal_is_permanent_by_default() -> None:
    assert _Refused("redirect").permanent is True


def test_a_failure_that_ran_out_of_retries_is_not_permanent() -> None:
    opener = ScriptedOpener([_http_error(503)] * 4)
    with pytest.raises(SourceError) as info:
        _client(opener, FakeClock()).get("nhl", NHL_URL)
    assert info.value.permanent is False


def test_a_network_error_is_not_permanent() -> None:
    opener = ScriptedOpener([OSError("down")] * 4)
    with pytest.raises(SourceError) as info:
        _client(opener, FakeClock()).get("nhl", NHL_URL)
    assert info.value.permanent is False


def test_a_source_error_is_transient_unless_it_says_otherwise() -> None:
    assert SourceError("nhl", NHL_URL, "x").permanent is False
    assert SourceError("nhl", NHL_URL, "x", permanent=True).permanent is True


def test_get_rejects_unknown_host_before_any_request() -> None:
    opener = ScriptedOpener([])
    with pytest.raises(ValueError, match="no host policy") as info:
        _client(opener, FakeClock()).get("example", "https://example.com/x")
    assert "add it to HOST_POLICIES" in str(info.value)
    assert opener.requests == []


@pytest.mark.parametrize("url", ["http://api-web.nhle.com/x", "ftp://api-web.nhle.com/x"])
def test_get_rejects_non_https_before_any_request(url: str) -> None:
    opener = ScriptedOpener([])
    with pytest.raises(ValueError, match="not https"):
        _client(opener, FakeClock()).get("nhl", url)
    assert opener.requests == []


def test_redirect_to_unlisted_host_is_refused() -> None:
    handler = _PolicyRedirect()
    request = urllib.request.Request(NHL_URL)
    with pytest.raises(_Refused, match="not allowed"):
        handler.redirect_request(
            request, io.BytesIO(), 302, "Found", HTTPMessage(), "https://evil.test/x"
        )


def test_redirect_to_listed_https_host_is_followed() -> None:
    handler = _PolicyRedirect()
    request = urllib.request.Request(NHL_URL)
    redirected = handler.redirect_request(
        request, io.BytesIO(), 302, "Found", HTTPMessage(), "https://api-web.nhle.com/v2/x"
    )
    assert redirected is not None
    assert redirected.full_url == "https://api-web.nhle.com/v2/x"


class _OversizeResponse:
    def __enter__(self) -> _OversizeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read1(self, size: int = -1) -> bytes:
        return b"x" * size


class _OversizeOpener:
    def open(self, request: urllib.request.Request, timeout: float) -> _OversizeResponse:
        del request, timeout
        return _OversizeResponse()


def test_default_opener_refuses_oversize_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http_module, "_OPENER", _OversizeOpener())
    request = urllib.request.Request(NHL_URL)
    with pytest.raises(_Refused, match="larger than"):
        http_module._default_opener(request, 1.0)


def test_source_error_message_names_operation_and_reason() -> None:
    error = SourceError("nhl", NHL_URL, "HTTP 404")
    assert str(error) == f"fetch nhl {NHL_URL}: HTTP 404"


class _TricklingResponse:
    """A response that returns one byte per read and takes one second for each."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.reads = 0

    def __enter__(self) -> _TricklingResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read1(self, size: int = -1) -> bytes:
        del size
        self.reads += 1
        self.clock.now += 1.0
        return b"x"


class _TricklingOpener:
    def __init__(self, response: _TricklingResponse) -> None:
        self.response = response

    def open(self, request: urllib.request.Request, timeout: float) -> _TricklingResponse:
        del request, timeout
        return self.response


def test_default_opener_bounds_total_time_of_a_trickling_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    response = _TricklingResponse(clock)
    monkeypatch.setattr(http_module, "_OPENER", _TricklingOpener(response))
    request = urllib.request.Request(NHL_URL)
    with pytest.raises(_Refused, match=r"not finished within 120 s"):
        http_module._default_opener(request, 1.0, clock=clock)
    assert response.reads <= HOST_POLICIES["api-web.nhle.com"].total_timeout_s + 1


def test_trickling_body_surfaces_as_source_error(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    monkeypatch.setattr(http_module, "_OPENER", _TricklingOpener(_TricklingResponse(clock)))

    def opener(request: urllib.request.Request, timeout: float) -> bytes:
        return http_module._default_opener(request, timeout, clock=clock)

    client = HttpClient(opener=opener, clock=clock, sleep=clock.sleep)
    with pytest.raises(SourceError, match="not finished within"):
        client.get("nhl", NHL_URL)


_HOSTS = {
    "api-web.nhle.com": "https://api-web.nhle.com/v1/x",
    "site.api.espn.com": "https://site.api.espn.com/apis/x",
    "www.dailyfaceoff.com": "https://www.dailyfaceoff.com/x",
}
_MAX_TRIES = HOST_POLICIES["api-web.nhle.com"].max_retries + 1


@given(
    st.lists(
        st.tuples(st.sampled_from(sorted(_HOSTS)), st.floats(min_value=0.0, max_value=10.0)),
        min_size=1,
        max_size=30,
    )
)
def test_property_requests_to_one_host_start_min_interval_apart(
    steps: list[tuple[str, float]],
) -> None:
    clock = FakeClock()
    starts: dict[str, list[float]] = {host: [] for host in _HOSTS}
    current_host = ""

    def opener(request: urllib.request.Request, timeout: float) -> bytes:
        del request, timeout
        starts[current_host].append(clock.now)
        return b"ok"

    client = HttpClient(opener=opener, clock=clock, sleep=clock.sleep)
    for host, gap in steps:
        clock.now += gap
        current_host = host
        client.get("src", _HOSTS[host])
    for host, times in starts.items():
        interval = HOST_POLICIES[host].min_interval_s
        assert all(b - a >= interval - 1e-9 for a, b in zip(times, times[1:], strict=False))


_OUTCOMES: dict[str, Callable[[], bytes | BaseException]] = {
    "ok": lambda: b"ok",
    "503": lambda: _http_error(503),
    "429": lambda: _http_error(429),
    "404": lambda: _http_error(404),
    "net": lambda: URLError("down"),
}


@given(st.lists(st.sampled_from(sorted(_OUTCOMES)), min_size=_MAX_TRIES, max_size=8))
def test_property_retries_stop_at_first_final_outcome_or_the_limit(names: list[str]) -> None:
    clock = FakeClock()
    expected_calls = _MAX_TRIES
    expect_body = False
    for index, name in enumerate(names[:_MAX_TRIES]):
        if name in {"ok", "404"}:
            expected_calls, expect_body = index + 1, name == "ok"
            break
    opener = ScriptedOpener([_OUTCOMES[name]() for name in names[:expected_calls]])
    client = _client(opener, clock)
    if expect_body:
        assert client.get("nhl", NHL_URL) == b"ok"
    else:
        with pytest.raises(SourceError):
            client.get("nhl", NHL_URL)
    assert len(opener.requests) == expected_calls
    assert all(0.0 < pause <= http_module.MAX_BACKOFF_S for pause in clock.sleeps)


@given(st.integers(min_value=0, max_value=999_999), st.sampled_from(["", " ", "\t", "\n "]))
def test_property_retry_after_digits_are_used_and_capped(seconds: int, pad: str) -> None:
    result = http_module._backoff_s(0, f"{pad}{seconds}{pad}")
    assert result == min(float(seconds), http_module.MAX_BACKOFF_S)


def _is_plain_seconds(text: str) -> bool:
    stripped = text.strip()
    return stripped.isascii() and stripped.isdigit() and len(stripped) <= 6


@given(
    st.integers(min_value=0, max_value=60),
    st.none()
    | st.text(max_size=12).filter(lambda text: not _is_plain_seconds(text))
    | st.integers(min_value=1_000_000).map(str),
)
def test_property_other_retry_after_text_falls_back_to_exponential(
    attempt: int, header: str | None
) -> None:
    result = http_module._backoff_s(attempt, header)
    assert result == min(2.0**attempt, http_module.MAX_BACKOFF_S)


def test_read_bounded_refuses_body_over_host_cap() -> None:
    policy = HostPolicy(min_interval_s=0.0, max_retries=0, max_body_bytes=10)
    with pytest.raises(_Refused, match="larger than 10 bytes"):
        http_module._read_bounded(io.BytesIO(b"x" * 11), float("inf"), lambda: 0.0, policy)
    body = http_module._read_bounded(io.BytesIO(b"x" * 10), float("inf"), lambda: 0.0, policy)
    assert body == b"x" * 10


def test_moneypuck_host_allows_its_large_csv() -> None:
    assert HOST_POLICIES["moneypuck.com"].max_body_bytes > 200 * 1024 * 1024
    assert (
        HOST_POLICIES["moneypuck.com"].total_timeout_s
        > HOST_POLICIES["api-web.nhle.com"].total_timeout_s
    )


class _TrickleHandler(socketserver.BaseRequestHandler):
    """Send a response header that promises a large body, then one byte at a time."""

    def handle(self) -> None:
        self.request.recv(4096)
        self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
        try:
            for _ in range(200):
                self.request.sendall(b"x")
                time.sleep(0.05)
        except OSError:
            return


def test_a_real_trickling_socket_is_cut_off_at_the_deadline() -> None:
    with socketserver.TCPServer(("127.0.0.1", 0), _TrickleHandler) as server:
        threading.Thread(target=server.handle_request, daemon=True).start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        connection.request("GET", "/")
        response = connection.getresponse()
        policy = HostPolicy(min_interval_s=0.0, max_retries=0, total_timeout_s=0.5)
        started = time.monotonic()
        with pytest.raises(_Refused, match="not finished within"):
            http_module._read_bounded(response, started + 0.5, time.monotonic, policy)
        assert time.monotonic() - started < 3.0
        connection.close()


def test_a_source_error_keeps_its_parts() -> None:
    error = SourceError("nhl", NHL_URL, "HTTP 404")
    assert (error.source, error.url, error.reason) == ("nhl", NHL_URL, "HTTP 404")
