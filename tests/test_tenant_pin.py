"""Tests for the fabdep.yml tenant pin (multi-tenant identity guard).

Covers four layers:
- config parsing of the `tenant:` alias string into TenantPin
- TenantPin.resolve() filling id/config_dir/require_spn from the machine-local
  registry (written by `fabdep auth login`)
- identity.assert_active_tenant tenant/identity checks
- WorkspaceDeployer wiring (alias resolved, AZURE_CONFIG_DIR set, assertion
  before any Fabric API call)
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from fabric_deploy.auth import login_tenant
from fabric_deploy.config import FabdepConfig, TenantPin
from fabric_deploy.identity import (
    AzureAccount,
    TenantMismatchError,
    active_account,
    assert_active_tenant,
    tenant_registry,
)
from fabric_deploy.workspace import WorkspaceDeployer


FABRIKAM = "99999999-9999-9999-9999-999999999999"
REGISTRY = {"fabrikam": {"id": FABRIKAM, "config_dir": "~/.azure-fabrikam"}}


def _seed_workspace(repo: Path) -> None:
    lh = repo / "test_lh.Lakehouse"
    lh.mkdir()
    (lh / ".platform").write_text(
        json.dumps({"metadata": {"displayName": "test_lh", "type": "Lakehouse"}})
    )


def _acct(tenant_id: str, user_type: str = "servicePrincipal") -> AzureAccount:
    return {  # type: ignore[return-value]
        "cloudName": "AzureCloud",
        "homeTenantId": tenant_id,
        "id": "sub-123",
        "isDefault": True,
        "name": "Some Account",
        "state": "Enabled",
        "tenantId": tenant_id,
        "user": {"name": "spn@app", "type": user_type},
    }


# --- config parsing --------------------------------------------------------


class TestTenantPinParsing:
    def test_alias_string(self):
        assert TenantPin.from_yaml_value("contoso") == TenantPin(alias="contoso")

    @pytest.mark.parametrize("bad", [{}, {"id": FABRIKAM}, None, 123, [], ""])
    def test_non_alias_raises(self, bad):
        with pytest.raises(ValueError, match="must be a registry alias string"):
            TenantPin.from_yaml_value(bad)

    def test_from_path_alias_string(self, tmp_path: Path):
        (tmp_path / "fabdep.yml").write_text(
            'workspace_base_name: "ws"\ntenant: contoso\n'
        )
        cfg = FabdepConfig.from_path(tmp_path, branch_override="dev")
        assert cfg.tenant == TenantPin(alias="contoso")

    def test_from_path_no_tenant_is_none(self, tmp_path: Path):
        (tmp_path / "fabdep.yml").write_text('workspace_base_name: "ws"\n')
        cfg = FabdepConfig.from_path(tmp_path, branch_override="dev")
        assert cfg.tenant is None


# --- alias resolution ------------------------------------------------------


class TestTenantPinResolve:
    def test_alias_fills_from_registry(self):
        resolved = TenantPin(alias="fabrikam").resolve(REGISTRY)
        assert resolved == TenantPin(
            alias="fabrikam", id=FABRIKAM, config_dir="~/.azure-fabrikam"
        )

    def test_require_spn_from_registry(self):
        reg = {"fabrikam": {"id": FABRIKAM, "config_dir": "~/.azure-fabrikam", "require_spn": True}}
        assert TenantPin(alias="fabrikam").resolve(reg).require_spn is True

    def test_unknown_alias_raises(self):
        with pytest.raises(ValueError, match="Unknown tenant alias 'nope'"):
            TenantPin(alias="nope").resolve(REGISTRY)

    def test_registry_entry_without_id_raises(self):
        with pytest.raises(ValueError, match="registered without an 'id'"):
            TenantPin(alias="broken").resolve({"broken": {"config_dir": "~/.x"}})


# --- active_account subprocess contract ------------------------------------


class TestActiveAccount:
    def test_returns_none_on_nonzero_exit(self, monkeypatch):
        monkeypatch.setattr(
            "fabric_deploy.identity.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="no login"),
        )
        assert active_account() is None

    def test_returns_none_on_garbage_stdout(self, monkeypatch):
        monkeypatch.setattr(
            "fabric_deploy.identity.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="not json", stderr=""),
        )
        assert active_account() is None


# --- auth assertion --------------------------------------------------------


class TestAssertActiveTenant:
    def test_match_returns_account(self, monkeypatch):
        monkeypatch.setattr(
            "fabric_deploy.identity.active_account", lambda config_dir=None: _acct(FABRIKAM)
        )
        acct = assert_active_tenant(FABRIKAM)
        assert acct["tenantId"] == FABRIKAM

    def test_mismatch_raises(self, monkeypatch):
        monkeypatch.setattr(
            "fabric_deploy.identity.active_account",
            lambda config_dir=None: _acct("11111111-1111-1111-1111-111111111111"),
        )
        with pytest.raises(TenantMismatchError, match="pins"):
            assert_active_tenant(FABRIKAM)

    def test_not_logged_in_raises(self, monkeypatch):
        monkeypatch.setattr(
            "fabric_deploy.identity.active_account", lambda config_dir=None: None
        )
        with pytest.raises(TenantMismatchError, match="No active Azure login"):
            assert_active_tenant(FABRIKAM, config_dir="~/.azure-fabrikam")

    def test_require_spn_rejects_user(self, monkeypatch):
        monkeypatch.setattr(
            "fabric_deploy.identity.active_account",
            lambda config_dir=None: _acct(FABRIKAM, user_type="user"),
        )
        with pytest.raises(TenantMismatchError, match="requires a service principal"):
            assert_active_tenant(FABRIKAM, require_spn=True)

    def test_require_spn_accepts_spn(self, monkeypatch):
        monkeypatch.setattr(
            "fabric_deploy.identity.active_account",
            lambda config_dir=None: _acct(FABRIKAM, user_type="servicePrincipal"),
        )
        assert assert_active_tenant(FABRIKAM, require_spn=True)["tenantId"] == FABRIKAM


# --- workspace wiring ------------------------------------------------------


class TestDeployerTenantPin:
    def _write(self, repo: Path, tenant_block: str = "") -> None:
        _seed_workspace(repo)
        (repo / "fabdep.yml").write_text('workspace_base_name: "ws"\n' + tenant_block)

    def test_alias_resolves_sets_config_dir_and_asserts(
        self, tmp_path: Path, monkeypatch, mock_fabric_api
    ):
        self._write(tmp_path, "tenant: fabrikam\n")
        calls = {}

        def _spy(tenant_id, *, config_dir=None, require_spn=False):
            calls.update(id=tenant_id, config_dir=config_dir, require_spn=require_spn)
            return _acct(FABRIKAM)

        monkeypatch.setattr("fabric_deploy.identity.tenant_registry", lambda: REGISTRY)
        monkeypatch.setattr("fabric_deploy.identity.assert_active_tenant", _spy)
        monkeypatch.delenv("AZURE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("FABRIC_BEARER_TOKEN", raising=False)

        WorkspaceDeployer(tmp_path, branch_override="dev")

        assert calls == {"id": FABRIKAM, "config_dir": "~/.azure-fabrikam", "require_spn": False}
        assert os.environ["AZURE_CONFIG_DIR"] == os.path.expanduser("~/.azure-fabrikam")

    def test_require_spn_threads_through_from_registry(
        self, tmp_path: Path, monkeypatch, mock_fabric_api
    ):
        self._write(tmp_path, "tenant: fabrikam\n")
        reg = {"fabrikam": {"id": FABRIKAM, "config_dir": "~/.azure-fabrikam", "require_spn": True}}
        calls = {}

        def _spy(tenant_id, *, config_dir=None, require_spn=False):
            calls.update(require_spn=require_spn)
            return _acct(FABRIKAM)

        monkeypatch.setattr("fabric_deploy.identity.tenant_registry", lambda: reg)
        monkeypatch.setattr("fabric_deploy.identity.assert_active_tenant", _spy)
        monkeypatch.delenv("FABRIC_BEARER_TOKEN", raising=False)

        WorkspaceDeployer(tmp_path, branch_override="dev")

        assert calls["require_spn"] is True

    def test_pin_overrides_existing_config_dir(
        self, tmp_path: Path, monkeypatch, mock_fabric_api
    ):
        self._write(tmp_path, "tenant: fabrikam\n")
        monkeypatch.setattr("fabric_deploy.identity.tenant_registry", lambda: REGISTRY)
        monkeypatch.setattr(
            "fabric_deploy.identity.assert_active_tenant",
            lambda *a, **k: _acct(FABRIKAM),
        )
        monkeypatch.setenv("AZURE_CONFIG_DIR", "/some/other/dir")
        monkeypatch.delenv("FABRIC_BEARER_TOKEN", raising=False)

        WorkspaceDeployer(tmp_path, branch_override="dev")

        assert os.environ["AZURE_CONFIG_DIR"] == os.path.expanduser("~/.azure-fabrikam")

    def test_config_dir_same_value_is_noop(
        self, tmp_path: Path, monkeypatch, mock_fabric_api
    ):
        self._write(tmp_path, "tenant: fabrikam\n")
        monkeypatch.setattr("fabric_deploy.identity.tenant_registry", lambda: REGISTRY)
        monkeypatch.setattr(
            "fabric_deploy.identity.assert_active_tenant",
            lambda *a, **k: _acct(FABRIKAM),
        )
        monkeypatch.setenv("AZURE_CONFIG_DIR", os.path.expanduser("~/.azure-fabrikam"))
        monkeypatch.delenv("FABRIC_BEARER_TOKEN", raising=False)

        WorkspaceDeployer(tmp_path, branch_override="dev")

        assert os.environ["AZURE_CONFIG_DIR"] == os.path.expanduser("~/.azure-fabrikam")

    def test_unknown_alias_propagates(self, tmp_path: Path, monkeypatch, mock_fabric_api):
        self._write(tmp_path, "tenant: bogus\n")
        monkeypatch.setattr("fabric_deploy.identity.tenant_registry", lambda: REGISTRY)
        monkeypatch.delenv("FABRIC_BEARER_TOKEN", raising=False)

        with pytest.raises(ValueError, match="Unknown tenant alias 'bogus'"):
            WorkspaceDeployer(tmp_path, branch_override="dev")

    def test_empty_registry_warns_and_runs_unguarded(
        self, tmp_path: Path, monkeypatch, mock_fabric_api, caplog
    ):
        # A machine that never ran `fabdep auth login` shouldn't be hard-blocked
        # by a committed alias — it falls back to the active login with a warning.
        self._write(tmp_path, "tenant: fabrikam\n")

        def _fail(*a, **k):
            raise AssertionError("assertion must be skipped with an empty registry")

        monkeypatch.setattr("fabric_deploy.identity.tenant_registry", dict)
        monkeypatch.setattr("fabric_deploy.identity.assert_active_tenant", _fail)
        monkeypatch.delenv("FABRIC_BEARER_TOKEN", raising=False)

        with caplog.at_level("WARNING"):
            WorkspaceDeployer(tmp_path, branch_override="dev")  # must not raise
        assert "no tenants are registered" in caplog.text

    def test_mismatch_propagates(self, tmp_path: Path, monkeypatch, mock_fabric_api):
        self._write(tmp_path, "tenant: fabrikam\n")

        def _boom(tenant_id, *, config_dir=None, require_spn=False):
            raise TenantMismatchError("wrong tenant")

        monkeypatch.setattr("fabric_deploy.identity.tenant_registry", lambda: REGISTRY)
        monkeypatch.setattr("fabric_deploy.identity.assert_active_tenant", _boom)
        monkeypatch.delenv("FABRIC_BEARER_TOKEN", raising=False)

        with pytest.raises(TenantMismatchError, match="wrong tenant"):
            WorkspaceDeployer(tmp_path, branch_override="dev")

    def test_no_pin_skips_assertion(self, tmp_path: Path, monkeypatch, mock_fabric_api):
        self._write(tmp_path)  # no tenant block

        def _fail(*a, **k):
            raise AssertionError("assert_active_tenant must not be called without a pin")

        monkeypatch.setattr("fabric_deploy.identity.assert_active_tenant", _fail)
        WorkspaceDeployer(tmp_path, branch_override="dev")  # must not raise

    def test_bearer_token_skips_assertion_but_sets_config_dir(
        self, tmp_path: Path, monkeypatch, mock_fabric_api
    ):
        self._write(tmp_path, "tenant: fabrikam\n")

        def _fail(*a, **k):
            raise AssertionError("assertion must be skipped under FABRIC_BEARER_TOKEN")

        monkeypatch.setattr("fabric_deploy.identity.tenant_registry", lambda: REGISTRY)
        monkeypatch.setattr("fabric_deploy.identity.assert_active_tenant", _fail)
        monkeypatch.setenv("FABRIC_BEARER_TOKEN", "tok")
        monkeypatch.delenv("AZURE_CONFIG_DIR", raising=False)

        WorkspaceDeployer(tmp_path, branch_override="dev")  # must not raise
        assert os.environ["AZURE_CONFIG_DIR"] == os.path.expanduser("~/.azure-fabrikam")


# --- fabdep auth login (registry writer) -----------------------------------


class TestLoginTenant:
    @pytest.fixture
    def profile(self, tmp_path, monkeypatch):
        p = tmp_path / "azureProfile.fabdep.json"
        monkeypatch.setattr("fabric_deploy.profile.FABDEP_PROFILE_PATH", p)
        return p

    @staticmethod
    def _capture_run(calls):
        def _run(cmd, **kwargs):
            calls["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0)
        return _run

    def test_spn_first_login_writes_full_entry(self, tmp_path, monkeypatch, profile):
        cert = tmp_path / "spn.pem"
        cert.write_text("x")
        calls = {}
        monkeypatch.setattr("fabric_deploy.auth.subprocess.run", self._capture_run(calls))
        monkeypatch.setattr("fabric_deploy.auth.active_account", lambda cfg=None: _acct(FABRIKAM))

        entry = login_tenant(
            "fabrikam", app_id="app-1", tenant=FABRIKAM, cert=str(cert),
            config_dir=str(tmp_path / ".azure-fabrikam"),
        )

        assert entry["id"] == FABRIKAM
        assert entry["app_id"] == "app-1" and entry["cert_path"] == str(cert)
        assert "--service-principal" in calls["cmd"]
        assert tenant_registry()["fabrikam"]["id"] == FABRIKAM  # persisted

    def test_relogin_reuses_stored_params(self, tmp_path, monkeypatch, profile):
        cert = tmp_path / "spn.pem"
        cert.write_text("x")
        profile.write_text(json.dumps({"tenants": {"fabrikam": {
            "id": FABRIKAM, "config_dir": str(tmp_path / ".azure-fabrikam"),
            "app_id": "app-1", "cert_path": str(cert)}}}))
        calls = {}
        monkeypatch.setattr("fabric_deploy.auth.subprocess.run", self._capture_run(calls))
        monkeypatch.setattr("fabric_deploy.auth.active_account", lambda cfg=None: _acct(FABRIKAM))

        login_tenant("fabrikam")  # no flags — reuse stored

        assert "app-1" in calls["cmd"] and str(cert) in calls["cmd"]

    def test_missing_params_raises(self, tmp_path, monkeypatch, profile):
        with pytest.raises(ValueError, match="First login for tenant 'new'"):
            login_tenant("new")

    def test_user_mode_omits_spn_fields(self, tmp_path, monkeypatch, profile):
        calls = {}
        monkeypatch.setattr("fabric_deploy.auth.subprocess.run", self._capture_run(calls))
        monkeypatch.setattr("fabric_deploy.auth.active_account", lambda cfg=None: _acct(FABRIKAM))

        entry = login_tenant("contoso", user=True, config_dir=str(tmp_path / ".azure-contoso"))

        assert entry["id"] == FABRIKAM
        assert "app_id" not in entry and "cert_path" not in entry
        assert "--service-principal" not in calls["cmd"]
