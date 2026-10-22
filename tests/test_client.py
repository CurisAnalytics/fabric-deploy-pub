"""Unit tests for client.py: workspace, capacity and item lookup, pagination,
refresh polling, model definitions and API roots.

Network is mocked at the `http_request` / `fabric_api_list` seams and
`time.sleep` is stubbed, so these run with no Fabric access and no real waits.
"""

from __future__ import annotations

import base64

import pytest
from fabric_cicd import constants

from fabric_deploy.client import (
    find_item_by_name,
    get_default_capacity_id,
    get_semantic_model_definition,
    get_sqldatabase_server_host,
    get_workspace_id_by_name,
    list_capacities,
    list_workspace_items,
    list_workspaces,
    refresh_semantic_model,
    refresh_sql_endpoint_metadata,
)

# --- pagination ------------------------------------------------------------


def _two_pages(monkeypatch) -> list[str]:
    """Serve every list endpoint as two pages; return the requested URLs."""
    seen: list[str] = []

    def fake_http(url, method="GET", data=None, scope=None, error_context="", idempotent=False):
        seen.append(url)
        if "continuationToken=" in url:
            return 200, {}, {"value": [{"id": "2", "displayName": "b"}]}
        return 200, {}, {"value": [{"id": "1", "displayName": "a"}], "continuationToken": "tok2"}

    monkeypatch.setattr("fabric_deploy.transport.http_request", fake_http)
    return seen


@pytest.mark.parametrize(
    "list_all", [list_workspaces, list_capacities, lambda: list_workspace_items("ws")]
)
def test_list_calls_follow_every_page(monkeypatch, list_all):
    """A workspace or capacity on page 2+ would otherwise resolve as "not found"."""
    _two_pages(monkeypatch)
    assert [i["id"] for i in list_all()] == ["1", "2"]


def test_list_workspace_items_keeps_the_type_filter_across_pages(monkeypatch):
    seen = _two_pages(monkeypatch)
    list_workspace_items("ws", "SemanticModel")
    assert all("type=SemanticModel" in url for url in seen)
    assert "continuationToken=tok2" in seen[1]


# --- get_default_capacity_id -----------------------------------------------


def _capacity(name: str, state: str = "Active") -> dict:
    return {"id": f"id-{name}", "displayName": name, "sku": "F2", "state": state}


def _capacities(monkeypatch, capacities: list[dict]) -> None:
    monkeypatch.setattr("fabric_deploy.client.list_capacities", lambda: capacities)


def test_default_capacity_is_the_only_active_one(monkeypatch, caplog):
    _capacities(monkeypatch, [_capacity("Paused", state="Inactive"), _capacity("Contoso F2")])
    with caplog.at_level("INFO", logger="fabric_deploy"):
        assert get_default_capacity_id() == "id-Contoso F2"
    assert "Contoso F2" in caplog.text


def test_default_capacity_refuses_to_pick_among_several(monkeypatch):
    _capacities(monkeypatch, [_capacity("Contoso F2"), _capacity("Fabrikam F4")])
    with pytest.raises(ValueError, match="capacity_id") as err:
        get_default_capacity_id()
    assert "Contoso F2" in str(err.value) and "Fabrikam F4" in str(err.value)


def test_default_capacity_none_active(monkeypatch):
    _capacities(monkeypatch, [_capacity("Paused", state="Inactive")])
    with pytest.raises(ValueError, match="(?s)No active capacities.*Paused"):
        get_default_capacity_id()


def test_default_capacity_none_visible(monkeypatch):
    _capacities(monkeypatch, [])
    with pytest.raises(ValueError, match="No capacities found"):
        get_default_capacity_id()


# --- get_workspace_id_by_name ----------------------------------------------


def _workspaces(monkeypatch, names: list[str]) -> list[int]:
    """Serve `names` as the visible workspaces; return a one-element call counter."""
    calls = [0]

    def fake_list():
        calls[0] += 1
        return [{"id": f"id-{n}", "displayName": n} for n in names]

    monkeypatch.setattr("fabric_deploy.client.list_workspaces", fake_list)
    return calls


def test_workspace_lookup_is_case_insensitive(monkeypatch):
    _workspaces(monkeypatch, ["Contoso [DEV]", "Fabrikam [DEV]"])
    assert get_workspace_id_by_name("contoso [dev]") == "id-Contoso [DEV]"


