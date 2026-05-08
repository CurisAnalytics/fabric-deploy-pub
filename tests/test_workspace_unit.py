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


class TestParameterScopeFilter:
    """Pin the scope-filter logic in _write_parameter_file."""

    def test_workspace_id_detected_only_in_notebook_is_dropped_when_notebook_excluded(
        self, deployer, tmp_path
    ):
        """Regression: Workspace IDs detected only in a Notebook used to be kept
        unconditionally, producing fabric-cicd warnings about an unmatched
        item_type filter when the Notebook was out of scope (e.g., when the
        notebook had been synced back from Fabric and contained the *target*
        workspace ID, which doesn't need substitution at all)."""
        from fabric_deploy.detect import DetectedParameter

        nb_only_wsid = "11111111-2222-3333-4444-555555555555"  # target wsid, NB-only
        sm_lakehouse = "22222222-3333-4444-5555-666666666666"  # source lh, SM
        deployer._detector.parameters = {
            nb_only_wsid: DetectedParameter(
                find_value=nb_only_wsid,
                description="Workspace ID",
                item_types={"Notebook"},
                dynamic_variable="$workspace.$id",
            ),
            sm_lakehouse: DetectedParameter(
                find_value=sm_lakehouse,
                description="Lakehouse ID",
                item_types={"SemanticModel"},
                dynamic_variable="$items.Lakehouse.lh.$id",
            ),
        }
        # Avoid touching the real repo.
        deployer._repo_dir_resolved = tmp_path

        deployer._write_parameter_file(item_types=["SemanticModel", "Report"])

        survivors = set(deployer._detector.parameters)
        assert nb_only_wsid not in survivors, (
            "Notebook-only Workspace ID should be dropped when Notebook is out of scope"
        )
        assert sm_lakehouse in survivors
