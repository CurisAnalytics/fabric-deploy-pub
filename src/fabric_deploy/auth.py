"""Azure authentication utilities for Fabric CI/CD."""

import json
import subprocess
from pathlib import Path
from typing import TypedDict


class AzureUser(TypedDict):
    name: str
    type: str


class AzureAccount(TypedDict):
    cloudName: str
    homeTenantId: str
    id: str
    isDefault: bool
    name: str
    state: str
    tenantId: str
    user: AzureUser


__all__ = [
    "AzureAccount",
    "list_accounts",
    "get_current_account",
    "switch_account",
    "rename_subscription",
]


def list_accounts(refresh: bool = False) -> list[AzureAccount]:
    """
    List all Azure accounts.
    Args: refresh: If True, force refresh from Azure (slower but current data)
    Returns: List of Azure accounts
    """
    cmd = ["az", "account", "list", "--output", "json"]
    if refresh:
        cmd.append("--refresh")
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def get_current_account() -> AzureAccount | None:
    """
    Get currently active account.
    Returns: Current account or None if no account is set
    """
    accounts = list_accounts()
    return next((a for a in accounts if a.get("isDefault")), None)


def switch_account(subscription_id: str) -> None:
    """
    Switch to a different account.
    subscription_id: Subscription ID to switch to
    """
    _ = subprocess.run(
        ["az", "account", "set", "--subscription", subscription_id],
        check=True,
        capture_output=True,
    )


def rename_subscription(subscription_id: str, new_name: str) -> None:
    """
    Rename an Azure subscription locally (edits ~/.azure/azureProfile.json).

    This is a local-only change — it does NOT call the Azure API and will not
    be visible to other users or tenant admins.

    Args:
        subscription_id: Subscription ID to rename
        new_name: New name for the subscription

    Raises:
        FileNotFoundError: If Azure CLI profile not found
        ValueError: If subscription ID not found in local profile
    """
    profile_path = Path.home() / ".azure" / "azureProfile.json"
    if not profile_path.exists():
        raise FileNotFoundError(
            f"Azure CLI profile not found at {profile_path}\n"
            "Make sure you're logged in with 'az login'"
        )

    # Azure CLI profile uses UTF-8-BOM encoding
    raw = profile_path.read_bytes()
    text = raw.decode("utf-8-sig")
    profile = json.loads(text)

    found = False
    for sub in profile.get("subscriptions", []):
        if sub.get("id") == subscription_id:
            sub["name"] = new_name
            found = True
            break

    if not found:
        raise ValueError(
            f"Subscription '{subscription_id}' not found in local Azure CLI profile.\n"
            "Run 'fabdep auth list' to see available subscriptions."
        )

    profile_path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
