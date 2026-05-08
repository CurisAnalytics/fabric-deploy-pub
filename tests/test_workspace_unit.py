"""Unit tests for WorkspaceDeployer that mock fabric-cicd internals.

These tests exercise the deployment call site without hitting the live
Fabric API, so they run anywhere `az login` is set up (or even with no
account at all, since the `az` calls are mocked too).

The key contract pinned here is the fabric-cicd 1.0 API: `FabricWorkspace`
must be constructed with a keyword-only `token_credential` argument.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from azure.core.credentials import TokenCredential

from fabric_deploy.workspace import WorkspaceDeployer


TEST_WS_DIR = Path(__file__).parent / "test-wkspc"
FAKE_WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def deployer():
    """Build a WorkspaceDeployer pointed at the test-wkspc fixture
    without making any network calls (all I/O is patched)."""
    with (
        patch(
            "fabric_deploy.workspace.get_workspace_id_by_name",
            return_value=FAKE_WORKSPACE_ID,
        ),
        # Suppress fabric-cicd module-level logging side effects
        patch("fabric_deploy.workspace.patch_fabric_cicd_logger"),
    ):
        d = WorkspaceDeployer(TEST_WS_DIR, branch_override="MAIN")
        return d


class TestFabricWorkspaceConstruction:
    """Pin the fabric-cicd 1.0 call contract."""

    def test_deploy_passes_token_credential_to_fabric_workspace(self, deployer):
        """FabricWorkspace must be constructed with a keyword `token_credential`."""
        with (
            patch("fabric_deploy.workspace.FabricWorkspace") as fake_fw,
            patch("fabric_deploy.workspace.publish_all_items"),
            patch("fabric_deploy.workspace.unpublish_all_orphan_items"),
            patch("fabric_deploy.workspace.patch_fabric_cicd_logger"),
        ):
            deployer.deploy(item_types=["Notebook"])

        assert fake_fw.called, "FabricWorkspace was never constructed"
        # All args must be keyword (1.0 signature is keyword-only)
        positional, kwargs = fake_fw.call_args
        assert positional == (), (
            f"FabricWorkspace was called with positional args {positional}; "
            "fabric-cicd 1.0 requires keyword-only arguments."
        )
        assert "token_credential" in kwargs, (
            "FabricWorkspace must be called with a `token_credential` keyword "
            "argument under fabric-cicd 1.0. Got: " + ", ".join(kwargs.keys())
        )
        cred = kwargs["token_credential"]
        assert isinstance(cred, TokenCredential), (
            f"`token_credential` must be a TokenCredential instance, got "
            f"{type(cred).__name__}"
        )

    def test_deploy_uses_resolved_workspace_id(self, deployer):
        """Sanity: the workspace_id resolved from the API is what we pass through."""
        with (
            patch("fabric_deploy.workspace.FabricWorkspace") as fake_fw,
            patch("fabric_deploy.workspace.publish_all_items"),
            patch("fabric_deploy.workspace.unpublish_all_orphan_items"),
            patch("fabric_deploy.workspace.patch_fabric_cicd_logger"),
        ):
            deployer.deploy(item_types=["Notebook"])

        kwargs = fake_fw.call_args.kwargs
        assert kwargs.get("workspace_id") == FAKE_WORKSPACE_ID
        assert kwargs.get("repository_directory") == str(TEST_WS_DIR.resolve())

    def test_publish_called_with_returned_workspace(self, deployer):
        """publish_all_items must receive the FabricWorkspace instance, not a kwarg."""
        with (
            patch("fabric_deploy.workspace.FabricWorkspace") as fake_fw,
            patch("fabric_deploy.workspace.publish_all_items") as fake_publish,
            patch("fabric_deploy.workspace.unpublish_all_orphan_items"),
            patch("fabric_deploy.workspace.patch_fabric_cicd_logger"),
        ):
            instance = MagicMock(name="FabricWorkspaceInstance")
            fake_fw.return_value = instance
            deployer.deploy(item_types=["Notebook"])

        assert fake_publish.called
        assert fake_publish.call_args.args[0] is instance


class TestParameterFileWriter:
    """Pin _write_parameter_file behavior under the no-auto-scoping model:
    every detected parameter flows through; only manual_parameters carry
    item_type filters into the emitted yaml."""

    def test_all_detected_params_flow_through_regardless_of_scope(
        self, deployer, tmp_path
    ):
        from fabric_deploy.detect import DetectedParameter

        wsid = "11111111-1111-1111-1111-111111111111"
        lhid = "22222222-2222-2222-2222-222222222222"
        deployer._detector.parameters = {
            wsid: DetectedParameter(
                find_value=wsid,
                description="Workspace ID",
                dynamic_variable="$workspace.$id",
            ),
            lhid: DetectedParameter(
                find_value=lhid,
                description="Lakehouse ID",
                dynamic_variable="$items.Lakehouse.lh.$id",
            ),
        }
        deployer._repo_dir_resolved = tmp_path

        deployer._write_parameter_file()

        # Both detected entries survive — scope no longer filters auto-detected.
        assert set(deployer._detector.parameters) == {wsid, lhid}

    def test_emitted_yaml_omits_item_type_for_unscoped_entries(
        self, deployer, tmp_path
    ):
        """Auto-detected entries have empty item_types and must NOT emit an
        `item_type:` line — fabric-cicd then applies them to all in-scope files."""
        import yaml
        from fabric_deploy.detect import DetectedParameter

        deployer._detector.parameters = {
            "wsid": DetectedParameter(
                find_value="wsid",
                description="Workspace ID",
                dynamic_variable="$workspace.$id",
            ),
        }
        deployer._repo_dir_resolved = tmp_path

        deployer._write_parameter_file()

        param_file = tmp_path / "parameter.yml"
        assert param_file.exists()
        data = yaml.safe_load(param_file.read_text())
        assert "item_type" not in data["find_replace"][0]

    def test_emitted_yaml_includes_item_type_for_manually_scoped_entries(
        self, deployer, tmp_path
    ):
        """Manual overrides set item_types — those must round-trip into yaml."""
        import yaml
        from fabric_deploy.detect import DetectedParameter

        deployer._detector.parameters = {
            "wsid": DetectedParameter(
                find_value="wsid",
                description="Workspace ID",
                item_types={"SemanticModel"},  # set by manual override
                dynamic_variable="$workspace.$id",
            ),
        }
        deployer._repo_dir_resolved = tmp_path

        deployer._write_parameter_file()

        param_file = tmp_path / "parameter.yml"
        data = yaml.safe_load(param_file.read_text())
        assert data["find_replace"][0]["item_type"] == ["SemanticModel"]
