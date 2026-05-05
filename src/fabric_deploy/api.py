"""Microsoft Fabric REST API utilities."""

import json
import urllib.error
import urllib.request
from typing import TypedDict

from azure.core.exceptions import ClientAuthenticationError
from azure.identity import AzureCliCredential


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
    "get_access_token",
    "list_workspaces",
    "find_workspace_id_by_name",
    "get_workspace_id_by_name",
    "create_workspace",
    "list_capacities",
    "get_default_capacity_id",
]


_FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"


def get_access_token() -> str:
    """
    Get Azure access token for Fabric API via the active `az` session.

    Uses `AzureCliCredential` so the active subscription chosen by
    `fabdep auth switch` / `az account set` is what gets used. No subprocess
    shell-out — `azure-identity` handles caching and refresh internally.

    Returns:
        Access token string

    Raises:
        RuntimeError: If token acquisition fails (e.g. `az login` required)
    """
    try:
        return AzureCliCredential().get_token(_FABRIC_SCOPE).token
    except ClientAuthenticationError as e:
        raise RuntimeError(
            f"Failed to get Azure access token for Fabric API.\n{e}\n\n"
            "Make sure you're logged in with 'az login'"
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
    token = get_access_token()
    url = f"https://api.fabric.microsoft.com{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    request_data = json.dumps(data).encode() if data else None

    try:
        req = urllib.request.Request(
            url, data=request_data, headers=headers, method=method
        )
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        raise RuntimeError(
            f"Failed to {error_context} (HTTP {e.code}).\n{error_body}\n\n"
            "Check your permissions and network connectivity."
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Failed to connect to Fabric API.\n{e.reason}\n\n"
            "Check your network connectivity."
        ) from e


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
