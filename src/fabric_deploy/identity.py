"""Authentication: which credential fabdep signs in with, and a guard on who it signs in as.

The credential mode comes from the `--auth` flag, else the FABDEP_AUTH environment
variable, else `auth:` in fabdep.yml, else `cli`:

    cli       AzureCliCredential: whoever `az login` signed in
    env       EnvironmentCredential: a service principal from AZURE_* variables
    workload  WorkloadIdentityCredential: a federated (OIDC) token file, as CI systems issue
    managed   ManagedIdentityCredential: the managed identity of the Azure host
    default   DefaultAzureCredential: azure-identity's own chain of the above

A token supplied in FABRIC_BEARER_TOKEN (and POWERBI_BEARER_TOKEN for the Power BI
API) is used as-is instead, for machines that cannot sign in themselves.

Every new token is checked against fabdep.yml: its `tid` claim must be the pinned
`tenant:`, and with `require_spn: true` it must be an app-only token. The claims
are read, not verified: the APIs verify tokens, this only keeps fabdep from
deploying as the wrong identity. REST calls and fabric-cicd share one credential,
so both run as the same identity and reuse its cached tokens.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

from azure.core.credentials import AccessToken, TokenCredential
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import (
    AzureCliCredential,
    DefaultAzureCredential,
    EnvironmentCredential,
    ManagedIdentityCredential,
    WorkloadIdentityCredential,
)

from .config import AUTH_MODES, AuthSettings

__all__ = [
    "FABRIC_SCOPE",
    "POWERBI_SCOPE",
    "AuthError",
    "apply_auth_settings",
    "apply_tenant_pin",
    "auth_hint",
    "auth_mode",
    "credential_in_use",
    "describe_identity",
    "fabric_credential",
    "get_access_token",
    "set_auth_mode",
]

FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
_AUTH_ENV = "FABDEP_AUTH"

# Relayed tokens per scope, in order of preference. The Power BI API may reject a
# Fabric-audience token; relay a Power BI one in POWERBI_BEARER_TOKEN if it does.
_RELAY_ENV = {
    FABRIC_SCOPE: ("FABRIC_BEARER_TOKEN",),
    POWERBI_SCOPE: ("POWERBI_BEARER_TOKEN", "FABRIC_BEARER_TOKEN"),
}
_RENEW_BEFORE_S = 300  # mint a fresh token when the cached one expires sooner than this
_RELAY_MIN_LIFE_S = 60  # a relayed token can't be renewed: refuse one about to expire

_SIGN_IN = {
    "cli": "Sign in with `az login --tenant {tenant}` (as a service principal: `az login "
    "--service-principal --username <client-id> --certificate <cert.pem> --tenant {tenant}`).",
    "env": "Set AZURE_TENANT_ID, AZURE_CLIENT_ID and AZURE_CLIENT_SECRET (or "
    "AZURE_CLIENT_CERTIFICATE_PATH) for the service principal.",
    "workload": "Set AZURE_TENANT_ID, AZURE_CLIENT_ID and AZURE_FEDERATED_TOKEN_FILE (a file "
    "holding the OIDC token your CI system issues), and add a federated credential for "
    "that issuer and subject to the app registration.",
    "managed": "Run on an Azure resource with a managed identity; set AZURE_CLIENT_ID to "
    "use a user-assigned one.",
    "default": "DefaultAzureCredential tries AZURE_* variables, workload identity, managed "
    "identity and `az login` in turn: set one of them up, or name one with --auth.",
}

_log = logging.getLogger(__name__)
_settings: AuthSettings | None = None  # fabdep.yml's identity keys, once a command adopts them
_mode_flag: str | None = None  # the --auth flag
_credential: _GuardedCredential | None = None


class AuthError(RuntimeError):
    """No token could be had, or the token is for an identity fabdep.yml rules out."""


def set_auth_mode(mode: str | None) -> None:
    """Force a credential mode (the --auth flag), ahead of FABDEP_AUTH and fabdep.yml."""
    global _mode_flag, _credential
    _mode_flag, _credential = mode, None


def apply_auth_settings(settings: AuthSettings | None, logger: logging.Logger) -> None:
    """Adopt fabdep.yml's identity keys for every token minted from here on.

    Call before the first Fabric API call. An unknown FABDEP_AUTH value fails here;
    the tenant can only be checked on a token, which happens on first use.
    """
    global _settings, _credential
    _settings = settings
    _credential = _GuardedCredential(*auth_mode(), settings=settings, logger=logger)


apply_tenant_pin = apply_auth_settings  # the name cli.py imports


def auth_mode() -> tuple[str, str]:
    """The credential mode in effect and where it was set."""
    if _mode_flag:
        return _mode_flag, "--auth"
    env = os.environ.get(_AUTH_ENV, "").strip().lower()
    if env:
        if env not in AUTH_MODES:
            raise AuthError(
                f"{_AUTH_ENV}={env!r} is not a credential mode; use one of: {', '.join(AUTH_MODES)}."
            )
        return env, _AUTH_ENV
    if _settings and _settings.mode:
        return _settings.mode, "fabdep.yml"
    return "cli", "default"


def credential_in_use() -> str:
    """Where Fabric tokens come from: a relayed token, or the mode and where it was set."""
    relay = _relay_var(FABRIC_SCOPE)
    if relay:
        return f"token relayed in {relay}"
    mode, source = auth_mode()
    return f"{mode} ({source})"


def auth_hint() -> str:
    """How to get a token for the right identity with the credential in use."""
    relay = _relay_var(FABRIC_SCOPE)
    if relay:
        return f"Set {relay} to a fresh token for the right identity, or unset it to sign in."
    return _sign_in_hint(auth_mode()[0], _settings.tenant if _settings else None)


def get_access_token(scope: str = FABRIC_SCOPE) -> str:
    """An access token for `scope`, from the shared credential.

    Raises:
        AuthError: No token could be had, or fabdep.yml rules out its identity.
    """
    return _shared().get_token(scope).token


def fabric_credential() -> TokenCredential:
    """The credential to hand fabric-cicd: the one REST calls use, cache included."""
    return _shared()


def describe_identity() -> dict[str, str]:
    """Mint a Fabric token and describe whom it is for (`fabdep auth status`)."""
    claims = _claims(get_access_token(), "The Fabric token")
    tenant = str(claims.get("tid", "?"))
    pinned = (
        "pinned in fabdep.yml" if _settings and _settings.tenant else "fabdep.yml pins no tenant"
    )
    expires = datetime.fromtimestamp(int(claims.get("exp", 0)), UTC)
    return {
        "Credential": credential_in_use(),
        "Identity": _who(claims),
        "Token type": "app-only (service principal or managed identity)"
        if _is_app_token(claims)
        else "user (delegated)",
        "Tenant": f"{tenant} ({pinned})",
        "Expires": expires.strftime("%Y-%m-%d %H:%M UTC"),
    }


def _shared() -> _GuardedCredential:
    global _credential
    if _credential is None:
        _credential = _GuardedCredential(*auth_mode(), settings=_settings, logger=_log)
    return _credential


def _new_credential(mode: str, tenant_id: str | None) -> TokenCredential:
    """The azure-identity credential for `mode`, pinned to `tenant_id` where the class allows.

    EnvironmentCredential takes its tenant from AZURE_TENANT_ID, a managed identity
    belongs to its host's tenant, and DefaultAzureCredential has no single tenant
    setting; for those, the `tid` check on each token enforces the pinned tenant.
    """
    if mode == "cli":
        return AzureCliCredential(tenant_id=tenant_id or "")  # "": the signed-in tenant
    if mode == "env":
        return EnvironmentCredential()
    if mode == "workload":
        return WorkloadIdentityCredential(tenant_id=tenant_id)  # None: AZURE_TENANT_ID
    if mode == "managed":
        return ManagedIdentityCredential(client_id=os.environ.get("AZURE_CLIENT_ID"))
    return DefaultAzureCredential()


class _GuardedCredential(TokenCredential):
    """The mode's credential (or a relayed token), cached per scope and checked against fabdep.yml."""

    def __init__(
        self, mode: str, source: str, *, settings: AuthSettings | None, logger: logging.Logger
    ) -> None:
        self._mode, self._source = mode, source
        self._settings = settings
        self._logger = logger
        self._inner: TokenCredential | None = None
        self._cache: dict[tuple[str, ...], AccessToken] = {}
        self._announced: set[str] = set()

    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        cached = self._cache.get(scopes)
        if cached and not kwargs and cached.expires_on - time.time() > _RENEW_BEFORE_S:
            return cached
        relay = _relay_var(scopes[0]) if len(scopes) == 1 else None
        if relay:
            token, issuer = self._relayed(relay, scopes[0]), f"the token in {relay}"
        else:
            token, issuer = self._mint(scopes, kwargs), f"the {self._mode} credential"
        self._check(token.token, issuer, relay)
        self._cache[scopes] = token
        return token

    def _relayed(self, var: str, scope: str) -> AccessToken:
        token = os.environ[var].strip()
        exp = int(_claims(token, f"The token in {var}").get("exp", 0))
        if exp - time.time() < _RELAY_MIN_LIFE_S:
            resource = scope.removesuffix("/.default")
            raise AuthError(
                f"The token in {var} has expired. Set it to a fresh one (`az account "
                f"get-access-token --resource {resource} --query accessToken --output tsv` "
                f"on a machine that can sign in), or unset it to use the {self._mode} credential."
            )
        if var not in self._announced:
            self._announced.add(var)
            self._logger.info(f"Using the access token in {var} instead of signing in")
        return AccessToken(token, exp)

    def _mint(self, scopes: tuple[str, ...], kwargs: dict[str, Any]) -> AccessToken:
        tenant = self._settings.tenant if self._settings else None
        try:
            if self._inner is None:
                self._inner = _new_credential(self._mode, tenant)
            return self._inner.get_token(*scopes, **kwargs)
        except (ClientAuthenticationError, ValueError) as e:
            raise AuthError(
                f"The {self._mode} credential ({self._source}) could not get a token: {e}\n"
                f"{_sign_in_hint(self._mode, tenant)}"
            ) from e

    def _check(self, token: str, issuer: str, relay: str | None) -> None:
        settings = self._settings
        if not settings or not (settings.tenant or settings.require_spn):
            return
        claims = _claims(token, f"The token from {issuer}")
        fix = (
            f"Set {relay} to a token for the right identity, or unset it."
            if relay
            else _sign_in_hint(self._mode, settings.tenant)
        )
        tid = str(claims.get("tid", "?"))
        if settings.tenant and tid.lower() != settings.tenant:
            raise AuthError(
                f"fabdep.yml pins tenant {settings.tenant}, but {issuer} signed in as "
                f"{_who(claims)} in tenant {tid}.\n{fix}"
            )
        if settings.require_spn and not _is_app_token(claims):
            raise AuthError(
                f"fabdep.yml sets require_spn: true, but {issuer} signed in as the user "
                f"{_who(claims)}. Use a service principal or managed identity.\n{fix}"
            )


