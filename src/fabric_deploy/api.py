"""Microsoft Fabric REST API utilities."""

import json
import subprocess
from typing import TypedDict


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
    "get_workspace_id_by_name",
    "workspace_exists",
    "create_workspace",
    "list_capacities",
    "get_default_capacity_id",
]


def get_access_token() -> str:
    """
    Get Azure access token for Fabric API.

    Returns:
        Access token string

    Raises:
        RuntimeError: If token acquisition fails
    """
    try:
        result = subprocess.run(
            [
                "az",
                "account",
                "get-access-token",
                "--resource",
                "https://api.fabric.microsoft.com",
                "--output",
                "json",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        token_data = json.loads(result.stdout)
        return token_data["accessToken"]
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.strip() if e.stderr else "Unknown error"
        raise RuntimeError(
            f"Failed to get Azure access token for Fabric API.\n{error_msg}\n\n"
            "Make sure you're logged in with 'az login'"
        ) from e
    except (json.JSONDecodeError, KeyError) as e:
        raise RuntimeError(f"Failed to parse access token response: {e}") from e


def list_workspaces() -> list[FabricWorkspaceInfo]:
    """
    List all Fabric workspaces accessible to the current user.

    Returns:
        List of workspace information dictionaries

    Raises:
        RuntimeError: If API call fails
    """
    import urllib.error
    import urllib.request

    token = get_access_token()

    url = "https://api.fabric.microsoft.com/v1/workspaces"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
            return data.get("value", [])
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        raise RuntimeError(
            f"Failed to list Fabric workspaces (HTTP {e.code}).\n{error_body}\n\n"
            "Check your permissions and network connectivity."
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Failed to connect to Fabric API.\n{e.reason}\n\n"
            "Check your network connectivity."
        ) from e


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
    import urllib.error
    import urllib.request

    token = get_access_token()

    url = "https://api.fabric.microsoft.com/v1/capacities"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
            return data.get("value", [])
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        raise RuntimeError(
            f"Failed to list Fabric capacities (HTTP {e.code}).\n{error_body}\n\n"
            "Check your permissions and network connectivity."
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Failed to connect to Fabric API.\n{e.reason}\n\n"
            "Check your network connectivity."
        ) from e


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

    # Filter for active capacities
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
        else:
            raise ValueError(
                "No capacities found.\n\n"
                "You don't have access to any Fabric capacities.\n"
                "Please create a capacity or request access, then specify capacity_id in fabdep.yml"
            )

    # Return first active capacity
    return active_capacities[0]["id"]


def get_workspace_id_by_name(workspace_name: str) -> str:
    """
    Get workspace ID by display name (case-insensitive match).

    Args:
        workspace_name: Workspace display name to search for

    Returns:
        Workspace ID (GUID)

    Raises:
        ValueError: If workspace not found or multiple matches found
        RuntimeError: If API call fails
    """
    workspaces = list_workspaces()

    # Case-insensitive search
    matches = [
        ws for ws in workspaces if ws["displayName"].lower() == workspace_name.lower()
    ]

    if len(matches) == 0:
        available = [ws["displayName"] for ws in workspaces]
        raise ValueError(
            f"Workspace not found: '{workspace_name}'\n\n"
            f"Available workspaces ({len(available)}):\n"
            + "\n".join(f"  - {name}" for name in sorted(available))
        )
    elif len(matches) > 1:
        raise ValueError(
            f"Multiple workspaces found with name '{workspace_name}':\n"
            + "\n".join(f"  - {ws['id']}: {ws['displayName']}" for ws in matches)
        )

    return matches[0]["id"]


def workspace_exists(workspace_name: str) -> bool:
    """
    Check if a workspace exists with the given display name.

    Args:
        workspace_name: Workspace display name to search for

    Returns:
        True if workspace exists, False otherwise

    Raises:
        RuntimeError: If API call fails
    """
    try:
        get_workspace_id_by_name(workspace_name)
        return True
    except ValueError:
        return False


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
    import urllib.error
    import urllib.request

    # Check if workspace already exists
    if workspace_exists(workspace_name):
        raise ValueError(f"Workspace already exists: '{workspace_name}'")

    token = get_access_token()

    url = "https://api.fabric.microsoft.com/v1/workspaces"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    payload = {
        "displayName": workspace_name,
        "capacityId": capacity_id,
    }
    if description:
        payload["description"] = description

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
            return data["id"]
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        raise RuntimeError(
            f"Failed to create workspace '{workspace_name}' (HTTP {e.code}).\n{error_body}\n\n"
            "Check your permissions and capacity ID."
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Failed to connect to Fabric API.\n{e.reason}\n\n"
            "Check your network connectivity."
        ) from e
