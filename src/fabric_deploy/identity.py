"""Authentication: how fabdep acquires a token and as whom.

This is the single home for credential acquisition — keeping auth logic out of
the REST client (`client.py`) and the deployer. Two strategies:

- **Default:** `AzureCliCredential`, so whatever account `fabdep auth switch`
  selected is what we deploy as. It mints any requested scope on demand.
- **Relay fallback:** a pre-fetched bearer token in the environment, for boxes
  where `az login` is CA-blocked (e.g. a client tenant from a locked-down host). This is a
  break-glass path — cert/interactive auth is preferred when available.

Semantic-model refresh lives on the Power BI API, a different audience than the
Fabric API; `get_access_token(scope)` handles both.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import time
from typing import TYPE_CHECKING, TypedDict

from azure.core.credentials import AccessToken, TokenCredential
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import AzureCliCredential

from .profile import _read_fabdep_profile

if TYPE_CHECKING:
    from .config import TenantPin, TenantRegistryEntry

__all__ = [
    "FABRIC_SCOPE",
    "POWERBI_SCOPE",
    "AzureAccount",
    "TenantMismatchError",
    "StaticTokenCredential",
    "fabric_credential",
    "get_access_token",
    "active_account",
    "assert_active_tenant",
    "tenant_registry",
    "apply_tenant_pin",
]

FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
# A relayed FABRIC_BEARER_TOKEN is Fabric-audience and may be rejected by the
# Power BI host; relay a Power BI token via POWERBI_BEARER_TOKEN when needed.
POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
_BEARER_ENV = "FABRIC_BEARER_TOKEN"
_POWERBI_BEARER_ENV = "POWERBI_BEARER_TOKEN"
# Treat a token as expired this many seconds early, so a long-running deploy
# doesn't begin with a token that dies mid-flight — we can't refresh a relayed
# token, so failing fast with a clear message beats a confusing 401 partway in.
_EXPIRY_SKEW_SECONDS = 60


def _env_bearer_token() -> str | None:
    """Return the relay bearer token from the environment, stripped, or None.

    A stray newline (e.g. from `FABRIC_BEARER_TOKEN=$(cat file)` on some
    shells) would corrupt the Authorization header, so strip defensively.
    """
    token = os.environ.get(_BEARER_ENV, "").strip()
    return token or None


def _env_powerbi_token() -> str | None:
    """Return the relayed Power BI bearer token from the environment, or None."""
    token = os.environ.get(_POWERBI_BEARER_ENV, "").strip()
    return token or None


def _jwt_exp(token: str) -> int:
    """Decode `exp` (unix seconds) from a JWT payload without verifying signature."""
    try:
        payload_b64 = token.split(".")[1]
        # base64url padding
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
        return int(payload["exp"])
    except (IndexError, KeyError, ValueError) as e:
        raise RuntimeError(f"Relay bearer token is not a decodable JWT: {e}") from e


def _token_expired(exp: int) -> bool:
    """True if `exp` (unix seconds) is past, or within the skew margin."""
    return time.time() >= exp - _EXPIRY_SKEW_SECONDS


class StaticTokenCredential(TokenCredential):
    """TokenCredential backed by a pre-fetched bearer token (env-supplied).

    Used when token acquisition is blocked on the local box (e.g. Fabrikam CA
    on a locked-down host) but a token relayed from another machine is available. Fails
    loudly if the token is expired rather than silently issuing a 401-bound
    request.
    """

    def __init__(self, token: str) -> None:
        self._token = token
        self._exp = _jwt_exp(token)

    def get_token(self, *scopes: str, **_: object) -> AccessToken:  # noqa: ARG002
        if _token_expired(self._exp):
            raise ClientAuthenticationError(
                f"{_BEARER_ENV} expired at unix {self._exp}; relay a fresh token."
            )
        return AccessToken(self._token, self._exp)


def fabric_credential() -> TokenCredential:
    """Return the credential fabdep should use for Fabric calls.

    If `FABRIC_BEARER_TOKEN` is set, wrap it as a `StaticTokenCredential`
    (token-relay mode). Otherwise return `AzureCliCredential()` (default).
    """
    token = _env_bearer_token()
    if token:
        return StaticTokenCredential(token)
    return AzureCliCredential()


def get_access_token(scope: str = FABRIC_SCOPE) -> str:
    """
    Get an Azure access token for the given API scope.

    If a relay bearer token is set, use it directly (token-relay mode for
    boxes where `az login` is CA-blocked). The Power BI scope prefers
    `POWERBI_BEARER_TOKEN` and falls back to `FABRIC_BEARER_TOKEN` (whose
    audience may be rejected by the Power BI host). Otherwise use
    `AzureCliCredential`, which mints the requested scope on demand against
    the active account chosen by `fabdep auth switch`.

    Args:
        scope: OAuth scope to request (Fabric API by default; pass the Power
            BI scope for dataset-refresh calls).

    Returns:
        Access token string

    Raises:
        RuntimeError: If token acquisition fails (e.g. `az login` required)
    """
    if scope == POWERBI_SCOPE:
        env_token = _env_powerbi_token() or _env_bearer_token()
    else:
        env_token = _env_bearer_token()
    if env_token:
        if _token_expired(_jwt_exp(env_token)):
            raise RuntimeError(
                f"Relay bearer token is expired; relay a fresh token from a box "
                "that can `az account get-access-token --resource "
                f"{scope.removesuffix('/.default')}`."
            )
        return env_token
    try:
        return AzureCliCredential().get_token(scope).token
    except ClientAuthenticationError as e:
        raise RuntimeError(
            f"Failed to get Azure access token for scope {scope}.\n{e}\n\n"
            "Make sure you're logged in with 'az login'"
        ) from e


# --- Identity introspection & tenant pinning -----------------------------


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


def tenant_registry() -> dict[str, TenantRegistryEntry]:
    """Machine-local `alias -> {id, config_dir, ...}` map from the fabdep profile.

    Lets a committed fabdep.yml carry a portable `tenant: <alias>` instead of a
    machine-specific config_dir or a bare tenant GUID. Written by `fabdep auth
    login` (the single source of truth); empty when none are registered.
    """
    return _read_fabdep_profile().get("tenants", {})


def apply_tenant_pin(pin: TenantPin | None, logger: logging.Logger) -> None:
    """Honor a fabdep.yml `tenant:` pin before any Fabric API call.

    Points `AZURE_CONFIG_DIR` at the tenant's isolated login so the right
    identity is selected automatically (no manual `fabdep auth switch`) and two
    tenants never share `~/.azure` state. Then asserts the active identity
    lives in the pinned tenant, so an identity mix-up surfaces as a clear
    tenant error here instead of a confusing "workspace not found" later — and,
    under `--new-workspace`, can't create the workspace in the wrong tenant.

    Shared by `WorkspaceDeployer` (deploy) and the refresh command so both
    paths resolve the same pinned identity.
    """
    if pin is None:
        return
    pin = pin.resolve(tenant_registry())
    assert pin.id is not None  # resolve() guarantees a concrete id or raises

    if pin.config_dir:
        target = os.path.expanduser(pin.config_dir)
        existing = os.environ.get("AZURE_CONFIG_DIR")
        if existing and existing != target:
            logger.info(
                f"Overriding AZURE_CONFIG_DIR ({existing} -> {target}) "
                "per fabdep.yml tenant pin"
            )
        # Set on the process env and intentionally not restored: AzureCliCredential
        # reads AZURE_CONFIG_DIR at call time, so it must persist through the run.
        # The CLI is one operation per process; concurrent multi-tenant work uses
        # separate processes (see the multi-tenant-auth recipe), so this is safe.
        os.environ["AZURE_CONFIG_DIR"] = target

    # A pre-injected bearer token owns auth and can't be introspected via
    # `az` — nothing to assert in that mode. Use the same helper api gates on,
    # so a malformed (whitespace) token is treated identically by both.
    if _env_bearer_token():
        return

    account = assert_active_tenant(
        pin.id, config_dir=pin.config_dir, require_spn=pin.require_spn
    )
    logger.info(
        f"Tenant pin OK: '{account.get('name')}' in {pin.id} "
        f"({account.get('user', {}).get('name')})"
    )