def _relay_var(scope: str) -> str | None:
    """The first environment variable holding a relayed token for `scope`, if any."""
    return next((v for v in _RELAY_ENV.get(scope, ()) if os.environ.get(v, "").strip()), None)


def _sign_in_hint(mode: str, tenant: str | None) -> str:
    return _SIGN_IN[mode].format(tenant=tenant or "<tenant-id>")


def _claims(token: str, what: str) -> dict[str, Any]:
    """A JWT's payload, unverified."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError, binascii.Error):
        claims = None
    if not isinstance(claims, dict):
        raise AuthError(f"{what} is not a JWT access token, so fabdep cannot tell whom it is for.")
    return claims


def _is_app_token(claims: dict[str, Any]) -> bool:
    # `idtyp` is optional on some APIs; delegated (user) tokens always carry `scp`.
    return claims.get("idtyp") == "app" or ("idtyp" not in claims and "scp" not in claims)


def _who(claims: dict[str, Any]) -> str:
    if _is_app_token(claims):
        app = claims.get("appid") or claims.get("azp") or "?"
        name = claims.get("app_displayname")
        return f"{name} (app {app})" if name else f"app {app}"
    for key in ("upn", "unique_name", "preferred_username", "oid"):
        if claims.get(key):
            return str(claims[key])
    return "an unnamed user"