def test_workspace_not_found_suggests_close_matches_from_one_listing(monkeypatch):
    calls = _workspaces(monkeypatch, ["Contoso [DEV]", "Contoso [PROD]", "Northwind [DEV]"])
    with pytest.raises(ValueError) as err:
        get_workspace_id_by_name("Contoso [TEST]")
    message = str(err.value)
    assert "Did you mean" in message
    assert "Contoso [DEV]" in message and "Contoso [PROD]" in message
    assert "Northwind" not in message
    assert calls == [1]


def test_workspace_not_found_suggests_at_most_five(monkeypatch):
    _workspaces(monkeypatch, [f"Contoso {i}" for i in range(9)])
    with pytest.raises(ValueError) as err:
        get_workspace_id_by_name("Contoso")
    assert str(err.value).count("  - Contoso") == 5


def test_workspace_not_found_without_close_matches_lists_nothing(monkeypatch):
    _workspaces(monkeypatch, ["Woodgrove Analytics", "Northwind Finance"])
    with pytest.raises(ValueError, match="No similarly named workspace") as err:
        get_workspace_id_by_name("Contoso [DEV]")
    assert "Woodgrove" not in str(err.value)


def test_workspace_duplicate_names_raise(monkeypatch):
    _workspaces(monkeypatch, ["Contoso", "contoso"])
    with pytest.raises(ValueError, match="Multiple workspaces"):
        get_workspace_id_by_name("Contoso")


# --- find_item_by_name ---------------------------------------------------


def _items(monkeypatch, items):
    monkeypatch.setattr("fabric_deploy.client.list_workspace_items", lambda ws, t: items)


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


# --- refresh_semantic_model poll loop ------------------------------------


def _refresh(monkeypatch, post_headers, states):
    """Fake the refresh POST and its polls; return the ordered sleep/poll events."""
    events: list[str] = []
    polls = iter(states)
    monkeypatch.setattr("fabric_deploy.client.time.sleep", lambda s: events.append(f"sleep {s}"))

    def fake_http(url, method="GET", data=None, scope=None, error_context="", idempotent=False):
        if method == "POST":
            return 202, post_headers, {}
        events.append(f"poll {url}")
        return 200, {}, next(polls)

    monkeypatch.setattr("fabric_deploy.client.http_request", fake_http)
    return events


def test_refresh_semantic_model_polls_until_terminal(monkeypatch):
    events = _refresh(
        monkeypatch,
        {"location": "https://api.powerbi.com/r/req1"},
        [{"status": "NotStarted"}, {"status": "Unknown"}, {"status": "Completed"}],
    )
    detail = refresh_semantic_model("ws", "ds", timeout_s=100, poll_s=3)
    assert detail["status"] == "Completed"
    # NotStarted and Unknown-without-endTime are in progress; every poll waits first.
    assert events == ["sleep 3", "poll https://api.powerbi.com/r/req1"] * 3


def test_refresh_semantic_model_returns_failed_detail(monkeypatch):
    _refresh(monkeypatch, {"location": "https://r/1"}, [{"status": "Failed"}])
    assert refresh_semantic_model("ws", "ds", poll_s=1)["status"] == "Failed"


def test_refresh_semantic_model_needs_a_refresh_id(monkeypatch):
    """`RequestId` is a per-call trace id on every response, not the refresh id."""
    _refresh(monkeypatch, {"requestid": "trace-1"}, [])
    with pytest.raises(RuntimeError, match="no Location"):
        refresh_semantic_model("ws", "ds")


# --- get_semantic_model_definition -----------------------------------------


def _definition_response(monkeypatch, body) -> list[dict[str, object]]:
    """Answer getDefinition with `body`; returns each call's keyword arguments."""
    calls: list[dict[str, object]] = []

    def fake_http(url, **kwargs):
        calls.append(kwargs)
        return 200, {}, body

    monkeypatch.setattr("fabric_deploy.client.http_request", fake_http)
    return calls


def test_semantic_model_definition_is_retried_as_a_read(monkeypatch):
    """getDefinition is a POST that changes nothing, so a server error may be retried."""
    calls = _definition_response(monkeypatch, {"definition": {"parts": []}})
    get_semantic_model_definition("ws", "sm")
    assert [(c["method"], c["idempotent"]) for c in calls] == [("POST", True)]


