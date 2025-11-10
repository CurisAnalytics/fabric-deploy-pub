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
