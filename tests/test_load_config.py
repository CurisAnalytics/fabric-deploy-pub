"""Unit tests for WorkspaceDeployer._load_config branches.

Covers:
- fabdep.yml present and valid
- fabdep.yml missing → dirname fallback succeeds via Fabric API
- fabdep.yml missing → dirname fallback fails → FileNotFoundError
- fabdep.yml missing workspace_base_name → ValueError
- branch_override takes precedence over git
- new_workspace=True triggers create_workspace
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fabric_deploy.workspace import WorkspaceDeployer


# --- helpers ---------------------------------------------------------------


def _seed_workspace(repo: Path, *, lakehouse: bool = True) -> None:
    """Create the minimum filesystem layout that satisfies WorkspaceDeployer
    init checks (a .Lakehouse dir with a .platform file)."""
    if lakehouse:
        lh = repo / "test_lh.Lakehouse"
        lh.mkdir()
        (lh / ".platform").write_text(
            json.dumps({"metadata": {"displayName": "test_lh", "type": "Lakehouse"}})
        )


def _write_fabdep_yml(repo: Path, body: str) -> None:
    (repo / "fabdep.yml").write_text(body)


# --- tests ---------------------------------------------------------------


class TestLoadConfig:
    def test_yaml_present_resolves_workspace(
        self, tmp_path: Path, mock_fabric_api
    ):
        _seed_workspace(tmp_path)
        _write_fabdep_yml(tmp_path, 'workspace_base_name: "myws"\n')

        d = WorkspaceDeployer(tmp_path, branch_override="prod")

        assert d.workspace_name == "myws [PROD]"
        assert d.workspace_id == mock_fabric_api.workspace_id

    def test_dirname_fallback_succeeds(self, tmp_path: Path, mock_fabric_api):
        # No fabdep.yml. The deployer should fall back to the directory name.
        repo = tmp_path / "fallback_ws"
        repo.mkdir()
        _seed_workspace(repo)

        d = WorkspaceDeployer(repo, branch_override="dev")

        assert d.workspace_name == "fallback_ws [DEV]"
        assert d.workspace_id == mock_fabric_api.workspace_id

    def test_dirname_fallback_fails_raises_filenotfound(
        self, tmp_path: Path, monkeypatch, mock_fabric_api
    ):
        # No fabdep.yml AND the directory-name workspace doesn't exist either.
        repo = tmp_path / "missing_ws"
        repo.mkdir()
        _seed_workspace(repo)

        def _missing(_name: str) -> str:
            raise ValueError("Workspace not found: 'missing_ws [DEV]'")

        monkeypatch.setattr(
            "fabric_deploy.workspace.get_workspace_id_by_name", _missing
        )

        with pytest.raises(FileNotFoundError, match="fabdep.yml not found"):
            WorkspaceDeployer(repo, branch_override="dev")

    def test_yaml_missing_workspace_base_name_raises(
        self, tmp_path: Path, mock_fabric_api
    ):
        _seed_workspace(tmp_path)
        # Empty fabdep.yml — yaml.safe_load returns None → handled as {}
        _write_fabdep_yml(tmp_path, "")

        with pytest.raises(ValueError, match="No workspace_base_name found"):
            WorkspaceDeployer(tmp_path, branch_override="prod")

    def test_branch_override_skips_git(
        self, tmp_path: Path, monkeypatch, mock_fabric_api
    ):
        """When branch_override is set, git must not be consulted at all."""
        _seed_workspace(tmp_path)
        _write_fabdep_yml(tmp_path, 'workspace_base_name: "ws"\n')

        # config.py resolves the branch via git.current_branch — if it's
        # invoked despite the override, this raises and the test fails.
        def _boom(repo_dir):
            raise AssertionError(
                "current_branch should not be called when branch_override is set"
            )

        monkeypatch.setattr("fabric_deploy.config.current_branch", _boom)

        d = WorkspaceDeployer(tmp_path, branch_override="staging")

        assert d.workspace_name == "ws [STAGING]"

    def test_new_workspace_creates_via_capacity(
        self, tmp_path: Path, mock_fabric_api
    ):
        _seed_workspace(tmp_path)
        _write_fabdep_yml(
            tmp_path,
            'workspace_base_name: "newws"\n'
            f'capacity_id: "{mock_fabric_api.capacity_id}"\n',
        )

        d = WorkspaceDeployer(tmp_path, branch_override="dev", new_workspace=True)

        assert d.workspace_name == "newws [DEV]"
        # Workspace ID still resolved via the (mocked) lookup after creation.
        assert d.workspace_id == mock_fabric_api.workspace_id
