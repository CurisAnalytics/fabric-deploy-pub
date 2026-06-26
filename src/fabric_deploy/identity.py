"""Authentication: how fabdep acquires a token and as whom.

This is the single home for credential acquisition — keeping auth logic out of
the REST client (`api.py`) and the deployer. Two strategies:

- **Default:** `AzureCliCredential`, so whatever account `fabdep auth switch`
  selected is what we deploy as. It mints any requested scope on demand.
- **Relay fallback:** a pre-fetched bearer token in the environment, for boxes
  where `az login` is CA-blocked (e.g. a client tenant from a locked-down host). This is a
  break-glass path — cert/interactive auth is preferred when available.

Semantic-model refresh lives on the Power BI API, a different audience than the
Fabric API; `get_access_token(scope)` handles both.
"""

import base64
import json
import os
import time

from azure.core.credentials import AccessToken, TokenCredential
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import AzureCliCredential

__all__ = [
    "FABRIC_SCOPE",
    "POWERBI_SCOPE",
    "StaticTokenCredential",
    "fabric_credential",
    "get_access_token",
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
