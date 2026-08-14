"""Unit tests for api.py: item lookup, pagination, LRO/refresh polling, scopes.

Network is mocked at the `http_request` / `fabric_api_request` seam and
`time.sleep` is stubbed, so these run with no Fabric access and no real waits.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from fabric_deploy.client import (
    find_item_by_name,
    get_sqldatabase_server_host,
    list_workspace_items,
    refresh_semantic_model,
)
from fabric_deploy.identity import POWERBI_SCOPE, get_access_token
from fabric_deploy.transport import poll_fabric_lro


def _jwt(exp_offset: int = 3600) -> str:
    """A syntactically valid unsigned JWT with exp = now + offset."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time()) + exp_offset}).encode()
    ).rstrip(b"=")
    return f"{header.decode()}.{payload.decode()}.sig"


# --- list_workspace_items pagination -------------------------------------


def test_list_workspace_items_follows_continuation_token(monkeypatch):
    pages = [
        {
            "value": [{"id": "1", "displayName": "a", "type": "SemanticModel"}],
            "continuationToken": "tok2",
        },
        {"value": [{"id": "2", "displayName": "b", "type": "SemanticModel"}]},
    ]
    seen: list[str] = []

    def fake_request(endpoint, error_context="x"):
        seen.append(endpoint)
        return pages[len(seen) - 1]

    monkeypatch.setattr("fabric_deploy.client.fabric_api_request", fake_request)
    items = list_workspace_items("ws", "SemanticModel")

    assert [i["id"] for i in items] == ["1", "2"]  # both pages merged
    assert "type=SemanticModel" in seen[0]
    assert "continuationToken=tok2" in seen[1]
    assert "type=SemanticModel" in seen[1]  # filter preserved across pages


# --- find_item_by_name ---------------------------------------------------


def _items(monkeypatch, items):
    monkeypatch.setattr(
        "fabric_deploy.client.list_workspace_items", lambda ws, t: items
    )


def test_find_item_single_when_no_name(monkeypatch):
    _items(monkeypatch, [{"id": "1", "displayName": "Only"}])
    assert find_item_by_name("ws", "SemanticModel")["id"] == "1"


def test_find_item_by_name_case_insensitive(monkeypatch):
    _items(monkeypatch, [{"id": "1", "displayName": "A"}, {"id": "2", "displayName": "B"}])
    assert find_item_by_name("ws", "SemanticModel", "b")["id"] == "2"


def test_find_item_none_raises_with_listing(monkeypatch):
    _items(monkeypatch, [{"id": "1", "displayName": "A"}])
    with pytest.raises(ValueError, match="No SemanticModel"):
        find_item_by_name("ws", "SemanticModel", "Ghost")


def test_find_item_ambiguous_raises(monkeypatch):
    _items(monkeypatch, [{"id": "1", "displayName": "A"}, {"id": "2", "displayName": "B"}])
    with pytest.raises(ValueError, match="Multiple SemanticModel"):
        find_item_by_name("ws", "SemanticModel")


# --- poll_fabric_lro ----------------------------------------------------


def test_poll_lro_runs_then_succeeds_and_fetches_result(monkeypatch):
    monkeypatch.setattr("fabric_deploy.transport.time.sleep", lambda _s: None)
    state = {"polls": 0}

    def fake_http(url, method="GET", data=None, scope=None, error_context=""):
        if url.endswith("/result"):
            return 200, {}, {"value": [{"tableName": "t", "status": "Success"}]}
        state["polls"] += 1
        if state["polls"] == 1:
            return 200, {"Retry-After": "1"}, {"status": "Running"}
        return 200, {}, {"status": "Succeeded"}

    monkeypatch.setattr("fabric_deploy.transport.http_request", fake_http)
    result = poll_fabric_lro(
        {"Location": "https://x/op", "x-ms-operation-id": "op1", "Retry-After": "1"},
        "sync",
    )
    assert result["value"][0]["tableName"] == "t"
    assert state["polls"] == 2  # polled through the Running state


