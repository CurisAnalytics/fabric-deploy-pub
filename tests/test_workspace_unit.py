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
        # The resolved workspace id / repo dir are passed straight through.
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


class TestFailOnStale:
    """Pin the --fail-on-stale contract: deploy raises before publish_all_items
    is called when the detector has recorded stale lakehouse attachments."""

    def test_raises_before_publishing_when_stale_attachments_present(
        self, deployer
    ):
        deployer._detector.stale_attachments = [
            ("foo.Notebook/notebook-content.py", ["99999999-9999-9999-9999-999999999999"])
        ]

        with (
            patch("fabric_deploy.workspace.FabricWorkspace") as fake_fw,
            patch("fabric_deploy.workspace.publish_all_items") as fake_publish,
            patch("fabric_deploy.workspace.unpublish_all_orphan_items"),
            patch("fabric_deploy.workspace.patch_fabric_cicd_logger"),
        ):
            with pytest.raises(RuntimeError, match="fail-on-stale"):
                deployer.deploy(item_types=["Notebook"], fail_on_stale=True)

        assert not fake_fw.called, "FabricWorkspace should not be constructed"
        assert not fake_publish.called, "publish_all_items should not be called"

    def test_deploy_proceeds_when_no_stale_attachments(self, deployer):
        deployer._detector.stale_attachments = []

        with (
            patch("fabric_deploy.workspace.FabricWorkspace"),
            patch("fabric_deploy.workspace.publish_all_items") as fake_publish,
            patch("fabric_deploy.workspace.unpublish_all_orphan_items"),
            patch("fabric_deploy.workspace.patch_fabric_cicd_logger"),
        ):
            deployer.deploy(item_types=["Notebook"], fail_on_stale=True)

        assert fake_publish.called

    def test_warning_only_by_default(self, deployer):
        """Without the flag, stale attachments don't block deploy."""
        deployer._detector.stale_attachments = [
            ("foo.Notebook/notebook-content.py", ["99999999-9999-9999-9999-999999999999"])
        ]

        with (
            patch("fabric_deploy.workspace.FabricWorkspace"),
            patch("fabric_deploy.workspace.publish_all_items") as fake_publish,
            patch("fabric_deploy.workspace.unpublish_all_orphan_items"),
            patch("fabric_deploy.workspace.patch_fabric_cicd_logger"),
        ):
            deployer.deploy(item_types=["Notebook"])  # fail_on_stale defaults to False

        assert fake_publish.called


class TestIncludeItemsValidation:
    """Pin the --include-items guard: a name that matches nothing must raise
    instead of silently deploying zero items (fabric-cicd's old behavior)."""

    def test_repo_items_keys_on_display_name(self, deployer):
        index = deployer._repo_items()
        # Keyed '<displayName>.<ItemType>' — note the space-containing names.
        assert "Fill Lakehouse.Notebook" in index
        assert "SQL DB.SemanticModel" in index

    def test_validate_returns_matched_subset(self, deployer):
        matched = deployer._validate_include_items(["Fill Lakehouse.Notebook"])
        assert matched == ["Fill Lakehouse.Notebook"]

    def test_validate_drops_unmatched_but_keeps_matched(self, deployer):
        matched = deployer._validate_include_items(
            ["Fill Lakehouse.Notebook", "Ghost.Notebook"]
        )
        assert matched == ["Fill Lakehouse.Notebook"]

    def test_validate_raises_when_nothing_matches(self, deployer):
        with pytest.raises(RuntimeError, match="matched no items"):
            deployer._validate_include_items(["Ghost.Notebook"])

    def test_deploy_raises_before_publish_on_total_mismatch(self, deployer):
        with (
            patch("fabric_deploy.workspace.FabricWorkspace") as fake_fw,
            patch("fabric_deploy.workspace.publish_all_items") as fake_publish,
            patch("fabric_deploy.workspace.unpublish_all_orphan_items"),
            patch("fabric_deploy.workspace.patch_fabric_cicd_logger"),
        ):
            with pytest.raises(RuntimeError, match="matched no items"):
                deployer.deploy(include_items=["Ghost.Notebook"])

        assert not fake_fw.called
        assert not fake_publish.called

    def test_deploy_passes_only_matched_items_to_publish(self, deployer):
        with (
            patch("fabric_deploy.workspace.FabricWorkspace"),
            patch("fabric_deploy.workspace.publish_all_items") as fake_publish,
            patch("fabric_deploy.workspace.unpublish_all_orphan_items"),
            patch("fabric_deploy.workspace.patch_fabric_cicd_logger"),
        ):
            deployer.deploy(
                include_items=["Fill Lakehouse.Notebook", "Ghost.Notebook"]
            )

        assert fake_publish.called
        assert fake_publish.call_args.kwargs["items_to_include"] == [
            "Fill Lakehouse.Notebook"
        ]


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
