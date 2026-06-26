"""Microsoft Fabric REST API utilities."""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import TypedDict

from .identity import FABRIC_SCOPE, POWERBI_SCOPE, get_access_token


# pyright: reportMissingTypeArgument=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownParameterType=false
# pyright: reportUnknownVariableType=false


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
    "refresh_sql_endpoint_metadata",
    "refresh_semantic_model",
]


_FABRIC_HOST = "https://api.fabric.microsoft.com"
_POWERBI_HOST = "https://api.powerbi.com"


def _http_request(
    url: str,
    method: str = "GET",
    data: dict | None = None,
    scope: str = FABRIC_SCOPE,
    error_context: str = "API request",
) -> tuple[int, dict[str, str], dict]:
    """Make an authenticated HTTP request and return (status, headers, body).

    Unlike `_fabric_api_request`, this exposes the status code and response
    headers — needed to follow long-running operations (202 + `Location` /
    `Retry-After`). An empty response body parses to `{}`.

    Args:
        url: Absolute request URL.
        method: HTTP method.
        data: Optional JSON request payload. For bodyless POST/PATCH/PUT an
            empty body is sent so a `Content-Length: 0` header is present
            (some Fabric endpoints require it).
        scope: OAuth scope for the token (Fabric vs Power BI).
        error_context: Context string for error messages.

    Returns:
        (status_code, headers_dict, parsed_body)

    Raises:
        RuntimeError: If the API call fails.
    """
    token = get_access_token(scope)
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
        request_data = json.dumps(data).encode()
    elif method in ("POST", "PATCH", "PUT"):
        request_data = b""  # forces Content-Length: 0
    else:
        request_data = None

    try:
        req = urllib.request.Request(
            url, data=request_data, headers=headers, method=method
        )
        with urllib.request.urlopen(req) as response:
            body = response.read().decode()
            parsed = json.loads(body) if body.strip() else {}
            return response.status, dict(response.headers), parsed
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        raise RuntimeError(
            f"Failed to {error_context} (HTTP {e.code}).\n{error_body}\n\n"
            "Check your permissions and network connectivity."
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Failed to connect to the API.\n{e.reason}\n\n"
            "Check your network connectivity."
        ) from e


def _fabric_api_request(
    endpoint: str,
    method: str = "GET",
    data: dict | None = None,
    error_context: str = "API request",
) -> dict:
    """
    Generic Fabric API request handler with error handling.

    Args:
        endpoint: API endpoint path (e.g., "/v1/workspaces")
        method: HTTP method (GET or POST)
        data: Optional request payload for POST requests
        error_context: Context string for error messages

    Returns:
        Parsed JSON response as dictionary

    Raises:
        RuntimeError: If API call fails
    """
    _status, _headers, body = _http_request(
        f"{_FABRIC_HOST}{endpoint}",
        method=method,
        data=data,
        scope=FABRIC_SCOPE,
        error_context=error_context,
    )
    return body


def list_workspaces() -> list[FabricWorkspaceInfo]:
    """
    List all Fabric workspaces accessible to the current user.

    Returns:
        List of workspace information dictionaries

    Raises:
        RuntimeError: If API call fails
    """
    data = _fabric_api_request("/v1/workspaces", error_context="list Fabric workspaces")
    return data.get("value", [])


def list_capacities() -> list[dict]:
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
    data = _fabric_api_request("/v1/capacities", error_context="list Fabric capacities")
    return data.get("value", [])