def test_poll_lro_raises_on_failed(monkeypatch):
    monkeypatch.setattr("fabric_deploy.transport.time.sleep", lambda _s: None)

    def fake_http(url, method="GET", data=None, scope=None, error_context=""):
        return 200, {}, {"status": "Failed", "error": {"message": "boom"}}

    monkeypatch.setattr("fabric_deploy.transport.http_request", fake_http)
    with pytest.raises(RuntimeError, match="Failed"):
        poll_fabric_lro({"Location": "https://x/op", "Retry-After": "1"}, "sync")


def test_poll_lro_raises_when_no_location():
    with pytest.raises(RuntimeError, match="no Location"):
        poll_fabric_lro({}, "sync")


# --- refresh_semantic_model poll loop ------------------------------------


def test_refresh_semantic_model_polls_until_completed(monkeypatch):
    monkeypatch.setattr("fabric_deploy.client.time.sleep", lambda _s: None)
    gets = {"n": 0}

    def fake_http(url, method="GET", data=None, scope=None, error_context=""):
        if method == "POST":
            return 202, {"Location": "https://api.powerbi.com/r/req1"}, {}
        gets["n"] += 1
        if gets["n"] == 1:
            return 200, {}, {"status": "Unknown"}  # no endTime -> in progress
        return 200, {}, {"status": "Completed"}

    monkeypatch.setattr("fabric_deploy.client.http_request", fake_http)
    detail = refresh_semantic_model("ws", "ds", timeout_s=100, poll_s=1)
    assert detail["status"] == "Completed"
    assert gets["n"] == 2


# --- get_access_token scope fallbacks ------------------------------------


@pytest.mark.parametrize("set_powerbi", [True, False])
def test_powerbi_scope_token_resolution(monkeypatch, set_powerbi: bool):
    """Power BI scope prefers POWERBI_BEARER_TOKEN, else falls back to Fabric's."""
    pbi, fabric = _jwt(), _jwt()
    monkeypatch.setenv("FABRIC_BEARER_TOKEN", fabric)
    if set_powerbi:
        monkeypatch.setenv("POWERBI_BEARER_TOKEN", pbi)
    else:
        monkeypatch.delenv("POWERBI_BEARER_TOKEN", raising=False)
    assert get_access_token(POWERBI_SCOPE) == (pbi if set_powerbi else fabric)


# --- get_sqldatabase_server_host ------------------------------------------


def _sqldbs(monkeypatch, value):
    monkeypatch.setattr(
        "fabric_deploy.client.fabric_api_request",
        lambda endpoint, error_context="x": {"value": value},
    )


def test_sqldatabase_host_strips_port_suffix(monkeypatch):
    _sqldbs(
        monkeypatch,
        [{
            "displayName": "mydb",
            "properties": {"serverFqdn": "abc-def.database.fabric.microsoft.com,1433"},
        }],
    )
    assert (
        get_sqldatabase_server_host("ws", "mydb")
        == "abc-def.database.fabric.microsoft.com"
    )


def test_sqldatabase_host_portless_fqdn_unchanged(monkeypatch):
    _sqldbs(
        monkeypatch,
        [{
            "displayName": "MyDB",
            "properties": {"serverFqdn": "abc-def.database.fabric.microsoft.com"},
        }],
    )
    # also exercises case-insensitive name matching
    assert (
        get_sqldatabase_server_host("ws", "mydb")
        == "abc-def.database.fabric.microsoft.com"
    )


def test_sqldatabase_host_no_match_raises_with_listing(monkeypatch):
    _sqldbs(monkeypatch, [{"displayName": "other", "properties": {}}])
    with pytest.raises(ValueError, match="No SQLDatabase matching 'mydb'"):
        get_sqldatabase_server_host("ws", "mydb")


def test_sqldatabase_host_missing_fqdn_raises(monkeypatch):
    _sqldbs(monkeypatch, [{"displayName": "mydb", "properties": {}}])
    with pytest.raises(ValueError, match="no serverFqdn"):
        get_sqldatabase_server_host("ws", "mydb")
