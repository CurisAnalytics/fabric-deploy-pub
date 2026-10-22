"""Unit tests for transport.py: API roots, requests, LRO polling.

`urlopen` (or `http_request`, one layer up) is faked and `time.sleep` is
recorded instead of slept, so nothing touches the network or waits.
"""

from __future__ import annotations

import email.utils
import http.client
import io
import urllib.error

import pytest
from fabric_cicd import constants

from fabric_deploy import transport
from fabric_deploy.transport import (
    fabric_api_list,
    fabric_api_request,
    fabric_url,
    http_request,
    poll_fabric_lro,
    powerbi_url,
)


def _headers(values: dict[str, str]) -> http.client.HTTPMessage:
    msg = http.client.HTTPMessage()
    for name, value in values.items():
        msg[name] = value
    return msg


class _Response:
    def __init__(self, status: int = 200, body: str = "", headers: dict[str, str] | None = None):
        self.status = status
        self.headers = _headers(headers or {})
        self._body = body.encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _http_error(code: int, headers: dict[str, str] | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.example/x", code, "error", _headers(headers or {}), io.BytesIO(b"{}")
    )


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr("fabric_deploy.transport.time.sleep", slept.append)
    return slept


@pytest.fixture
def urlopen(monkeypatch):
    """Serve queued responses (or raise queued exceptions) from a fake urlopen."""
    monkeypatch.setattr("fabric_deploy.transport.get_access_token", lambda _scope: "tok")

    class Fake:
        def __init__(self) -> None:
            self.queue: list[object] = []
            self.calls: list[tuple[str, float | None]] = []

        def __call__(self, req, timeout=None):
            self.calls.append((req.full_url, timeout))
            outcome = self.queue.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    fake = Fake()
    monkeypatch.setattr("fabric_deploy.transport.urllib.request.urlopen", fake)
    return fake


@pytest.fixture
def roots(monkeypatch):
    """Point fabric-cicd's API roots somewhere recognisable."""
    monkeypatch.setattr(
        constants, "FABRIC_API_ROOT_URL", "https://contoso.api.fabric.microsoft.com"
    )
    monkeypatch.setattr(constants, "DEFAULT_API_ROOT_URL", "https://contoso.api.powerbi.com")


def _fake_http(monkeypatch, handler):
    calls: list[str] = []

    def fake(url, method="GET", data=None, scope=None, error_context=""):
        calls.append(url)
        return handler(url)

    monkeypatch.setattr("fabric_deploy.transport.http_request", fake)
    return calls


# --- http_request --------------------------------------------------------


def test_header_names_are_lower_cased(urlopen):
    urlopen.queue = [_Response(202, headers={"Location": "https://op", "Retry-After": "3"})]
    _status, headers, _body = http_request("https://api.example/x", method="POST")
    assert headers == {"location": "https://op", "retry-after": "3"}


def test_empty_body_parses_to_empty_dict(urlopen):
    urlopen.queue = [_Response(200, body="  ")]
    assert http_request("https://api.example/x")[2] == {}


def test_non_json_success_body_is_a_clear_error(urlopen):
    urlopen.queue = [_Response(200, body="<html>sign in</html>")]
    with pytest.raises(RuntimeError, match=r"(?s)list things.*not JSON.*<html>sign in"):
        http_request("https://api.example/x", error_context="list things")


def test_request_sets_a_timeout(urlopen):
    urlopen.queue = [_Response(body="{}")]
    http_request("https://api.example/x")
    assert urlopen.calls == [("https://api.example/x", transport.REQUEST_TIMEOUT_S)]


def test_read_timeout_is_a_runtime_error(urlopen):
    urlopen.queue = [TimeoutError("timed out")]
    with pytest.raises(RuntimeError, match="list things: no response within"):
        http_request("https://api.example/x", error_context="list things")


@pytest.mark.parametrize("code", [429, 500, 503])
def test_retries_throttling_and_server_errors_honouring_retry_after(urlopen, sleeps, code):
    urlopen.queue = [_http_error(code, {"Retry-After": "7"}), _Response(body='{"ok": true}')]
    assert http_request("https://api.example/x", method="POST")[2] == {"ok": True}
    assert sleeps == [7]
    assert len(urlopen.calls) == 2


def test_retry_after_http_date(urlopen, sleeps, monkeypatch):
    now = 1_700_000_000
    monkeypatch.setattr("fabric_deploy.transport.time.time", lambda: now)
    when = email.utils.formatdate(now + 12, usegmt=True)
    urlopen.queue = [_http_error(429, {"Retry-After": when}), _Response(body="{}")]
    http_request("https://api.example/x")
    assert sleeps == [12]


def test_backs_off_exponentially_without_retry_after(urlopen, sleeps):
    urlopen.queue = [_http_error(503), _http_error(503), _Response(body="{}")]
    http_request("https://api.example/x")
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0] > 0


def test_retry_wait_is_capped(urlopen, sleeps):
    urlopen.queue = [_http_error(429, {"Retry-After": "86400"}), _Response(body="{}")]
    http_request("https://api.example/x")
    assert sleeps == [transport.MAX_RETRY_WAIT_S]


