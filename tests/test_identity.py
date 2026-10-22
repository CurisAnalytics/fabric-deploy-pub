"""Tests for the credential layer in fabric_deploy.identity (incl. relay path)."""

import base64
import json
import time

import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import AzureCliCredential

from fabric_deploy import identity
from fabric_deploy.config import TenantPin
from fabric_deploy.identity import (
    _BEARER_ENV,
    StaticTokenCredential,
    _jwt_exp,
    fabric_credential,
    get_access_token,
)


def _make_jwt(exp: int) -> str:
    """Build a syntactically-valid (unsigned) JWT with the given exp claim."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=")
    return f"{header.decode()}.{payload.decode()}.sig"


class TestJwtExp:
    def test_decodes_exp_claim(self):
        token = _make_jwt(1_700_000_000)
        assert _jwt_exp(token) == 1_700_000_000

    def test_malformed_token_raises(self):
        with pytest.raises(RuntimeError, match="not a decodable JWT"):
            _jwt_exp("not.a.jwt!!!")


class TestStaticTokenCredential:
    def test_returns_token_when_unexpired(self):
        token = _make_jwt(int(time.time()) + 3600)
        cred = StaticTokenCredential(token)
        result = cred.get_token("https://api.fabric.microsoft.com/.default")
        assert result.token == token

    def test_raises_when_expired(self):
        token = _make_jwt(int(time.time()) - 60)
        cred = StaticTokenCredential(token)
        with pytest.raises(ClientAuthenticationError, match="expired"):
            cred.get_token("https://api.fabric.microsoft.com/.default")


class TestStaticTokenCredentialSkew:
    def test_raises_when_within_skew_margin(self):
        # token still technically valid (30s left) but inside the 60s skew
        token = _make_jwt(int(time.time()) + 30)
        cred = StaticTokenCredential(token)
        with pytest.raises(ClientAuthenticationError, match="expired"):
            cred.get_token("https://api.fabric.microsoft.com/.default")


class TestFabricCredential:
    def test_returns_cli_credential_by_default(self, monkeypatch):
        monkeypatch.delenv(_BEARER_ENV, raising=False)
        assert isinstance(fabric_credential(), AzureCliCredential)

    def test_returns_static_credential_when_env_set(self, monkeypatch):
        token = _make_jwt(int(time.time()) + 3600)
        monkeypatch.setenv(_BEARER_ENV, token)
        cred = fabric_credential()
        assert isinstance(cred, StaticTokenCredential)

    def test_blank_env_falls_through_to_cli(self, monkeypatch):
        monkeypatch.setenv(_BEARER_ENV, "   ")
        assert isinstance(fabric_credential(), AzureCliCredential)


class TestGetAccessToken:
    def test_returns_env_token_when_unexpired(self, monkeypatch):
        token = _make_jwt(int(time.time()) + 3600)
        monkeypatch.setenv(_BEARER_ENV, token)
        assert get_access_token() == token

    def test_strips_whitespace_from_env_token(self, monkeypatch):
        token = _make_jwt(int(time.time()) + 3600)
        monkeypatch.setenv(_BEARER_ENV, f"\n  {token}\n")
        assert get_access_token() == token

    def test_raises_on_expired_env_token(self, monkeypatch):
        token = _make_jwt(int(time.time()) - 60)
        monkeypatch.setenv(_BEARER_ENV, token)
        with pytest.raises(RuntimeError, match="expired"):
            get_access_token()


# --- same-tenant identity fallback -----------------------------------------

TENANT = "dddddddd-dddd-dddd-dddd-dddddddddddd"
OTHER_TENANT = "99999999-9999-9999-9999-999999999999"


def _account(sub: str, tenant: str = TENANT, user_type: str = "user") -> dict:
    return {
        "id": sub,
        "isDefault": sub == "spn-sub",
        "name": f"acct-{sub}",
        "state": "Enabled",
        "tenantId": tenant,
        "user": {"name": f"{sub}@example", "type": user_type},
    }


class _FakeCliCredential:
    """AzureCliCredential stand-in: only `working` subscriptions mint tokens."""

    working: set[str | None] = set()

    def __init__(self, *, subscription=None, **_):
        self.subscription = subscription

    def get_token(self, *_scopes, **_kw):
        if self.subscription in self.working:
            return AccessToken(f"token-{self.subscription}", int(time.time()) + 3600)
        raise ClientAuthenticationError(
            "AADSTS7000215: Invalid client secret provided.\nTrace ID: x"
        )


@pytest.fixture
def cli_fallback(monkeypatch):
    """Isolate the credential cache/pin and stub the CLI credential + account list."""
    monkeypatch.delenv(_BEARER_ENV, raising=False)
    monkeypatch.setattr(identity, "_CLI_CREDENTIAL", None)
    monkeypatch.setattr(identity, "_ACTIVE_PIN", None)
    monkeypatch.setattr(identity, "AzureCliCredential", _FakeCliCredential)
    monkeypatch.setattr(identity, "active_account", lambda config_dir=None: _account("spn-sub"))
    _FakeCliCredential.working = set()
    yield monkeypatch
    _FakeCliCredential.working = set()


def _accounts(*accts):
    return lambda tenant_id: [a for a in accts if a["tenantId"] == tenant_id]


class TestSameTenantFallback:
    def test_falls_back_to_sibling_in_same_tenant(self, cli_fallback, caplog):
        # Active identity (spn-sub) is broken; the human's own login still works.
        _FakeCliCredential.working = {"user-sub"}
        cli_fallback.setattr(
            identity, "_enabled_accounts", _accounts(_account("spn-sub"), _account("user-sub"))
        )
        with caplog.at_level("WARNING"):
            assert get_access_token() == "token-user-sub"
        assert "falling back to" in caplog.text

    def test_caches_credential_for_fabric_credential(self, cli_fallback):
        _FakeCliCredential.working = {"user-sub"}
        cli_fallback.setattr(
            identity, "_enabled_accounts", _accounts(_account("spn-sub"), _account("user-sub"))
        )
        _ = get_access_token()
        # fabric-cicd must deploy as the identity the REST calls fell back to.
        assert fabric_credential().subscription == "user-sub"

    def test_never_crosses_tenants(self, cli_fallback):
        _FakeCliCredential.working = {"other-sub"}
        cli_fallback.setattr(
            identity,
            "_enabled_accounts",
            _accounts(_account("spn-sub"), _account("other-sub", tenant=OTHER_TENANT)),
        )
        with pytest.raises(RuntimeError, match="Failed to get Azure access token"):
            get_access_token()

    def test_require_spn_pin_skips_user_accounts(self, cli_fallback):
        _FakeCliCredential.working = {"user-sub"}
        cli_fallback.setattr(
            identity, "_ACTIVE_PIN", TenantPin(alias="contoso", id=TENANT, require_spn=True)
        )
        cli_fallback.setattr(
            identity, "_enabled_accounts", _accounts(_account("spn-sub"), _account("user-sub"))
        )
        with pytest.raises(RuntimeError, match="Failed to get Azure access token"):
            get_access_token()

    def test_pin_tenant_wins_over_active_account(self, cli_fallback):
        # Pin names the tenant to search, even if the active account is elsewhere.
        _FakeCliCredential.working = {"user-sub"}
        cli_fallback.setattr(identity, "_ACTIVE_PIN", TenantPin(alias="contoso", id=TENANT))
        cli_fallback.setattr(
            identity,
            "active_account",
            lambda config_dir=None: _account("stray-sub", tenant=OTHER_TENANT),
        )
        cli_fallback.setattr(identity, "_enabled_accounts", _accounts(_account("user-sub")))
        assert get_access_token() == "token-user-sub"

    def test_no_fallback_when_active_identity_works(self, cli_fallback):
        _FakeCliCredential.working = {None}

        def _boom(_tenant_id):
            raise AssertionError("must not enumerate accounts on a healthy identity")

        cli_fallback.setattr(identity, "_enabled_accounts", _boom)
        assert get_access_token() == "token-None"
