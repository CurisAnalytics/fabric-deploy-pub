"""Azure account management for the `fabdep auth` subcommands.

Identity/credential concerns (token acquisition, tenant pinning, identity
introspection) live in `identity`; the fabdep profile's JSON I/O lives in
`profile`. This module is just the account-management actions the CLI exposes:
list / switch / rename / multi-tenant login + bind.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import TYPE_CHECKING

from .identity import AzureAccount, active_account, describe_tenants, tenant_registry
from .profile import read_fabdep_profile, write_fabdep_profile

if TYPE_CHECKING:
    from .config import TenantRegistryEntry


__all__ = [
    "list_accounts",
    "switch_account",
    "rename_subscription",
    "login_tenant",
    "bind_tenant",
]


def login_tenant(
    alias: str,
    *,
    app_id: str | None = None,
    tenant: str | None = None,
    cert: str | None = None,
    config_dir: str | None = None,
    user: bool = False,
) -> TenantRegistryEntry:
    """Log a tenant in to an isolated AZURE_CONFIG_DIR and register the alias.

    SPN-cert mode (default) needs app_id + tenant + cert; on a re-login these are
    reused from the stored entry, so `login_tenant("contoso")` is enough after the
    first time. `user=True` does an interactive `az login` (non-SPN). The tenant
    id written to the registry is read live from the fresh login, so the registry
    can't drift from reality.
    """
    existing = tenant_registry().get(alias, {})
    cfg = os.path.expanduser(
        config_dir or existing.get("config_dir") or f"~/.azure-{alias}"
    )
    env = {**os.environ, "AZURE_CONFIG_DIR": cfg}

    if user:
        cmd = ["az", "login", "--allow-no-subscriptions", "--output", "none"]
        pinned_tenant = tenant or existing.get("id")
        if pinned_tenant:
            cmd += ["--tenant", pinned_tenant]
    else:
        app_id = app_id or existing.get("app_id")
        cert = cert or existing.get("cert_path")
        tenant = tenant or existing.get("id")
        missing = [
            flag
            for flag, val in (("--app", app_id), ("--tenant", tenant), ("--cert", cert))
            if not val
        ]
        if missing:
            raise ValueError(
                f"First login for tenant '{alias}' needs {', '.join(missing)} "
                "(nothing stored to reuse). Pass --user for a non-SPN login."
            )
        assert app_id and tenant and cert  # narrowed by the `missing` check above
        cert = os.path.expanduser(cert)
        if not os.path.exists(cert):
            raise FileNotFoundError(f"cert not found: {cert}")
        cmd = [
            "az", "login", "--service-principal",
            "--username", app_id, "--tenant", tenant, "--certificate", cert,
            "--allow-no-subscriptions", "--output", "none",
        ]

    subprocess.run(cmd, env=env, check=True)

    acct = active_account(cfg)
    if acct is None:
        raise RuntimeError(
            f"login for '{alias}' reported success but no active account in {cfg}"
        )

    entry: TenantRegistryEntry = {"id": acct["tenantId"], "config_dir": cfg}
    if not user:
        assert app_id and cert  # the SPN branch above set and validated both
        entry["app_id"] = app_id
        entry["cert_path"] = cert
    require_spn = existing.get("require_spn")
    if require_spn:
        entry["require_spn"] = require_spn

    # Upsert the alias into the `tenants` registry, preserving everything else.
    profile = read_fabdep_profile()
    profile.setdefault("tenants", {})[alias] = entry
    write_fabdep_profile(profile)
    return entry


def bind_tenant(alias: str, target: str) -> TenantRegistryEntry:
    """Register `alias` against an identity already in the shared `az` login.

    The developer-machine counterpart of `login_tenant`: nothing is logged in
    and no config dir is written. `target` is either a tenant GUID — any enabled
    account in that tenant will do at deploy time — or a subscription id / name
    as `fabdep auth list` shows it (renames included), which pins that account.
    The pin then mints tokens with `--subscription`, leaving the active account
    alone. Replaces an existing entry for the alias (keeping `require_spn`).
    """
    accounts = [a for a in list_accounts() if a.get("state", "Enabled") == "Enabled"]
    entry: TenantRegistryEntry
    if any(a.get("tenantId") == target for a in accounts):
        entry = {"id": target}
    else:
        matches = [a for a in accounts if a["id"] == target] or [
            a for a in accounts if a["name"].lower() == target.lower()
        ]
        if not matches:
            raise ValueError(
                f"'{target}' is not a tenant id, subscription id, or account name "
                f"in `az account list`.\n{describe_tenants(accounts)}\n"
                "Log in to the tenant first: az login --tenant <tenant-id>"
            )
        if len(matches) > 1:
            raise ValueError(
                f"Multiple accounts match '{target}':\n"
                + "\n".join(f"  - {a['id']}: {a['name']}" for a in matches)
            )
        entry = {"id": matches[0]["tenantId"], "subscription": matches[0]["id"]}

    if tenant_registry().get(alias, {}).get("require_spn"):
        entry["require_spn"] = True

    profile = read_fabdep_profile()
    profile.setdefault("tenants", {})[alias] = entry
    write_fabdep_profile(profile)
    return entry


def _persist_spn(account: AzureAccount) -> None:
    """Save an SPN account entry to the fabdep profile if not already there."""
    profile = read_fabdep_profile()
    accounts: list[dict] = profile.setdefault("accounts", [])

    for i, existing in enumerate(accounts):
        if existing.get("id") == account["id"]:
            accounts[i] = dict(account)
            write_fabdep_profile(profile)
            return

    accounts.append(dict(account))
    write_fabdep_profile(profile)


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

    fabdep_profile = read_fabdep_profile()
    aliases: dict[str, str] = fabdep_profile.get("aliases", {})
    spn_accounts: list[AzureAccount] = fabdep_profile.get("accounts", [])
    az_ids = {a["id"] for a in az_accounts}

    # SPNs disappear from `az account list` after a refresh; persist them so
    # they survive across `az login` cycles.
    for acct in az_accounts:
        if acct.get("user", {}).get("type", "").lower() == "serviceprincipal":
            _persist_spn(acct)
    for spn in spn_accounts:
        if spn["id"] not in az_ids:
            spn["isDefault"] = False
            az_accounts.append(spn)

    for acct in az_accounts:
        alias = aliases.get(acct["id"])
        if alias:
            acct["name"] = alias

    return az_accounts


def switch_account(account: str) -> None:
    """
    Switch to a different account.

    Accepts either a subscription ID or a display name (including fabdep
    aliases). Names are resolved to IDs via the merged account list.
    """
    accounts = list_accounts()

    by_id = next((a for a in accounts if a["id"] == account), None)
    if by_id is not None:
        target_id = by_id["id"]
    else:
        by_name = [a for a in accounts if a["name"].lower() == account.lower()]
        if not by_name:
            raise ValueError(
                f"Account '{account}' not found.\n"
                "Run 'fabdep auth list' to see available accounts."
            )
        if len(by_name) > 1:
            raise ValueError(
                f"Multiple accounts match '{account}':\n"
                + "\n".join(f"  - {a['id']}: {a['name']}" for a in by_name)
            )
        target_id = by_name[0]["id"]

    _ = subprocess.run(
        ["az", "account", "set", "--subscription", target_id],
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
    accounts = list_accounts()
    if not any(a["id"] == subscription_id for a in accounts):
        raise ValueError(
            f"Account '{subscription_id}' not found.\n"
            "Run 'fabdep auth list' to see available accounts."
        )

    profile = read_fabdep_profile()
    aliases: dict[str, str] = profile.setdefault("aliases", {})
    aliases[subscription_id] = new_name
    write_fabdep_profile(profile)