def get_default_capacity_id() -> str:
    """
    Get the first active capacity ID available to the user.

    Returns:
        Capacity ID (GUID) of the first active capacity

    Raises:
        ValueError: If no active capacities are found
        RuntimeError: If API call fails
    """
    capacities = list_capacities()
    active_capacities = [c for c in capacities if c.get("state") == "Active"]

    if not active_capacities:
        available = [
            f"{c['displayName']} ({c['sku']}, {c['state']})" for c in capacities
        ]
        if available:
            raise ValueError(
                "No active capacities found.\n\n"
                f"Available capacities ({len(available)}):\n"
                + "\n".join(f"  - {name}" for name in available)
                + "\n\nPlease activate a capacity or specify capacity_id in fabdep.yml"
            )
        raise ValueError(
            "No capacities found.\n\n"
            "You don't have access to any Fabric capacities.\n"
            "Please create a capacity or request access, then specify capacity_id in fabdep.yml"
        )

    return active_capacities[0]["id"]


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
    workspaces = list_workspaces()

    matches = [
        ws for ws in workspaces if ws["displayName"].lower() == workspace_name.lower()
    ]

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
    found = find_workspace_id_by_name(workspace_name)
    if found is not None:
        return found

    # Listing again to render a helpful error message — cheap relative to the
    # cost of a failed deployment, and only happens on the unhappy path.
    available = [ws["displayName"] for ws in list_workspaces()]
    raise ValueError(
        f"Workspace not found: '{workspace_name}'\n\n"
        f"Available workspaces ({len(available)}):\n"
        + "\n".join(f"  - {name}" for name in sorted(available))
    )


