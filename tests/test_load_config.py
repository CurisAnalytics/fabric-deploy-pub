"""How fabdep.yml and the branch resolve to a target workspace."""

from __future__ import annotations

from pathlib import Path

import pytest

from fabric_deploy.workspace import WorkspaceDeployer

# --- helpers ---------------------------------------------------------------


def _write_fabdep_yml(repo: Path, body: str) -> None:
    (repo / "fabdep.yml").write_text(body)


# --- tests ---------------------------------------------------------------


class TestLoadConfig:
    def test_yaml_present_resolves_workspace(self, tmp_path: Path, mock_fabric_api):
        _write_fabdep_yml(tmp_path, 'workspace_base_name: "myws"\n')

        d = WorkspaceDeployer(tmp_path, branch_override="prod")

        assert d.workspace_name == "myws [PROD]"
        assert d.workspace_id == mock_fabric_api.workspace_id

    def test_missing_fabdep_yml_raises_before_any_api_call(self, tmp_path: Path, monkeypatch):
        """The folder name is no stand-in for the workspace name."""
        monkeypatch.setattr(
            "fabric_deploy.workspace.get_workspace_id_by_name",
            lambda _name: pytest.fail("looked up a workspace without a fabdep.yml"),
        )
        with pytest.raises(FileNotFoundError, match="(?s)No fabdep.yml in .*workspace_base_name"):
            WorkspaceDeployer(tmp_path, branch_override="dev")

    def test_yaml_missing_workspace_base_name_raises(self, tmp_path: Path, mock_fabric_api):
        # Empty fabdep.yml — yaml.safe_load returns None → handled as {}
        _write_fabdep_yml(tmp_path, "")

        with pytest.raises(ValueError, match="must set workspace_base_name"):
            WorkspaceDeployer(tmp_path, branch_override="prod")

    def test_branch_override_skips_git(self, tmp_path: Path, monkeypatch, mock_fabric_api):
        """When branch_override is set, git must not be consulted at all."""
        _write_fabdep_yml(tmp_path, 'workspace_base_name: "ws"\n')

        # config.py resolves the branch via git.current_branch — if it's
        # invoked despite the override, this raises and the test fails.
        def _boom(repo_dir):
            raise AssertionError("current_branch should not be called when branch_override is set")

        monkeypatch.setattr("fabric_deploy.config.current_branch", _boom)

        d = WorkspaceDeployer(tmp_path, branch_override="staging")

        assert d.workspace_name == "ws [STAGING]"

    def test_new_workspace_creates_via_capacity(self, tmp_path: Path, mock_fabric_api):
        _write_fabdep_yml(
            tmp_path,
            f'workspace_base_name: "newws"\ncapacity_id: "{mock_fabric_api.capacity_id}"\n',
        )

        d = WorkspaceDeployer(tmp_path, branch_override="dev", new_workspace=True)

        assert d.workspace_name == "newws [DEV]"
        # Workspace ID still resolved via the (mocked) lookup after creation.
        assert d.workspace_id == mock_fabric_api.workspace_id
