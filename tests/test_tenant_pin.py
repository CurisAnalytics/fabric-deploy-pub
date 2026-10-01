"""Tests for the fabdep.yml tenant pin (multi-tenant identity guard).

Covers four layers:
- config parsing of the `tenant:` alias string into TenantPin
- TenantPin.resolve() filling id/config_dir/subscription/require_spn from the
  machine-local registry (written by `fabdep auth login` / `fabdep auth bind`)
- identity.assert_active_tenant tenant/identity checks
- WorkspaceDeployer wiring (alias resolved, AZURE_CONFIG_DIR set, assertion
  before any Fabric API call)
- dir-less (bound) pins selecting an identity from the shared az login
- the `fabdep auth bind` registry writer
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

import pytest

from fabric_deploy import identity
from fabric_deploy.auth import bind_tenant, login_tenant
from fabric_deploy.cli import handle_auth_command
from fabric_deploy.config import FabdepConfig, TenantPin
from fabric_deploy.identity import (
    AzureAccount,
    TenantMismatchError,
    active_account,
    apply_tenant_pin,
    assert_active_tenant,
    describe_tenants,
    tenant_registry,
)
from fabric_deploy.workspace import WorkspaceDeployer

FABRIKAM = "99999999-9999-9999-9999-999999999999"
CONTOSO = "dddddddd-dddd-dddd-dddd-dddddddddddd"
REGISTRY = {"fabrikam": {"id": FABRIKAM, "config_dir": "~/.azure-fabrikam"}}


def _seed_workspace(repo: Path) -> None:
    lh = repo / "test_lh.Lakehouse"
    lh.mkdir()
    (lh / ".platform").write_text(
        json.dumps({"metadata": {"displayName": "test_lh", "type": "Lakehouse"}})
    )


def _acct(
    tenant_id: str,
    user_type: str = "servicePrincipal",
    *,
    sub: str = "sub-123",
    name: str = "Some Account",
    default: bool = True,
) -> AzureAccount:
    return {  # type: ignore[return-value]
        "cloudName": "AzureCloud",
        "homeTenantId": tenant_id,
        "id": sub,
        "isDefault": default,
        "name": name,
        "state": "Enabled",
        "tenantId": tenant_id,
        "user": {"name": f"{sub}@{user_type}", "type": user_type},
    }


class _FakeCliCredential:
    def __init__(self, *, subscription=None, **_):
        self.subscription = subscription


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

    def test_misspelled_tenant_key_warns_and_is_ignored(self, tmp_path: Path, caplog):
        (tmp_path / "fabdep.yml").write_text('workspace_base_name: "ws"\ntenan: contoso\n')
        with caplog.at_level("WARNING"):
            cfg = FabdepConfig.from_path(tmp_path, branch_override="dev")
        assert cfg.tenant is None
        assert "unknown key 'tenan'" in caplog.text
        assert "did you mean 'tenant'?" in caplog.text

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

    def test_bound_subscription_from_registry(self):
        reg = {"contoso": {"id": CONTOSO, "subscription": "sub-contoso"}}
        pin = TenantPin(alias="contoso").resolve(reg)
        assert (pin.id, pin.config_dir, pin.subscription) == (CONTOSO, None, "sub-contoso")

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
        monkeypatch.setattr(
            "fabric_deploy.identity._list_accounts",
            lambda: [_acct(CONTOSO, "user", sub="s1", name="Contoso Personal")],
        )
        monkeypatch.setattr("fabric_deploy.identity.read_fabdep_profile", dict)
        monkeypatch.delenv("FABRIC_BEARER_TOKEN", raising=False)

        with caplog.at_level("WARNING"):
            WorkspaceDeployer(tmp_path, branch_override="dev")  # must not raise
        assert "no tenants are registered" in caplog.text
        assert f"{CONTOSO}  Contoso Personal [active]" in caplog.text
        assert "fabdep auth bind fabrikam <tenant-id>" in caplog.text

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


# --- dir-less (bound) pins: select from the shared az login ---------------


class TestBoundPinSelection:
    """`fabdep auth bind` entries have no config_dir: the pin must pick an
    identity out of the global `~/.azure` without rewriting az state."""

    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch):
        monkeypatch.setattr(identity, "_CLI_CREDENTIAL", None)
        monkeypatch.setattr(identity, "_ACTIVE_PIN", None)
        monkeypatch.setattr(identity, "AzureCliCredential", _FakeCliCredential)
        monkeypatch.setattr(identity, "read_fabdep_profile", lambda: {"aliases": {}})
        monkeypatch.delenv("FABRIC_BEARER_TOKEN", raising=False)
        monkeypatch.delenv("AZURE_CONFIG_DIR", raising=False)
        self.mp = monkeypatch

    def _registry(self, entry: dict) -> None:
        self.mp.setattr(identity, "tenant_registry", lambda: {"contoso": entry})

    def _az(self, active, *listed) -> None:
        self.mp.setattr(identity, "active_account", lambda config_dir=None: active)
        self.mp.setattr(identity, "_list_accounts", lambda: list(listed))

    @staticmethod
    def _logger():
        import logging

        return logging.getLogger("test.pin")

    def test_active_in_tenant_is_left_alone(self):
        self._registry({"id": CONTOSO})
        self._az(_acct(CONTOSO, "user"))

        apply_tenant_pin(TenantPin(alias="contoso"), self._logger())

        assert identity._CLI_CREDENTIAL is None
        assert "AZURE_CONFIG_DIR" not in os.environ

    def test_wrong_active_tenant_selects_sibling(self, caplog):
        self._registry({"id": CONTOSO})
        self._az(
            _acct(FABRIKAM, "user", sub="w1", name="Fabrikam Personal"),
            _acct(FABRIKAM, "user", sub="w1", name="Fabrikam Personal"),
            _acct(CONTOSO, "servicePrincipal", sub="c-spn", name="Contoso SPN", default=False),
            _acct(CONTOSO, "user", sub="c-user", name="Contoso Personal", default=False),
        )

        with caplog.at_level("INFO"):
            apply_tenant_pin(TenantPin(alias="contoso"), self._logger())

        assert identity._CLI_CREDENTIAL.subscription == "c-user"  # user login before SPN
        assert "using 'Contoso Personal'" in caplog.text
        assert "active az account unchanged" in caplog.text

    def test_log_line_honors_rename_labels(self, caplog):
        self._registry({"id": CONTOSO})
        self.mp.setattr(
            identity, "read_fabdep_profile", lambda: {"aliases": {"c-user": "contoso-me"}}
        )
        self._az(_acct(FABRIKAM, "user"), _acct(CONTOSO, "user", sub="c-user", default=False))

        with caplog.at_level("INFO"):
            apply_tenant_pin(TenantPin(alias="contoso"), self._logger())

        assert "using 'contoso-me'" in caplog.text

    def test_bound_subscription_is_pinned(self):
        self._registry({"id": CONTOSO, "subscription": "c-spn"})
        self._az(
            _acct(CONTOSO, "user", sub="c-user"),  # active, same tenant — still not the bound one
            _acct(CONTOSO, "user", sub="c-user"),
            _acct(CONTOSO, "servicePrincipal", sub="c-spn", default=False),
        )

        apply_tenant_pin(TenantPin(alias="contoso"), self._logger())

        assert identity._CLI_CREDENTIAL.subscription == "c-spn"

    def test_bound_subscription_active_is_noop(self):
        self._registry({"id": CONTOSO, "subscription": "c-spn"})
        self._az(_acct(CONTOSO, "servicePrincipal", sub="c-spn"))

        apply_tenant_pin(TenantPin(alias="contoso"), self._logger())

        assert identity._CLI_CREDENTIAL is None

    def test_require_spn_skips_user_logins(self):
        self._registry({"id": CONTOSO, "require_spn": True})
        self._az(
            _acct(CONTOSO, "user", sub="c-user"),  # active user in-tenant, but SPN required
            _acct(CONTOSO, "user", sub="c-user"),
            _acct(CONTOSO, "servicePrincipal", sub="c-spn", default=False),
        )

        apply_tenant_pin(TenantPin(alias="contoso"), self._logger())

        assert identity._CLI_CREDENTIAL.subscription == "c-spn"

    def test_no_identity_in_tenant_raises_with_listing(self):
        self._registry({"id": CONTOSO})
        self._az(_acct(FABRIKAM, "user", sub="w1", name="Fabrikam Personal"), _acct(FABRIKAM, "user", sub="w1", name="Fabrikam Personal"))

        with pytest.raises(TenantMismatchError) as exc:
            apply_tenant_pin(TenantPin(alias="contoso"), self._logger())

        msg = str(exc.value)
        assert f"No enabled az account in tenant {CONTOSO}" in msg
        assert f"{FABRIKAM}  Fabrikam Personal [active]" in msg
        assert f"az login --tenant {CONTOSO}" in msg
        assert identity._CLI_CREDENTIAL is None

    def test_bound_subscription_missing_raises(self):
        self._registry({"id": CONTOSO, "subscription": "gone"})
        self._az(_acct(CONTOSO, "user", sub="c-user"), _acct(CONTOSO, "user", sub="c-user"))

        with pytest.raises(TenantMismatchError, match="No enabled az subscription gone"):
            apply_tenant_pin(TenantPin(alias="contoso"), self._logger())

    def test_not_logged_in_raises(self):
        self._registry({"id": CONTOSO})
        self._az(None)

        with pytest.raises(TenantMismatchError, match="none \\(run `az login`\\)"):
            apply_tenant_pin(TenantPin(alias="contoso"), self._logger())

    def test_unknown_alias_error_lists_tenants(self):
        self.mp.setattr(identity, "tenant_registry", lambda: {"fabrikam": {"id": FABRIKAM}})
        self._az(_acct(CONTOSO, "user", sub="c1", name="Contoso Personal"), _acct(CONTOSO, "user", sub="c1", name="Contoso Personal"))

        with pytest.raises(ValueError, match="Unknown tenant alias 'contoso'") as exc:
            apply_tenant_pin(TenantPin(alias="contoso"), self._logger())
        assert f"{CONTOSO}  Contoso Personal" in str(exc.value)

    def test_config_dir_entry_still_uses_isolated_assertion(self):
        # The `fabdep auth login` path is untouched: no selection, env dir set.
        self._registry({"id": CONTOSO, "config_dir": "~/.azure-contoso"})
        self.mp.setattr(identity, "assert_active_tenant", lambda *a, **k: _acct(CONTOSO))
        self.mp.setattr(identity, "_list_accounts", lambda: pytest.fail("must not list"))

        apply_tenant_pin(TenantPin(alias="contoso"), self._logger())

        assert os.environ["AZURE_CONFIG_DIR"] == os.path.expanduser("~/.azure-contoso")
        assert identity._CLI_CREDENTIAL is None


class TestDescribeTenants:
    def test_groups_by_tenant_with_labels(self, monkeypatch):
        monkeypatch.setattr(
            identity, "read_fabdep_profile", lambda: {"aliases": {"c2": "Contoso SPN"}}
        )
        accounts = [
            _acct(FABRIKAM, "user", sub="w1", name="Fabrikam Personal"),
            _acct(CONTOSO, "user", sub="c1", name="Contoso Personal", default=False),
            _acct(CONTOSO, "servicePrincipal", sub="c2", name="N/A(tenant level)", default=False),
            {**_acct(CONTOSO, "user", sub="c3", name="Disabled", default=False), "state": "Disabled"},
        ]
        text = describe_tenants(accounts)  # type: ignore[arg-type]
        assert f"  {CONTOSO}  Contoso Personal, Contoso SPN" in text
        assert f"  {FABRIKAM}  Fabrikam Personal [active]" in text
        assert "Disabled" not in text

    def test_empty_says_log_in(self, monkeypatch):
        monkeypatch.setattr(identity, "read_fabdep_profile", dict)
        assert "none (run `az login`)" in describe_tenants([])


# --- fabdep auth bind (registry writer, no login) ---------------------------


class TestBindTenant:
    @pytest.fixture
    def profile(self, tmp_path, monkeypatch):
        p = tmp_path / "azureProfile.fabdep.json"
        monkeypatch.setattr("fabric_deploy.profile.FABDEP_PROFILE_PATH", p)
        return p

    @pytest.fixture(autouse=True)
    def _accounts(self, monkeypatch):
        listed = [
            _acct(CONTOSO, "user", sub="c-user", name="Contoso Personal"),
            _acct(CONTOSO, "servicePrincipal", sub="c-spn", name="Contoso SPN", default=False),
            _acct(FABRIKAM, "user", sub="w1", name="Fabrikam Personal", default=False),
            {**_acct(CONTOSO, "user", sub="c-dead", name="Old"), "state": "Disabled"},
        ]
        monkeypatch.setattr("fabric_deploy.auth.list_accounts", lambda: listed)
        monkeypatch.setattr(
            "fabric_deploy.auth.subprocess.run",
            lambda *a, **k: pytest.fail("bind must never run az"),
        )

    def test_tenant_guid_writes_id_only(self, profile):
        entry = bind_tenant("contoso", CONTOSO)
        assert entry == {"id": CONTOSO}
        assert json.loads(profile.read_text())["tenants"]["contoso"] == {"id": CONTOSO}

    def test_account_name_pins_subscription(self, profile):
        entry = bind_tenant("contoso", "contoso spn")  # case-insensitive, rename-aware name
        assert entry == {"id": CONTOSO, "subscription": "c-spn"}

    def test_subscription_id_pins_subscription(self, profile):
        assert bind_tenant("contoso", "c-user") == {"id": CONTOSO, "subscription": "c-user"}

    def test_disabled_account_not_bindable(self, profile):
        with pytest.raises(ValueError, match="not a tenant id, subscription id, or account name"):
            bind_tenant("contoso", "c-dead")

    def test_unknown_target_raises_with_listing(self, profile):
        with pytest.raises(ValueError) as exc:
            bind_tenant("contoso", "nope")
        assert f"{CONTOSO}  Contoso Personal [active], Contoso SPN" in str(exc.value)
        assert "az login --tenant" in str(exc.value)

    def test_rebind_replaces_login_entry_but_keeps_require_spn(self, profile):
        profile.write_text(json.dumps({"tenants": {"contoso": {
            "id": "old", "config_dir": "~/.azure-contoso", "app_id": "a", "cert_path": "c",
            "require_spn": True,
        }}}))
        entry = bind_tenant("contoso", CONTOSO)
        assert entry == {"id": CONTOSO, "require_spn": True}
        assert "config_dir" not in json.loads(profile.read_text())["tenants"]["contoso"]

    def test_cli_handler_prints_binding(self, profile, capsys):
        handle_auth_command(argparse.Namespace(auth_command="bind", alias="contoso", target="Contoso SPN"))
        assert f"registered tenant 'contoso' -> {CONTOSO} (account c-spn)" in capsys.readouterr().out

    def test_cli_handler_exits_nonzero_on_failure(self, profile, capsys):
        with pytest.raises(SystemExit):
            handle_auth_command(argparse.Namespace(auth_command="bind", alias="contoso", target="nope"))


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