def test_semantic_model_definition_decodes_parts(monkeypatch):
    tmdl = "role Readers\n"
    payload = base64.b64encode(tmdl.encode()).decode()
    _definition_response(
        monkeypatch,
        {"definition": {"parts": [{"path": "definition/roles/Readers.tmdl", "payload": payload}]}},
    )
    assert get_semantic_model_definition("ws", "sm") == {"definition/roles/Readers.tmdl": tmdl}


@pytest.mark.parametrize("body", [{}, {"status": "Succeeded"}])
def test_semantic_model_definition_missing_raises(monkeypatch, body):
    """No definition is an error, never an empty model (callers would read it as no roles)."""
    _definition_response(monkeypatch, body)
    with pytest.raises(RuntimeError, match="no definition"):
        get_semantic_model_definition("ws", "sm")


# --- get_sqldatabase_server_host ------------------------------------------


def _sqldbs(monkeypatch, value):
    monkeypatch.setattr(
        "fabric_deploy.client.fabric_api_list", lambda endpoint, error_context: value
    )


def test_sqldatabase_host_strips_port_suffix(monkeypatch):
    _sqldbs(
        monkeypatch,
        [
            {
                "displayName": "mydb",
                "properties": {"serverFqdn": "abc-def.database.fabric.microsoft.com,1433"},
            }
        ],
    )
    assert get_sqldatabase_server_host("ws", "mydb") == "abc-def.database.fabric.microsoft.com"


def test_sqldatabase_host_portless_fqdn_unchanged(monkeypatch):
    _sqldbs(
        monkeypatch,
        [
            {
                "displayName": "MyDB",
                "properties": {"serverFqdn": "abc-def.database.fabric.microsoft.com"},
            }
        ],
    )
    # also exercises case-insensitive name matching
    assert get_sqldatabase_server_host("ws", "mydb") == "abc-def.database.fabric.microsoft.com"


def test_sqldatabase_host_no_match_raises_with_listing(monkeypatch):
    _sqldbs(monkeypatch, [{"displayName": "other", "properties": {}}])
    with pytest.raises(ValueError, match="No SQLDatabase matching 'mydb'"):
        get_sqldatabase_server_host("ws", "mydb")


def test_sqldatabase_host_missing_fqdn_raises(monkeypatch):
    _sqldbs(monkeypatch, [{"displayName": "mydb", "properties": {}}])
    with pytest.raises(ValueError, match="no serverFqdn"):
        get_sqldatabase_server_host("ws", "mydb")


# --- API roots -------------------------------------------------------------


def test_endpoint_calls_use_fabric_cicd_api_roots(monkeypatch):
    """Fabric calls go to FABRIC_API_ROOT_URL, Power BI calls to DEFAULT_API_ROOT_URL."""
    monkeypatch.setattr(constants, "FABRIC_API_ROOT_URL", "https://fabric.contoso.example")
    monkeypatch.setattr(constants, "DEFAULT_API_ROOT_URL", "https://pbi.contoso.example")
    monkeypatch.setattr("fabric_deploy.client.time.sleep", lambda _s: None)
    urls: list[str] = []

    def fake_http(url, method="GET", data=None, scope=None, error_context="", idempotent=False):
        urls.append(url)
        if url.endswith("/refreshes"):
            return 202, {"x-ms-request-id": "r1"}, {}
        if "/refreshes/" in url:
            return 200, {}, {"status": "Completed"}
        return 200, {}, {"definition": {"parts": []}}

    monkeypatch.setattr("fabric_deploy.client.http_request", fake_http)
    refresh_sql_endpoint_metadata("ws", "ep")
    refresh_semantic_model("ws", "ds")
    get_semantic_model_definition("ws", "sm")

    assert urls == [
        "https://fabric.contoso.example/v1/workspaces/ws/sqlEndpoints/ep/refreshMetadata",
        "https://pbi.contoso.example/v1.0/myorg/groups/ws/datasets/ds/refreshes",
        "https://pbi.contoso.example/v1.0/myorg/groups/ws/datasets/ds/refreshes/r1",
        "https://fabric.contoso.example/v1/workspaces/ws/semanticModels/sm/getDefinition?format=TMDL",
    ]
