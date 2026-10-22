"""Credential selection, the tenant/SPN guard, token caching and the token relay."""

from __future__ import annotations

import logging
import time

import pytest

from fabric_deploy import identity
from fabric_deploy.config import TenantPin
from fabric_deploy.identity import (
    FABRIC_SCOPE,
    POWERBI_SCOPE,
    AuthError,
    apply_tenant_pin,
    auth_mode,
    fabric_credential,
    get_access_token,
    set_auth_mode,
)

from .conftest import APP_ID, CONTOSO_TENANT, FABRIKAM_TENANT, make_jwt

LOG = logging.getLogger("fabric_deploy.test")
# Captured at import, before the autouse fixture swaps in a stub that refuses to sign in.
REAL_NEW_CREDENTIAL = identity._new_credential
APP_CLAIMS = {"tid": CONTOSO_TENANT, "idtyp": "app", "appid": APP_ID}


class TestModeSelection:
    @pytest.mark.parametrize(
        ("flag", "env", "yml", "expected"),
        [
            (None, None, None, ("cli", "default")),
            (None, None, "managed", ("managed", "fabdep.yml")),
            (None, "env", "managed", ("env", "FABDEP_AUTH")),
            ("workload", "env", "managed", ("workload", "--auth")),
        ],
    )
    def test_flag_then_env_then_fabdep_yml_then_cli(self, monkeypatch, flag, env, yml, expected):
        if env:
            monkeypatch.setenv("FABDEP_AUTH", env)
        set_auth_mode(flag)
        apply_tenant_pin(TenantPin(auth=yml) if yml else None, LOG)
        assert auth_mode() == expected

    def test_unknown_env_mode_fails_before_any_token(self, monkeypatch):
        monkeypatch.setenv("FABDEP_AUTH", "azcli")
        with pytest.raises(AuthError, match="FABDEP_AUTH='azcli'.*cli, env, workload, managed"):
            apply_tenant_pin(None, LOG)

    def test_mode_and_pinned_tenant_reach_the_credential(self, credential):
        apply_tenant_pin(TenantPin(id=CONTOSO_TENANT, auth="workload"), LOG)
        _ = get_access_token()
        assert credential.built == [("workload", CONTOSO_TENANT)]


class TestCredentialFactory:
    """Each mode builds its azure-identity class, pinned to the tenant where the class allows."""

    @pytest.fixture(autouse=True)
    def _record(self, monkeypatch):
        self.made: list[tuple[str, dict]] = []

        def recorder(name: str):
            def make(**kwargs):
                self.made.append((name, kwargs))
                return object()

            return make

        for name in (
            "AzureCliCredential",
            "EnvironmentCredential",
            "WorkloadIdentityCredential",
            "ManagedIdentityCredential",
            "DefaultAzureCredential",
        ):
            monkeypatch.setattr(identity, name, recorder(name))
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            ("cli", ("AzureCliCredential", {"tenant_id": CONTOSO_TENANT})),
            ("env", ("EnvironmentCredential", {})),
            ("workload", ("WorkloadIdentityCredential", {"tenant_id": CONTOSO_TENANT})),
            ("managed", ("ManagedIdentityCredential", {"client_id": None})),
            ("default", ("DefaultAzureCredential", {})),
        ],
    )
    def test_pinned_tenant(self, mode, expected):
        _ = REAL_NEW_CREDENTIAL(mode, CONTOSO_TENANT)
        assert self.made == [expected]

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            ("cli", ("AzureCliCredential", {"tenant_id": ""})),  # whatever az signed in to
            ("workload", ("WorkloadIdentityCredential", {"tenant_id": None})),  # AZURE_TENANT_ID
        ],
    )
    def test_unpinned_tenant(self, mode, expected):
        _ = REAL_NEW_CREDENTIAL(mode, None)
        assert self.made == [expected]

    def test_managed_uses_a_user_assigned_identity_from_env(self, monkeypatch):
        monkeypatch.setenv("AZURE_CLIENT_ID", APP_ID)
        _ = REAL_NEW_CREDENTIAL("managed", None)
        assert self.made == [("ManagedIdentityCredential", {"client_id": APP_ID})]


