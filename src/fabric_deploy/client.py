"""Fabric / Power BI REST domain client — endpoint calls over the transport layer."""

import base64
import difflib
import logging
import time
from typing import Any, TypedDict

from .identity import FABRIC_SCOPE, POWERBI_SCOPE
from .transport import (
    fabric_api_list,
    fabric_api_request,
    fabric_url,
    http_request,
    poll_fabric_lro,
    powerbi_url,
)

_logger = logging.getLogger(__name__)


class FabricWorkspaceInfo(TypedDict):
    """Fabric workspace information from REST API."""

    id: str
    displayName: str
    description: str
    type: str
    capacityId: str


__all__ = [
    "FabricWorkspaceInfo",
    "list_workspaces",
    "find_workspace_id_by_name",
    "get_workspace_id_by_name",
    "create_workspace",
    "list_capacities",
    "get_default_capacity_id",
    "list_workspace_items",
    "find_item_by_name",
    "get_sqldatabase_server_host",
    "refresh_sql_endpoint_metadata",
    "refresh_semantic_model",
    "get_semantic_model_definition",
]


def list_workspaces() -> list[FabricWorkspaceInfo]:
    """
    List all Fabric workspaces accessible to the current user.

    Returns:
        List of workspace information dictionaries

    Raises:
        RuntimeError: If API call fails
    """
    return fabric_api_list("/v1/workspaces", error_context="list Fabric workspaces")


def list_capacities() -> list[dict[str, Any]]:
    """
    List all Fabric capacities accessible to the current user.

    Returns:
        List of capacity information dictionaries with keys:
        - id (str): Capacity ID (GUID)
        - displayName (str): Capacity display name
        - sku (str): Capacity SKU (e.g., F4, F8, F16)
        - region (str): Azure region
        - state (str): Capacity state ("Active" or "Inactive")

    Raises:
        RuntimeError: If API call fails
    """
    return fabric_api_list("/v1/capacities", error_context="list Fabric capacities")


def get_default_capacity_id() -> str:
    """
    Get the capacity for a new workspace when fabdep.yml sets no `capacity_id`.

    Only an unambiguous choice is made: the one active capacity the identity
    can see.

    Returns:
        Capacity ID (GUID) of the only active capacity

    Raises:
        ValueError: If no capacity, or more than one, is active
        RuntimeError: If API call fails
    """
    capacities = list_capacities()
    active = [c for c in capacities if c.get("state") == "Active"]

    if len(active) == 1:
        _logger.info(f"Using the only active capacity: {_describe_capacity(active[0])}")
        return active[0]["id"]
    if active:
        raise ValueError(
            f"{len(active)} active capacities are available; set capacity_id in "
            "fabdep.yml to choose one:\n"
            + "\n".join(f"  - {_describe_capacity(c)}" for c in active)
        )
    if capacities:
        raise ValueError(
            "No active capacities found.\n\n"
            f"Available capacities ({len(capacities)}):\n"
            + "\n".join(f"  - {_describe_capacity(c)}" for c in capacities)
            + "\n\nActivate a capacity or set capacity_id in fabdep.yml."
        )
    raise ValueError(
        "No capacities found.\n\n"
        "This identity has access to no Fabric capacity. Create one or request "
        "access, then set capacity_id in fabdep.yml."
    )


def _describe_capacity(capacity: dict[str, Any]) -> str:
    return f"{capacity['displayName']} ({capacity['sku']}, {capacity['state']}): {capacity['id']}"


def find_workspace_id_by_name(workspace_name: str) -> str | None:
    """
    Find workspace ID by display name (case-insensitive). Non-raising lookup.

    Args:
        workspace_name: Workspace display name to search for

    Returns:
        Workspace ID (GUID) if a single match is found, else None

    Raises:
        ValueError: If multiple workspaces match the same name
        RuntimeError: If API call fails
    """
    return _match_workspace(list_workspaces(), workspace_name)


def _match_workspace(workspaces: list[FabricWorkspaceInfo], workspace_name: str) -> str | None:
    """The ID of the one workspace named `workspace_name` (case-insensitive), or None."""
    matches = [ws for ws in workspaces if ws["displayName"].lower() == workspace_name.lower()]

    if len(matches) == 0:
        return None
    if len(matches) > 1:
        raise ValueError(
            f"Multiple workspaces found with name '{workspace_name}':\n"
            + "\n".join(f"  - {ws['id']}: {ws['displayName']}" for ws in matches)
        )
    return matches[0]["id"]


def get_workspace_id_by_name(workspace_name: str) -> str:
    """
    Get workspace ID by display name (case-insensitive). Raises if not found.

    Args:
        workspace_name: Workspace display name to search for

    Returns:
        Workspace ID (GUID)

    Raises:
        ValueError: If workspace not found or multiple matches found
        RuntimeError: If API call fails
    """
    workspaces = list_workspaces()
    found = _match_workspace(workspaces, workspace_name)
    if found is not None:
        return found

    by_lower = {ws["displayName"].lower(): ws["displayName"] for ws in workspaces}
    close = difflib.get_close_matches(workspace_name.lower(), list(by_lower), n=5)
    if close:
        hint = "Did you mean:\n" + "\n".join(f"  - {by_lower[name]}" for name in close)
    else:
        hint = (
            f"No similarly named workspace among the {len(workspaces)} this identity can "
            "see. Check the name and that the identity has access to the workspace."
        )
    raise ValueError(f"Workspace not found: '{workspace_name}'\n\n{hint}")