def create_workspace(
    workspace_name: str,
    capacity_id: str,
    description: str | None = None,
) -> str:
    """
    Create a new Fabric workspace.

    Args:
        workspace_name: Display name for the new workspace
        capacity_id: Fabric capacity ID (GUID)
        description: Optional workspace description

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
    if description:
        payload["description"] = description

    data = _fabric_api_request(
        "/v1/workspaces",
        method="POST",
        data=payload,
        error_context=f"create workspace '{workspace_name}'",
    )
    return data["id"]


# --- Item lookup ---------------------------------------------------------


def list_workspace_items(
    workspace_id: str, item_type: str | None = None
) -> list[dict]:
    """List items in a workspace, optionally filtered by Fabric item type.

    Args:
        workspace_id: Workspace GUID.
        item_type: Fabric item type (e.g. "SemanticModel", "SQLEndpoint").
            Omit to list every item.

    Returns:
        List of item dicts (each has `id`, `displayName`, `type`).
    """
    base = f"/v1/workspaces/{workspace_id}/items"
    params = [f"type={item_type}"] if item_type else []
    ctx = f"list {item_type or 'workspace'} items"

    items: list[dict] = []
    token: str | None = None
    while True:
        query = list(params)
        if token:
            query.append(f"continuationToken={urllib.parse.quote(token)}")
        endpoint = base + ("?" + "&".join(query) if query else "")
        data = _fabric_api_request(endpoint, error_context=ctx)
        items.extend(data.get("value", []))
        # The items API paginates; a model/endpoint on page 2+ would otherwise
        # be invisible and resolve as "not found".
        token = data.get("continuationToken")
        if not token:
            return items


def find_item_by_name(
    workspace_id: str, item_type: str, name: str | None = None
) -> dict:
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


# --- Refresh / framing ---------------------------------------------------


def _poll_fabric_lro(
    headers: dict[str, str],
    error_context: str,
    timeout_s: int = 600,
) -> dict:
    """Follow a Fabric 202 long-running operation to completion.

    Polls the `Location` URL on the cadence the service asks for
    (`Retry-After`), then fetches the operation result. Raises on a Failed /
    Cancelled operation, a 202 with no `Location` to poll, or a timeout.

    Returns:
        The operation result body (`{}` if the operation has no result).
    """
    location = headers.get("Location") or headers.get("location")
    op_id = headers.get("x-ms-operation-id") or headers.get("x-ms-operationid")
    # Clamp to >= 1: a Retry-After of 0 would never advance `waited` -> spin.
    retry_after = max(1, int(headers.get("Retry-After") or 5))
    if not location:
        # 202 with nothing to poll — don't report a no-op as success.
        raise RuntimeError(
            f"{error_context}: 202 Accepted but no Location header to poll."
        )

    waited = 0
    while True:
        sleep_for = min(retry_after, max(1, timeout_s - waited))
        time.sleep(sleep_for)
        waited += sleep_for
        _status, hdrs, body = _http_request(
            location, scope=FABRIC_SCOPE, error_context=f"poll {error_context}"
        )
        op_status = body.get("status")
        if op_status in ("Succeeded", "Completed"):
            if op_id:
                try:
                    _s, _h, result = _http_request(
                        f"{_FABRIC_HOST}/v1/operations/{op_id}/result",
                        scope=FABRIC_SCOPE,
                        error_context=f"fetch {error_context} result",
                    )
                    return result
                except RuntimeError:
                    return body  # result endpoint optional for some ops
            return body
        if op_status in ("Failed", "Cancelled"):
            raise RuntimeError(f"{error_context} {op_status}: {body.get('error')}")
        retry_after = max(1, int(hdrs.get("Retry-After") or retry_after))
        if waited >= timeout_s:
            raise RuntimeError(
                f"{error_context} did not finish within {timeout_s}s "
                f"(last status: {op_status})."
            )


def refresh_sql_endpoint_metadata(
    workspace_id: str, sql_endpoint_id: str, timeout_s: int = 600
) -> list[dict]:
    """Sync a SQL analytics endpoint's metadata (table discovery + schema).

    Required after any Delta write before a Direct Lake refresh, otherwise the
    refresh fails with "cannot access the source Delta table". Handles both
    the 200 (synchronous) and 202 (LRO) responses.

    Returns:
        Per-table sync statuses: `[{tableName, status, error?}, ...]`.
    """
    status, headers, body = _http_request(
        f"{_FABRIC_HOST}/v1/workspaces/{workspace_id}/sqlEndpoints/{sql_endpoint_id}/refreshMetadata",
        method="POST",
        scope=FABRIC_SCOPE,
        error_context="refresh SQL endpoint metadata",
    )
    if status == 202:
        body = _poll_fabric_lro(
            headers, error_context="SQL endpoint metadata sync", timeout_s=timeout_s
        )
    return body.get("value", []) if isinstance(body, dict) else []


def refresh_semantic_model(
    workspace_id: str,
    dataset_id: str,
    timeout_s: int = 900,
    poll_s: int = 10,
) -> dict:
    """Trigger an enhanced (framing) refresh of a semantic model and poll it.

    Uses the Power BI enhanced-refresh API (POST → 202 + `Location` → poll the
    refresh object until terminal). For a Direct Lake model this is the
    framing step; a `Completed` status proves the model re-framed but NOT that
    measures evaluate.

    Returns:
        The final refresh-detail object, including `status` and — on failure —
        `serviceExceptionJson` / `messages`.
    """
    body = {"type": "automatic", "commitMode": "transactional", "objects": []}
    _status, headers, _resp = _http_request(
        f"{_POWERBI_HOST}/v1.0/myorg/groups/{workspace_id}/datasets/{dataset_id}/refreshes",
        method="POST",
        data=body,
        scope=POWERBI_SCOPE,
        error_context="trigger semantic model refresh",
    )

    location = headers.get("Location") or headers.get("location")
    request_id = headers.get("x-ms-request-id") or headers.get("RequestId")
    if location and location.startswith("/"):
        location = f"{_POWERBI_HOST}{location}"
    if not location:
        location = (
            f"{_POWERBI_HOST}/v1.0/myorg/groups/{workspace_id}"
            f"/datasets/{dataset_id}/refreshes/{request_id}"
        )

    waited = 0
    detail: dict = {}
    while True:
        _s, _h, detail = _http_request(
            location, scope=POWERBI_SCOPE, error_context="poll semantic model refresh"
        )
        status = detail.get("status")
        # "Unknown" with no endTime means still in progress.
        if status and status != "InProgress" and not (
            status == "Unknown" and not detail.get("endTime")
        ):
            return detail
        if waited >= timeout_s:
            raise RuntimeError(
                f"Semantic model refresh did not finish within {timeout_s}s "
                f"(last status: {status})."
            )
        sleep_for = min(poll_s, max(1, timeout_s - waited))
        time.sleep(sleep_for)
        waited += sleep_for