def test_gives_up_after_bounded_attempts(urlopen, sleeps):
    urlopen.queue = [_http_error(429, {"Retry-After": "1"}) for _ in range(transport.MAX_ATTEMPTS)]
    with pytest.raises(RuntimeError, match=r"list things \(HTTP 429\)"):
        http_request("https://api.example/x", error_context="list things")
    assert len(urlopen.calls) == transport.MAX_ATTEMPTS
    assert len(sleeps) == transport.MAX_ATTEMPTS - 1


def test_client_errors_are_not_retried(urlopen, sleeps):
    urlopen.queue = [_http_error(404)]
    with pytest.raises(RuntimeError, match=r"HTTP 404"):
        http_request("https://api.example/x")
    assert len(urlopen.calls) == 1
    assert sleeps == []


# --- API roots -----------------------------------------------------------


def test_urls_follow_fabric_cicd_api_roots(roots):
    assert fabric_url("/v1/workspaces") == "https://contoso.api.fabric.microsoft.com/v1/workspaces"
    assert powerbi_url("/v1.0/myorg") == "https://contoso.api.powerbi.com/v1.0/myorg"


def test_fabric_api_request_uses_the_fabric_root(monkeypatch, roots):
    calls = _fake_http(monkeypatch, lambda url: (200, {}, {"id": "1"}))
    assert fabric_api_request("/v1/workspaces") == {"id": "1"}
    assert calls == ["https://contoso.api.fabric.microsoft.com/v1/workspaces"]


# --- poll_fabric_lro -----------------------------------------------------


def test_poll_lro_runs_then_succeeds_and_fetches_result(monkeypatch, sleeps):
    states = iter([(200, {"retry-after": "2"}, {"status": "Running"})])

    def handler(url):
        if url.endswith("/result"):
            return 200, {}, {"value": [{"tableName": "t", "status": "Success"}]}
        return next(states, (200, {}, {"status": "Succeeded"}))

    calls = _fake_http(monkeypatch, handler)
    result = poll_fabric_lro(
        {"location": "https://op/1", "x-ms-operation-id": "op1", "retry-after": "1"}, "sync"
    )
    assert result["value"][0]["tableName"] == "t"
    assert calls == ["https://op/1", "https://op/1", fabric_url("/v1/operations/op1/result")]
    assert sleeps == [1, 2]  # the service's cadence, updated per poll


def test_poll_lro_result_fetch_failure_raises(monkeypatch, sleeps):
    """A failed result fetch must not pass off the status body as the result."""

    def handler(url):
        if url.endswith("/result"):
            raise RuntimeError("Failed to fetch sync result (HTTP 404).")
        return 200, {}, {"status": "Succeeded"}

    _fake_http(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="HTTP 404"):
        poll_fabric_lro({"location": "https://op/1", "x-ms-operation-id": "op1"}, "sync")


def test_poll_lro_without_operation_id_has_no_result(monkeypatch, sleeps):
    _fake_http(monkeypatch, lambda url: (200, {}, {"status": "Succeeded", "percentComplete": 100}))
    assert poll_fabric_lro({"location": "https://op/1"}, "sync") == {}


def test_poll_lro_accepts_http_date_retry_after(monkeypatch, sleeps):
    now = 1_700_000_000
    monkeypatch.setattr("fabric_deploy.transport.time.time", lambda: now)
    _fake_http(monkeypatch, lambda url: (200, {}, {"status": "Succeeded"}))
    when = email.utils.formatdate(now + 4, usegmt=True)
    poll_fabric_lro({"location": "https://op/1", "retry-after": when}, "sync")
    assert sleeps == [4]


def test_poll_lro_raises_on_failed(monkeypatch, sleeps):
    _fake_http(monkeypatch, lambda url: (200, {}, {"status": "Failed", "error": {"code": "x"}}))
    with pytest.raises(RuntimeError, match="Failed"):
        poll_fabric_lro({"location": "https://op/1"}, "sync")


def test_poll_lro_raises_when_no_location():
    with pytest.raises(RuntimeError, match="no Location"):
        poll_fabric_lro({}, "sync")


def test_poll_lro_times_out(monkeypatch, sleeps):
    _fake_http(monkeypatch, lambda url: (200, {}, {"status": "NotStarted"}))
    with pytest.raises(RuntimeError, match="did not finish within 3s"):
        poll_fabric_lro({"location": "https://op/1", "retry-after": "1"}, "sync", timeout_s=3)
    assert sum(sleeps) == 3


# --- fabric_api_list -----------------------------------------------------


def test_list_follows_continuation_token_and_keeps_the_query(monkeypatch):
    pages = {
        fabric_url("/v1/things?type=A"): {"value": [1], "continuationToken": "t/2+"},
        fabric_url("/v1/things?type=A&continuationToken=t/2%2B"): {
            "value": [2],
            "continuationToken": "t3",
        },
        fabric_url("/v1/things?type=A&continuationToken=t3"): {"value": [3]},
    }
    calls = _fake_http(monkeypatch, lambda url: (200, {}, pages[url]))
    assert fabric_api_list("/v1/things?type=A", "list things") == [1, 2, 3]
    assert len(calls) == 3


def test_list_without_query_sends_the_token_as_the_first_parameter(monkeypatch):
    pages = {
        fabric_url("/v1/things"): {"value": [1], "continuationToken": "t2"},
        fabric_url("/v1/things?continuationToken=t2"): {"value": []},
    }
    _fake_http(monkeypatch, lambda url: (200, {}, pages[url]))
    assert fabric_api_list("/v1/things", "list things") == [1]