def create_workspace(workspace_name: str, capacity_id: str) -> str:
    """
    Create a new Fabric workspace.

    Args:
        workspace_name: Display name for the new workspace
        capacity_id: Fabric capacity ID (GUID)

    Returns:
        Workspace ID (GUID) of the created workspace

    Raises:
        RuntimeError: If API call fails
        ValueError: If workspace already exists
    """
    if find_workspace_id_by_name(workspace_name) is not None:
        raise ValueError(f"Workspace already exists: '{workspace_name}'")

    payload = {
        "displayName": workspace_name,
        "capacityId": capacity_id,
    }

    data = fabric_api_request(
        "/v1/workspaces",
        method="POST",
        data=payload,
        error_context=f"create workspace '{workspace_name}'",
    )
    return data["id"]


# --- Item lookup ---------------------------------------------------------


def list_workspace_items(workspace_id: str, item_type: str | None = None) -> list[dict[str, Any]]:
    """List items in a workspace, optionally filtered by Fabric item type.

    Args:
        workspace_id: Workspace GUID.
        item_type: Fabric item type (e.g. "SemanticModel", "SQLEndpoint").
            Omit to list every item.

    Returns:
        List of item dicts (each has `id`, `displayName`, `type`).
    """
    endpoint = f"/v1/workspaces/{workspace_id}/items"
    if item_type:
        endpoint += f"?type={item_type}"
    return fabric_api_list(endpoint, error_context=f"list {item_type or 'workspace'} items")


def find_item_by_name(workspace_id: str, item_type: str, name: str | None = None) -> dict[str, Any]:
    """Resolve a single workspace item of `item_type` by display name.

    With `name=None`, succeeds only when exactly one item of that type exists
    (the common single-model / single-endpoint staging case). Raises a
    helpful, list-everything error on no/ambiguous match — so a typo surfaces
    here instead of as a silent no-op downstream.

    Args:
        workspace_id: Workspace GUID.
        item_type: Fabric item type to match.
        name: Display name to match (case-insensitive), or None.

    Returns:
        The matching item dict.

    Raises:
        ValueError: On zero or multiple matches.
    """
    items = list_workspace_items(workspace_id, item_type)
    if name is not None:
        matches = [i for i in items if i["displayName"].lower() == name.lower()]
    else:
        matches = items

    if len(matches) == 1:
        return matches[0]

    available = sorted(i["displayName"] for i in items)
    listing = "\n".join(f"  - {n}" for n in available) or "  (none)"
    if not matches:
        target = f" matching '{name}'" if name else ""
        raise ValueError(
            f"No {item_type}{target} found in the workspace.\n"
            f"Available {item_type}s ({len(available)}):\n{listing}"
        )
    raise ValueError(
        f"Multiple {item_type}s found"
        + (f" matching '{name}'" if name else "")
        + f" — specify one by name:\n{listing}"
    )


def get_sqldatabase_server_host(workspace_id: str, name: str) -> str:
    """Resolve a SQL database's bare TDS hostname in the target workspace.

    Uses the typed `/sqlDatabases` endpoint — the generic `/items` list omits
    type-specific properties. The API's `serverFqdn` is ADO.NET-style with a
    port suffix ("<host>.database.fabric.microsoft.com,1433"); source repos
    embed the bare host (M expressions, JDBC variables), so the suffix is
    stripped before returning.

    Raises:
        ValueError: No SQL database matches `name`, or it has no serverFqdn
            yet (still provisioning).
    """
    items = fabric_api_list(
        f"/v1/workspaces/{workspace_id}/sqlDatabases", error_context="list SQL databases"
    )
    matches = [i for i in items if i.get("displayName", "").lower() == name.lower()]
    if len(matches) != 1:
        available = sorted(i.get("displayName", "?") for i in items)
        listing = "\n".join(f"  - {n}" for n in available) or "  (none)"
        raise ValueError(
            f"{'No' if not matches else 'Multiple'} SQLDatabase matching '{name}' "
            f"found in the workspace.\nAvailable SQLDatabases ({len(available)}):\n{listing}"
        )

    server_fqdn = matches[0].get("properties", {}).get("serverFqdn", "")
    if not server_fqdn:
        raise ValueError(f"SQLDatabase '{name}' has no serverFqdn yet (still provisioning?)")
    return server_fqdn.split(",")[0]


# --- Refresh / framing ---------------------------------------------------


