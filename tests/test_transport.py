"""Unit tests for transport.py: API roots, requests, LRO polling.

`urlopen` (or `http_request`, one layer up) is faked and `time.sleep` is
recorded instead of slept, so nothing touches the network or waits.
"""

from __future__ import annotations

import http.client

import pytest
from fabric_cicd import constants

from fabric_deploy.transport import (
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