class TestSignInFailures:
    """Real azure-identity credentials that fail locally, without any network call."""

    @pytest.fixture(autouse=True)
    def _real_credentials(self, monkeypatch, tmp_path):
        monkeypatch.setattr(identity, "_new_credential", REAL_NEW_CREDENTIAL)
        for var in (
            "AZURE_TENANT_ID",
            "AZURE_CLIENT_ID",
            "AZURE_CLIENT_SECRET",
            "AZURE_CLIENT_CERTIFICATE_PATH",
            "AZURE_FEDERATED_TOKEN_FILE",
            "AZURE_USERNAME",
            "AZURE_PASSWORD",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("PATH", str(tmp_path))  # no `az` to find

    def test_cli_without_az_says_how_to_sign_in(self):
        apply_tenant_pin(TenantPin(id=CONTOSO_TENANT), LOG)
        with pytest.raises(AuthError, match="cli credential") as exc:
            _ = get_access_token()
        assert f"az login --tenant {CONTOSO_TENANT}" in str(exc.value)

    def test_env_lists_the_variables_to_set(self):
        set_auth_mode("env")
        with pytest.raises(AuthError, match="AZURE_CLIENT_SECRET"):
            _ = get_access_token()

    def test_workload_lists_the_variables_to_set(self):
        set_auth_mode("workload")
        with pytest.raises(AuthError, match="AZURE_FEDERATED_TOKEN_FILE"):
            _ = get_access_token()


class TestIdentityGuard:
    def test_pinned_tenant_matches_case_insensitively(self, credential):
        credential.claims["tid"] = CONTOSO_TENANT.upper()
        apply_tenant_pin(TenantPin(id=CONTOSO_TENANT), LOG)
        assert get_access_token()

    def test_wrong_tenant_names_both_tenants_and_the_fix(self, credential):
        credential.claims["tid"] = FABRIKAM_TENANT
        apply_tenant_pin(TenantPin(id=CONTOSO_TENANT), LOG)
        with pytest.raises(AuthError) as exc:
            _ = get_access_token()
        msg = str(exc.value)
        assert f"user@contoso.com in tenant {FABRIKAM_TENANT}" in msg
        assert f"fabdep.yml pins tenant {CONTOSO_TENANT}" in msg
        assert f"az login --tenant {CONTOSO_TENANT}" in msg

    def test_rejected_token_is_not_cached(self, credential):
        credential.claims["tid"] = FABRIKAM_TENANT
        apply_tenant_pin(TenantPin(id=CONTOSO_TENANT), LOG)
        for _ in range(2):
            with pytest.raises(AuthError):
                _ = get_access_token()
        assert len(credential.calls) == 2

    def test_unpinned_accepts_any_tenant(self, credential):
        credential.claims["tid"] = FABRIKAM_TENANT
        apply_tenant_pin(None, LOG)
        assert get_access_token()

    def test_require_spn_rejects_a_user(self, credential):
        apply_tenant_pin(TenantPin(require_spn=True), LOG)
        with pytest.raises(AuthError, match="require_spn: true.*user user@contoso.com"):
            _ = get_access_token()

    @pytest.mark.parametrize(
        "claims",
        [APP_CLAIMS, {"tid": CONTOSO_TENANT, "appid": APP_ID}],  # no idtyp, no scp: app-only
        ids=["idtyp", "no-scp"],
    )
    def test_require_spn_accepts_an_app_token(self, credential, claims):
        credential.claims = claims
        apply_tenant_pin(TenantPin(id=CONTOSO_TENANT, require_spn=True), LOG)
        assert get_access_token()


class TestTokenCache:
    def test_one_token_per_scope(self, credential):
        for scope in (FABRIC_SCOPE, FABRIC_SCOPE, POWERBI_SCOPE, POWERBI_SCOPE):
            _ = get_access_token(scope)
        assert credential.calls == [(FABRIC_SCOPE,), (POWERBI_SCOPE,)]

    def test_renews_a_token_close_to_expiry(self, credential):
        credential.lifetime = 120
        _ = get_access_token()
        _ = get_access_token()
        assert len(credential.calls) == 2

    def test_fabric_cicd_shares_the_rest_calls_credential(self, credential):
        assert fabric_credential().get_token(FABRIC_SCOPE).token == get_access_token()
        assert credential.calls == [(FABRIC_SCOPE,)]


class TestRelay:
    def test_used_instead_of_signing_in(self, monkeypatch, caplog):
        token = make_jwt(tid=CONTOSO_TENANT)
        monkeypatch.setenv("FABRIC_BEARER_TOKEN", f"\n  {token}\n")
        with caplog.at_level(logging.INFO, logger="fabric_deploy"):
            assert get_access_token() == token
            assert fabric_credential().get_token(FABRIC_SCOPE).token == token
        assert caplog.text.count("FABRIC_BEARER_TOKEN") == 1

    @pytest.mark.parametrize("powerbi_set", [True, False])
    def test_power_bi_prefers_its_own_token(self, monkeypatch, powerbi_set):
        fabric, powerbi = make_jwt(aud="fabric"), make_jwt(aud="powerbi")
        monkeypatch.setenv("FABRIC_BEARER_TOKEN", fabric)
        if powerbi_set:
            monkeypatch.setenv("POWERBI_BEARER_TOKEN", powerbi)
        assert get_access_token(POWERBI_SCOPE) == (powerbi if powerbi_set else fabric)
        assert get_access_token(FABRIC_SCOPE) == fabric

    def test_other_scopes_still_sign_in(self, monkeypatch, credential):
        monkeypatch.setenv("FABRIC_BEARER_TOKEN", make_jwt())
        _ = get_access_token("https://storage.azure.com/.default")
        assert credential.calls == [("https://storage.azure.com/.default",)]

    def test_expired_token_says_what_to_do(self, monkeypatch):
        monkeypatch.setenv("FABRIC_BEARER_TOKEN", make_jwt(exp=int(time.time()) - 60))
        with pytest.raises(AuthError, match="FABRIC_BEARER_TOKEN has expired") as exc:
            _ = get_access_token()
        assert "az account get-access-token" in str(exc.value)

    def test_checked_against_the_pin(self, monkeypatch):
        monkeypatch.setenv("FABRIC_BEARER_TOKEN", make_jwt(tid=FABRIKAM_TENANT))
        apply_tenant_pin(TenantPin(id=CONTOSO_TENANT), LOG)
        with pytest.raises(AuthError, match="FABRIC_BEARER_TOKEN"):
            _ = get_access_token()

    def test_malformed_token(self, monkeypatch):
        monkeypatch.setenv("FABRIC_BEARER_TOKEN", "not-a-jwt")
        with pytest.raises(AuthError, match="not a JWT"):
            _ = get_access_token()
