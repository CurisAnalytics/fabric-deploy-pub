"""fabdep.yml's identity keys (`tenant:`, `auth:`, `require_spn:`) and how deploys adopt them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fabric_deploy.config import AuthSettings, FabdepConfig, load_auth_settings
from fabric_deploy.identity import FABRIC_SCOPE, AuthError, fabric_credential
from fabric_deploy.workspace import WorkspaceDeployer

from .conftest import CONTOSO_TENANT, FABRIKAM_TENANT


def _write(repo: Path, body: str) -> None:
    (repo / "fabdep.yml").write_text(f'workspace_base_name: "ws"\n{body}', encoding="utf-8")


def _config(repo: Path, body: str) -> FabdepConfig:
    _write(repo, body)
    return FabdepConfig.from_path(repo, branch_override="dev")


class TestParsing:
    def test_tenant_guid(self, tmp_path):
        auth = _config(tmp_path, f"tenant: {CONTOSO_TENANT.upper()}\n").auth
        assert auth == AuthSettings(tenant=CONTOSO_TENANT)

    @pytest.mark.parametrize("bad", ["contoso", "contoso.onmicrosoft.com", "123", "[1]"])
    def test_tenant_must_be_a_guid(self, tmp_path, bad):
        with pytest.raises(ValueError, match=r"tenant ID \(a GUID\)") as exc:
            _ = _config(tmp_path, f"tenant: {bad}\n")
        assert "az account show --query tenantId" in str(exc.value)

    def test_auth_and_require_spn(self, tmp_path):
        auth = _config(tmp_path, "auth: workload\nrequire_spn: true\n").auth
        assert auth == AuthSettings(mode="workload", require_spn=True)

    def test_unknown_auth_mode_lists_the_modes(self, tmp_path):
        with pytest.raises(ValueError, match="cli, env, workload, managed, default"):
            _ = _config(tmp_path, "auth: azcli\n")

    def test_require_spn_must_be_a_boolean(self, tmp_path):
        with pytest.raises(ValueError, match="require_spn"):
            _ = _config(tmp_path, 'require_spn: "yes"\n')

    def test_no_identity_keys(self, tmp_path):
        assert _config(tmp_path, "").auth is None

    def test_misspelled_key_warns_and_is_ignored(self, tmp_path, caplog):
        with caplog.at_level("WARNING"):
            cfg = _config(tmp_path, f"tenan: {CONTOSO_TENANT}\n")
        assert cfg.auth is None
        assert "unknown key 'tenan'" in caplog.text
        assert "did you mean 'tenant'?" in caplog.text

    def test_load_without_fabdep_yml(self, tmp_path):
        assert load_auth_settings(tmp_path) is None

    def test_load_reads_only_the_identity_keys(self, tmp_path):
        (tmp_path / "fabdep.yml").write_text(f"tenant: {CONTOSO_TENANT}\nauth: env\n")
        assert load_auth_settings(tmp_path) == AuthSettings(tenant=CONTOSO_TENANT, mode="env")


class TestDeployerAdoptsTheSettings:
    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        lh = tmp_path / "lh.Lakehouse"
        lh.mkdir()
        (lh / ".platform").write_text(
            json.dumps({"metadata": {"displayName": "lh", "type": "Lakehouse"}})
        )
        return tmp_path

    def test_pinned_tenant_and_mode_reach_the_credential(self, repo, mock_fabric_api, credential):
        _write(repo, f"tenant: {CONTOSO_TENANT}\nauth: env\n")
        _ = WorkspaceDeployer(repo, branch_override="dev")
        _ = fabric_credential().get_token(FABRIC_SCOPE)
        assert credential.built == [("env", CONTOSO_TENANT)]

    def test_wrong_tenant_token_is_refused(self, repo, mock_fabric_api, credential):
        credential.claims["tid"] = FABRIKAM_TENANT
        _write(repo, f"tenant: {CONTOSO_TENANT}\n")
        _ = WorkspaceDeployer(repo, branch_override="dev")
        with pytest.raises(AuthError, match=f"pins tenant {CONTOSO_TENANT}"):
            _ = fabric_credential().get_token(FABRIC_SCOPE)

    def test_bad_auth_mode_fails_before_any_api_call(self, repo, monkeypatch, mock_fabric_api):
        _write(repo, "")
        monkeypatch.setenv("FABDEP_AUTH", "azcli")
        monkeypatch.setattr(
            "fabric_deploy.workspace.get_workspace_id_by_name",
            lambda _name: pytest.fail("workspace lookup ran before the auth check"),
        )
        with pytest.raises(AuthError, match="FABDEP_AUTH"):
            _ = WorkspaceDeployer(repo, branch_override="dev")
