"""Azure authentication utilities for Fabric CI/CD."""

import json
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from .config import TenantRegistryEntry


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


class TenantMismatchError(RuntimeError):
    """The active Azure identity does not satisfy fabdep.yml's tenant pin."""


__all__ = [
    "AzureAccount",
    "TenantMismatchError",
    "list_accounts",
    "get_current_account",
    "switch_account",
    "rename_subscription",
    "active_account",
    "assert_active_tenant",
    "tenant_registry",
    "login_tenant",
]


def active_account(config_dir: str | None = None) -> AzureAccount | None:
    """Return the active `az` account, or None if not logged in.

    Honors `config_dir` (an `AZURE_CONFIG_DIR`) so an isolated per-tenant
    login can be inspected without disturbing the process environment.
    """
    env = os.environ.copy()
    if config_dir:
        env["AZURE_CONFIG_DIR"] = os.path.expanduser(config_dir)
    result = subprocess.run(
        ["az", "account", "show", "--output", "json"],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        # Honor the "None if not logged in" contract even if `az` somehow
        # returns success with non-JSON stdout, rather than leaking a decode error.
        return None


def assert_active_tenant(
    expected_tenant_id: str,
    *,
    config_dir: str | None = None,
    require_spn: bool = False,
) -> AzureAccount:
    """Verify the active identity lives in `expected_tenant_id`.

    Raises `TenantMismatchError` if no one is logged in, the active tenant
    differs, or `require_spn` is set and the identity is a user. Returns the
    account on success.
    """
    acct = active_account(config_dir)
    where = f" in AZURE_CONFIG_DIR={os.path.expanduser(config_dir)}" if config_dir else ""

    if acct is None:
        raise TenantMismatchError(
            f"No active Azure login found{where}.\n"
            f"fabdep.yml pins tenant {expected_tenant_id}.\n"
            "Log in for this tenant, e.g.:\n"
            "  AZURE_CONFIG_DIR=<dir> az login --service-principal \\\n"
            "    --username <app-id> --tenant <tenant-id> --certificate <cert.pem>"
        )

    # Pin against tenantId (the tenant the identity is acting in), not
    # homeTenantId — a guest / multi-tenant SPN acts in tenantId.
    actual = acct.get("tenantId")
    if actual != expected_tenant_id:
        user = acct.get("user", {}).get("name", "?")
        raise TenantMismatchError(
            f"Active identity is in tenant {actual}, but fabdep.yml pins "
            f"{expected_tenant_id}.\n"
            f"Active account: {acct.get('name')} (identity: {user}).\n"
            "Switch identity or correct the tenant pin before deploying."
        )

    if require_spn and acct.get("user", {}).get("type", "").lower() != "serviceprincipal":
        user = acct.get("user", {}).get("name", "?")
        raise TenantMismatchError(
            f"fabdep.yml requires a service principal for tenant "
            f"{expected_tenant_id}, but the active identity is a user ({user})."
        )

    return acct


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


def tenant_registry() -> dict[str, "TenantRegistryEntry"]:
    """Machine-local `alias -> {id, config_dir, ...}` map from the fabdep profile.

    Lets a committed fabdep.yml carry a portable `tenant: <alias>` instead of a
    machine-specific config_dir or a bare tenant GUID. Written by `fabdep auth
    login` (the single source of truth); empty when none are registered.
    """
    return _read_fabdep_profile().get("tenants", {})


def login_tenant(
    alias: str,
    *,
    app_id: str | None = None,
    tenant: str | None = None,
    cert: str | None = None,
    config_dir: str | None = None,
    user: bool = False,
) -> "TenantRegistryEntry":
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
    profile = _read_fabdep_profile()
    profile.setdefault("tenants", {})[alias] = entry
    _write_fabdep_profile(profile)
    return entry


def _persist_spn(account: AzureAccount) -> None:
    """Save an SPN account entry to the fabdep profile if not already there."""
    profile = _read_fabdep_profile()
    accounts: list[dict] = profile.setdefault("accounts", [])

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


def get_current_account() -> AzureAccount | None:
    """
    Get currently active account.
    Returns: Current account or None if no account is set
    """
    accounts = list_accounts()
    return next((a for a in accounts if a.get("isDefault")), None)


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

    profile = _read_fabdep_profile()
    aliases: dict[str, str] = profile.setdefault("aliases", {})
    aliases[subscription_id] = new_name
    _write_fabdep_profile(profile)
