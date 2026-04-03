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

FABDEP_PROFILE_PATH = Path.home() / ".azure" / "azureProfile.fabdep.json"


def _read_fabdep_profile() -> dict:
    """Read the fabdep profile, returning empty structure if missing."""
    if not FABDEP_PROFILE_PATH.exists():
        return {"accounts": [], "aliases": {}}
    return json.loads(FABDEP_PROFILE_PATH.read_text(encoding="utf-8"))


def _write_fabdep_profile(profile: dict) -> None:
    """Write the fabdep profile."""
    FABDEP_PROFILE_PATH.write_text(
        json.dumps(profile, indent=2), encoding="utf-8"
    )


def _persist_spn(account: AzureAccount) -> None:
    """Save an SPN account entry to the fabdep profile if not already there."""
    profile = _read_fabdep_profile()
    accounts: list[dict] = profile.setdefault("accounts", [])

    # Update existing or append
    for i, existing in enumerate(accounts):
        if existing.get("id") == account["id"]:
            accounts[i] = dict(account)
            _write_fabdep_profile(profile)
            return

    accounts.append(dict(account))
    _write_fabdep_profile(profile)


def list_accounts() -> list[AzureAccount]:
    """
    List all Azure accounts, merging in persisted SPNs and aliases.

    Accounts from `az account list` are merged with SPN entries stored in the
    fabdep profile. Aliases from the profile override display names.
    """
    result = subprocess.run(
        ["az", "account", "list", "--output", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    az_accounts: list[AzureAccount] = json.loads(result.stdout)

    fabdep_profile = _read_fabdep_profile()
    aliases: dict[str, str] = fabdep_profile.get("aliases", {})
    spn_accounts: list[AzureAccount] = fabdep_profile.get("accounts", [])

    # Index az accounts by subscription ID
    az_ids = {a["id"] for a in az_accounts}

    # Auto-persist any SPNs from az account list (before alias override)
    for acct in az_accounts:
        if acct.get("user", {}).get("type", "").lower() == "serviceprincipal":
            _persist_spn(acct)

    # Add persisted SPNs that aren't in az account list
    for spn in spn_accounts:
        if spn["id"] not in az_ids:
            spn["isDefault"] = False
            az_accounts.append(spn)

    # Apply aliases
    for acct in az_accounts:
        alias = aliases.get(acct["id"])
        if alias:
            acct["name"] = alias

    return az_accounts


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
    Rename an account by storing an alias in the fabdep profile.

    The alias persists across `az account list --refresh` and `az login`.

    Args:
        subscription_id: Subscription ID to rename
        new_name: New display name

    Raises:
        ValueError: If subscription ID not found in any account list
    """
    # Verify the account exists
    accounts = list_accounts()
    if not any(a["id"] == subscription_id for a in accounts):
        raise ValueError(
            f"Account '{subscription_id}' not found.\n"
            "Run 'fabdep auth list' to see available accounts."
        )

    profile = _read_fabdep_profile()
    aliases: dict[str, str] = profile.setdefault("aliases", {})
    aliases[subscription_id] = new_name
    _write_fabdep_profile(profile)
