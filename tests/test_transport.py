"""Unit tests for transport.py: API roots, requests, LRO polling.

`http_request` is faked and `time.sleep` is recorded instead of slept, so
nothing touches the network or waits.
"""

from __future__ import annotations

import pytest
from fabric_cicd import constants

from fabric_deploy.transport import fabric_api_request, fabric_url, powerbi_url


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


# --- API roots -----------------------------------------------------------


def test_urls_follow_fabric_cicd_api_roots(roots):
    assert fabric_url("/v1/workspaces") == "https://contoso.api.fabric.microsoft.com/v1/workspaces"
    assert powerbi_url("/v1.0/myorg") == "https://contoso.api.powerbi.com/v1.0/myorg"


def test_fabric_api_request_uses_the_fabric_root(monkeypatch, roots):
    calls = _fake_http(monkeypatch, lambda url: (200, {}, {"id": "1"}))
    assert fabric_api_request("/v1/workspaces") == {"id": "1"}
    assert calls == ["https://contoso.api.fabric.microsoft.com/v1/workspaces"]