def refresh_sql_endpoint_metadata(
    workspace_id: str, sql_endpoint_id: str, timeout_s: int = 600
) -> list[dict[str, Any]]:
    """Sync a SQL analytics endpoint's metadata (table discovery + schema).

    Required after any Delta write before a Direct Lake refresh, otherwise the
    refresh fails with "cannot access the source Delta table". Handles both
    the 200 (synchronous) and 202 (LRO) responses.

    Returns:
        Per-table sync statuses: `[{tableName, status, error?}, ...]`.
    """
    status, headers, body = http_request(
        fabric_url(f"/v1/workspaces/{workspace_id}/sqlEndpoints/{sql_endpoint_id}/refreshMetadata"),
        method="POST",
        scope=FABRIC_SCOPE,
        error_context="refresh SQL endpoint metadata",
    )
    if status == 202:
        body = poll_fabric_lro(
            headers, error_context="SQL endpoint metadata sync", timeout_s=timeout_s
        )
    return body.get("value", []) if isinstance(body, dict) else []


def refresh_semantic_model(
    workspace_id: str,
    dataset_id: str,
    timeout_s: int = 900,
    poll_s: int = 10,
) -> dict[str, Any]:
    """Trigger an enhanced (framing) refresh of a semantic model and poll it.

    Uses the Power BI enhanced-refresh API (POST → 202 + `Location` → poll the
    refresh object until terminal). For a Direct Lake model this is the
    framing step; a `Completed` status proves the model re-framed but NOT that
    measures evaluate (a broken measure only fails when a query runs it).

    Returns:
        The final refresh-detail object, including `status` and — on failure —
        `serviceExceptionJson` / `messages`.
    """
    body: dict[str, Any] = {"type": "automatic", "commitMode": "transactional", "objects": []}
    _status, headers, _resp = http_request(
        powerbi_url(f"/v1.0/myorg/groups/{workspace_id}/datasets/{dataset_id}/refreshes"),
        method="POST",
        data=body,
        scope=POWERBI_SCOPE,
        error_context="trigger semantic model refresh",
    )

    location = headers.get("location")
    request_id = headers.get("x-ms-request-id")
    if location and location.startswith("/"):
        location = powerbi_url(location)
    elif not location and request_id:
        location = powerbi_url(
            f"/v1.0/myorg/groups/{workspace_id}/datasets/{dataset_id}/refreshes/{request_id}"
        )
    if not location:
        raise RuntimeError(
            "Semantic model refresh: 202 Accepted but no Location or x-ms-request-id to poll."
        )

    waited = 0
    while True:
        # A just-queued refresh is never done; wait before every poll.
        sleep_for = min(poll_s, max(1, timeout_s - waited))
        time.sleep(sleep_for)
        waited += sleep_for
        _s, _h, detail = http_request(
            location, scope=POWERBI_SCOPE, error_context="poll semantic model refresh"
        )
        status = detail.get("status")
        # "Unknown" with no endTime means still in progress.
        in_progress = status in (None, "", "NotStarted", "InProgress") or (
            status == "Unknown" and not detail.get("endTime")
        )
        if not in_progress:
            return detail
        if waited >= timeout_s:
            raise RuntimeError(
                f"Semantic model refresh did not finish within {timeout_s}s "
                f"(last status: {status})."
            )


# --- Item definition read ------------------------------------------------


def get_semantic_model_definition(
    workspace_id: str, item_id: str, timeout_s: int = 300
) -> dict[str, str]:
    """Fetch a semantic model's definition as `{part_path: decoded_text}`.

    Uses the Fabric getDefinition API in TMDL format (POST -> 202 LRO -> parts
    as InlineBase64). Only definition parts (roles, model.tmdl, tables, …) are
    returned — RLS *role memberships* are engine state and are NOT present here,
    so this can never read or round-trip who is assigned to a role.

    Args:
        workspace_id: Workspace GUID.
        item_id: Semantic model item GUID.
        timeout_s: LRO poll timeout.

    Returns:
        Mapping of definition part path (e.g. "definition/roles/RLS.tmdl") to
        its UTF-8-decoded text.

    Raises:
        RuntimeError: The call fails or the response carries no definition.
    """
    status, headers, body = http_request(
        fabric_url(
            f"/v1/workspaces/{workspace_id}/semanticModels/{item_id}/getDefinition?format=TMDL"
        ),
        method="POST",
        scope=FABRIC_SCOPE,
        error_context="get semantic model definition",
    )
    if status == 202:
        body = poll_fabric_lro(
            headers, error_context="semantic model getDefinition", timeout_s=timeout_s
        )

    definition = body.get("definition")
    if definition is None:
        raise RuntimeError(
            f"getDefinition for semantic model {item_id} returned no definition: {body}"
        )
    parts = definition.get("parts", [])
    result: dict[str, str] = {}
    for part in parts:
        path = part.get("path")
        payload = part.get("payload")
        if not path or payload is None:
            continue
        if part.get("payloadType", "InlineBase64") != "InlineBase64":
            continue
        result[path] = base64.b64decode(payload).decode("utf-8")
    return result
