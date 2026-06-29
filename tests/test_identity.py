"""Tests for the credential layer in fabric_deploy.identity (incl. relay path)."""

import base64
import json
import time

import pytest
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import AzureCliCredential

from fabric_deploy.identity import (
    StaticTokenCredential,
    _BEARER_ENV,
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
