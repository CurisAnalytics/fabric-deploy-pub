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

from fabric_deploy.params import ParameterFile
from fabric_deploy.scope import _repo_items, validate_include_items
from fabric_deploy.workspace import WorkspaceDeployer, format_deploy_options

from .conftest import SAMPLE_DEV_WORKSPACE_ID

FAKE_WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"


class TestFormatDeployOptions:
    def test_defaults(self):
        assert format_deploy_options() == "Options: (defaults)"

    def test_scoped_cross_branch_deploy(self):
        """The Woodgrove-style invocation: fabdep -t SemanticModel -b prod."""
        line = format_deploy_options(branch_override="prod", item_types=["SemanticModel"])
        assert line == "Options: --branch prod --item-types SemanticModel"

    def test_all_flags(self):
        line = format_deploy_options(
            branch_override="prod",
            new_workspace=True,
            skip_lakehouse=True,
            max_retries=3,
            no_cleanup=True,
            item_types=["Notebook", "DataPipeline"],
            exclude_types=["Report"],
            include_items=["My Model.SemanticModel"],
            fail_on_stale=True,
            allow_rls_change=True,
            allow_unbound_udf=True,
        )
        for needle in (
            "--branch prod",
            "--new-workspace",
            "--skip-lakehouse",
            "--max-retries 3",
            "--no-cleanup",
            "--item-types Notebook DataPipeline",
            "--exclude-types Report",
            "--include-items 'My Model.SemanticModel'",
            "--fail-on-stale",
            "--allow-rls-change",
            "--allow-unbound-udf",
        ):
            assert needle in line


@pytest.fixture
def deployer(sample_ws):
    """Build a WorkspaceDeployer pointed at the sample workspace fixture
    without making any network calls (all I/O is patched)."""
    with (
        patch(
            "fabric_deploy.workspace.get_workspace_id_by_name",
            return_value=FAKE_WORKSPACE_ID,
        ),
        patch("fabric_deploy.workspace.list_workspaces", return_value=[]),
        # Suppress fabric-cicd module-level logging side effects
        patch("fabric_deploy.workspace.patch_fabric_cicd_logger"),
    ):
        d = WorkspaceDeployer(sample_ws, branch_override="MAIN")
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
            f"`token_credential` must be a TokenCredential instance, got {type(cred).__name__}"
        )
        # The resolved workspace id / repo dir are passed straight through.
        assert kwargs.get("workspace_id") == FAKE_WORKSPACE_ID
        assert kwargs.get("repository_directory") == str(deployer._repo_dir_resolved)

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


class TestFeatureFlags:
    def test_appended_flags_are_still_supported_upstream(self):
        """fabdep appends these flags for --include-items deploys. fabric-cicd
        1.3 exposes the supported set — catch an upstream rename at test time
        instead of at deploy time."""
        from fabric_cicd import get_supported_feature_flags

        supported = set(get_supported_feature_flags())
        assert "enable_experimental_features" in supported
        assert "enable_items_to_include" in supported


class TestFailOnStale:
    """Pin the --fail-on-stale contract: deploy raises before publish_all_items
    is called when a notebook warehouse attachment can't be remapped."""

    UNRESOLVED = [
        ("foo.Notebook/notebook-content.py", "99999999-9999-9999-9999-999999999999", "Unknown")
    ]

    @pytest.mark.parametrize(
        ("unresolved", "fail_on_stale", "blocks"),
        [(UNRESOLVED, True, True), ([], True, False), (UNRESOLVED, False, False)],
    )
    def test_blocks_only_unresolved_bindings_with_the_flag(
        self, deployer, unresolved, fail_on_stale, blocks
    ):
        deployer._detector.unresolved_warehouse_bindings = list(unresolved)
        with (
            patch("fabric_deploy.workspace.FabricWorkspace") as fake_fw,
            patch("fabric_deploy.workspace.publish_all_items") as fake_publish,
            patch("fabric_deploy.workspace.unpublish_all_orphan_items"),
            patch("fabric_deploy.workspace.patch_fabric_cicd_logger"),
        ):
            if blocks:
                with pytest.raises(RuntimeError, match="fail-on-stale"):
                    deployer.deploy(item_types=["Notebook"], fail_on_stale=fail_on_stale)
            else:
                deployer.deploy(item_types=["Notebook"], fail_on_stale=fail_on_stale)

        assert fake_fw.called is not blocks
        assert fake_publish.called is not blocks


class TestUdfBindingGuard:
    """Pin the unbound-UDF guard: a bound button whose function resolves to no
    repo .UserDataFunction blocks the deploy when its report is in scope,
    unless --allow-unbound-udf."""

    SKIPPED = [
        (
            "Finance Report",
            "write_pl_statement",
            "33333333-4444-5555-6666-777777777777",
            "no .UserDataFunction item in the repo declares 'write_pl_statement'",
        )
    ]

    def _deploy(self, deployer, **kwargs):
        with (
            patch("fabric_deploy.workspace.FabricWorkspace"),
            patch("fabric_deploy.workspace.publish_all_items") as fake_publish,
            patch("fabric_deploy.workspace.unpublish_all_orphan_items"),
            patch("fabric_deploy.workspace.patch_fabric_cicd_logger"),
        ):
            deployer.deploy(**kwargs)
        return fake_publish

    def test_blocks_by_default_when_report_in_scope(self, deployer):
        deployer._detector.udf_skipped = list(self.SKIPPED)

        with (
            patch("fabric_deploy.workspace.FabricWorkspace") as fake_fw,
            patch("fabric_deploy.workspace.publish_all_items") as fake_publish,
            patch("fabric_deploy.workspace.unpublish_all_orphan_items"),
            patch("fabric_deploy.workspace.patch_fabric_cicd_logger"),
        ):
            with pytest.raises(RuntimeError, match="allow-unbound-udf"):
                deployer.deploy(item_types=["Report"])

        assert not fake_fw.called
        assert not fake_publish.called

    def test_allow_flag_deploys_anyway(self, deployer):
        deployer._detector.udf_skipped = list(self.SKIPPED)
        fake_publish = self._deploy(deployer, item_types=["Report"], allow_unbound_udf=True)
        assert fake_publish.called

    def test_report_out_of_type_scope_does_not_block(self, deployer):
        deployer._detector.udf_skipped = list(self.SKIPPED)
        fake_publish = self._deploy(deployer, item_types=["Notebook"])
        assert fake_publish.called

    def test_report_not_in_include_items_does_not_block(self, deployer):
        deployer._detector.udf_skipped = list(self.SKIPPED)
        fake_publish = self._deploy(deployer, include_items=["Fill Lakehouse.Notebook"])
        assert fake_publish.called

    def test_no_skipped_buttons_does_not_block(self, deployer):
        deployer._detector.udf_skipped = []
        fake_publish = self._deploy(deployer, item_types=["Report"])
        assert fake_publish.called


class TestDeployedSummaryLine:
    """The 'Deployed N item(s)' line must reflect the type scope: an item
    excluded via -x stays in include_items but was never published."""

    def test_excluded_type_dropped_from_summary(self, deployer, caplog):
        with (
            patch("fabric_deploy.workspace.FabricWorkspace"),
            patch("fabric_deploy.workspace.publish_all_items"),
            patch("fabric_deploy.workspace.unpublish_all_orphan_items"),
            patch("fabric_deploy.workspace.patch_fabric_cicd_logger"),
            caplog.at_level("INFO"),
        ):
            deployer.deploy(
                include_items=["Fill Lakehouse.Notebook", "SQL DB.SemanticModel"],
                exclude_types=["SemanticModel"],
            )

        deployed_lines = [r.message for r in caplog.records if r.message.startswith("Deployed ")]
        assert deployed_lines == ["Deployed 1 item(s): Fill Lakehouse.Notebook"]


class TestIncludeItemsValidation:
    """Pin the --include-items guard: a name that matches nothing must raise
    instead of silently deploying zero items (fabric-cicd's old behavior)."""

    def test_repo_items_keys_on_display_name(self, deployer):
        index = _repo_items(deployer._repo_dir_resolved)
        # Keyed '<displayName>.<ItemType>' — note the space-containing names.
        assert "Fill Lakehouse.Notebook" in index
        assert "SQL DB.SemanticModel" in index

    def _validate(self, deployer, items):
        return validate_include_items(items, deployer._repo_dir_resolved, deployer.logger)

    def test_validate_returns_matched_subset(self, deployer):
        assert self._validate(deployer, ["Fill Lakehouse.Notebook"]) == ["Fill Lakehouse.Notebook"]

    def test_validate_drops_unmatched_but_keeps_matched(self, deployer):
        assert self._validate(deployer, ["Fill Lakehouse.Notebook", "Ghost.Notebook"]) == [
            "Fill Lakehouse.Notebook"
        ]

    def test_validate_raises_when_nothing_matches(self, deployer):
        with pytest.raises(RuntimeError, match="matched no items"):
            self._validate(deployer, ["Ghost.Notebook"])

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
            deployer.deploy(include_items=["Fill Lakehouse.Notebook", "Ghost.Notebook"])

        assert fake_publish.called
        assert fake_publish.call_args.kwargs["items_to_include"] == ["Fill Lakehouse.Notebook"]


class TestParameterFileWriter:
    """Pin _write_parameter_file behavior under the no-auto-scoping model:
    every detected parameter flows through; only manual_parameters carry
    item_type filters into the emitted yaml."""

    def test_all_detected_params_flow_through_regardless_of_scope(self, deployer, tmp_path):
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
        ParameterFile(tmp_path, deployer.logger).write(deployer._detector, FAKE_WORKSPACE_ID, 0, 0)

        # Both detected entries survive — scope no longer filters auto-detected.
        assert set(deployer._detector.parameters) == {wsid, lhid}

    def test_emitted_yaml_omits_item_type_for_unscoped_entries(self, deployer, tmp_path):
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
        ParameterFile(tmp_path, deployer.logger).write(deployer._detector, FAKE_WORKSPACE_ID, 0, 0)

        param_file = tmp_path / "parameter.yml"
        assert param_file.exists()
        data = yaml.safe_load(param_file.read_text())
        assert "item_type" not in data["find_replace"][0]

    def test_emitted_yaml_includes_item_type_for_manually_scoped_entries(self, deployer, tmp_path):
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
        ParameterFile(tmp_path, deployer.logger).write(deployer._detector, FAKE_WORKSPACE_ID, 0, 0)

        param_file = tmp_path / "parameter.yml"
        data = yaml.safe_load(param_file.read_text())
        assert data["find_replace"][0]["item_type"] == ["SemanticModel"]


# --- sibling-workspace anchor resolution ------------------------------------

# The sample notebook's legacy metadata names this workspace.
DEV_WORKSPACE_ID = SAMPLE_DEV_WORKSPACE_ID
QA_WORKSPACE_ID = "00000000-0000-0000-0000-00000000000d"
SRC_LAKEHOUSE_ID = "00000000-0000-0000-0000-00000000001a"


def _anchored_deployer(
    monkeypatch, repo, *, workspaces, lakehouses=None, list_error=None, **kwargs
) -> tuple[WorkspaceDeployer, MagicMock, MagicMock]:
    """Deployer on the sample workspace with the workspace listing under control."""
    list_ws = MagicMock(return_value=workspaces)
    if list_error:
        list_ws.side_effect = RuntimeError(list_error)
    list_items = MagicMock(return_value=lakehouses or [])
    monkeypatch.setattr("fabric_deploy.workspace.list_workspaces", list_ws)
    monkeypatch.setattr("fabric_deploy.workspace.list_workspace_items", list_items)
    with (
        patch(
            "fabric_deploy.workspace.get_workspace_id_by_name",
            return_value=FAKE_WORKSPACE_ID,
        ),
        patch("fabric_deploy.workspace.patch_fabric_cicd_logger"),
    ):
        d = WorkspaceDeployer(repo, branch_override="MAIN", **kwargs)
    return d, list_ws, list_items


class TestSiblingWorkspaceAnchors:
    def test_target_is_anchored_even_without_siblings(self, monkeypatch, sample_ws):
        d, _, list_items = _anchored_deployer(monkeypatch, sample_ws, workspaces=[])

        assert d.source_workspace_ids == {FAKE_WORKSPACE_ID}
        assert d.source_lakehouse_ids == {}
        assert not list_items.called
        assert FAKE_WORKSPACE_ID in d._detector._source_workspace_ids

    def test_referenced_sibling_anchors_and_lists_its_lakehouses(self, monkeypatch, sample_ws):
        """The content names DEV (legacy metadata here; a pipeline workspaceId
        or an abfss literal in an auto-bound repo), so DEV's lakehouse ids
        are fetched to map alongside its workspace id."""
        d, _, list_items = _anchored_deployer(
            monkeypatch,
            sample_ws,
            workspaces=[
                {"id": FAKE_WORKSPACE_ID, "displayName": "Fabdep Test [MAIN]"},
                {"id": DEV_WORKSPACE_ID, "displayName": "Fabdep Test [DEV]"},
            ],
            lakehouses=[
                {"id": SRC_LAKEHOUSE_ID, "displayName": "fabdep_lakehouse", "type": "Lakehouse"}
            ],
        )

        list_items.assert_called_once_with(DEV_WORKSPACE_ID, "Lakehouse")
        assert d.source_workspace_ids == {FAKE_WORKSPACE_ID, DEV_WORKSPACE_ID}
        assert d.source_lakehouse_ids == {SRC_LAKEHOUSE_ID: "fabdep_lakehouse"}
        assert DEV_WORKSPACE_ID in d._detector._source_workspace_ids
        assert d._detector.parameters[DEV_WORKSPACE_ID].dynamic_variable == "$workspace.$id"

    def test_unreferenced_sibling_is_anchored_but_not_listed(self, monkeypatch, sample_ws):
        d, _, list_items = _anchored_deployer(
            monkeypatch,
            sample_ws,
            workspaces=[{"id": QA_WORKSPACE_ID, "displayName": "Fabdep Test [QA]"}],
        )

        assert QA_WORKSPACE_ID in d.source_workspace_ids
        assert not list_items.called

    def test_other_repos_workspaces_are_not_siblings(self, monkeypatch, sample_ws):
        d, _, _ = _anchored_deployer(
            monkeypatch,
            sample_ws,
            workspaces=[
                {"id": QA_WORKSPACE_ID, "displayName": "Other [DEV]"},
                {"id": DEV_WORKSPACE_ID, "displayName": "Fabdep Test Extra [DEV]"},
                {"id": "00000000-0000-0000-0000-00000000000e", "displayName": "Fabdep Test"},
            ],
        )

        assert d.source_workspace_ids == {FAKE_WORKSPACE_ID}

    def test_sibling_match_is_case_insensitive(self, monkeypatch, sample_ws):
        d, _, _ = _anchored_deployer(
            monkeypatch,
            sample_ws,
            workspaces=[{"id": QA_WORKSPACE_ID, "displayName": "fabdep test [qa]"}],
        )

        assert QA_WORKSPACE_ID in d.source_workspace_ids

    def test_listing_failure_warns_and_keeps_target_anchor(self, monkeypatch, sample_ws, caplog):
        d, _, _ = _anchored_deployer(monkeypatch, sample_ws, workspaces=[], list_error="403")

        assert d.source_workspace_ids == {FAKE_WORKSPACE_ID}
        assert "anchor" in caplog.text

    def test_lakehouse_listing_failure_warns_and_continues(self, monkeypatch, sample_ws, caplog):
        list_ws = MagicMock(
            return_value=[{"id": DEV_WORKSPACE_ID, "displayName": "Fabdep Test [DEV]"}]
        )
        monkeypatch.setattr("fabric_deploy.workspace.list_workspaces", list_ws)
        monkeypatch.setattr(
            "fabric_deploy.workspace.list_workspace_items",
            MagicMock(side_effect=RuntimeError("403")),
        )
        with (
            patch(
                "fabric_deploy.workspace.get_workspace_id_by_name",
                return_value=FAKE_WORKSPACE_ID,
            ),
            patch("fabric_deploy.workspace.patch_fabric_cicd_logger"),
        ):
            d = WorkspaceDeployer(sample_ws, branch_override="MAIN")

        assert DEV_WORKSPACE_ID in d.source_workspace_ids
        assert d.source_lakehouse_ids == {}
        assert "Fabdep Test [DEV]" in caplog.text


class TestSkipLakehouseWithAutoBinding:
    def test_warns_that_auto_bound_notebooks_rebind_anyway(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """fabric-cicd resolves a lakehouse logicalId to the target's lakehouse
        before parameters apply, so -L cannot keep an auto-bound notebook on
        the source lakehouse; say so instead of silently not doing it."""
        (tmp_path / "fabdep.yml").write_text('workspace_base_name: "Base"\n')
        lh = tmp_path / "lh.Lakehouse"
        lh.mkdir()
        (lh / ".platform").write_text(
            '{"metadata": {"displayName": "lh", "type": "Lakehouse"}, '
            '"config": {"logicalId": "66666666-6666-6666-6666-666666666666"}}'
        )
        nb = tmp_path / "nb.Notebook"
        nb.mkdir()
        (nb / ".platform").write_text('{"metadata": {"displayName": "nb", "type": "Notebook"}}')
        (nb / "notebook-content.py").write_text(
            '# META {\n# META   "dependencies": {"lakehouse": {\n'
            '# META     "default_lakehouse": "66666666-6666-6666-6666-666666666666",\n'
            '# META     "default_lakehouse_workspace_id": "00000000-0000-0000-0000-000000000000"\n'
            "# META   }}\n# META }\n"
        )
        (nb / "notebook-settings.json").write_text('{"auto-binding": {"lakehouse": "on"}}')
        monkeypatch.setattr("fabric_deploy.workspace.list_workspaces", lambda: [])
        with (
            patch(
                "fabric_deploy.workspace.get_workspace_id_by_name",
                return_value=FAKE_WORKSPACE_ID,
            ),
            patch("fabric_deploy.workspace.patch_fabric_cicd_logger"),
        ):
            WorkspaceDeployer(tmp_path, branch_override="MAIN", skip_lakehouse=True)

        assert "--skip-lakehouse" in caplog.text
        assert "nb.Notebook" in caplog.text


class TestRunSummaryLines:
    def test_deploy_without_banner_still_names_source_and_target(self, deployer, caplog):
        deployer.source_workspaces = {"00000000-0000-0000-0000-00000000000d": "Fabdep Test [DEV]"}
        with (
            patch("fabric_deploy.workspace.FabricWorkspace"),
            patch("fabric_deploy.workspace.publish_all_items"),
            patch("fabric_deploy.workspace.unpublish_all_orphan_items"),
            patch("fabric_deploy.workspace.patch_fabric_cicd_logger"),
            caplog.at_level("INFO"),
        ):
            deployer.deploy(item_types=["Notebook"], banner=False)

        messages = [r.message for r in caplog.records]
        assert not any("fabdep v" in m for m in messages)
        assert (
            "Source Workspace: Fabdep Test [DEV] (00000000-0000-0000-0000-00000000000d)" in messages
        )
        assert f"Target Workspace: 'Fabdep Test [MAIN]' ({FAKE_WORKSPACE_ID})" in messages
        assert not any(
            m.startswith("Workspace Name") or m.startswith("Parameter file") for m in messages
        )

    def test_banner_names_fabdep(self, caplog):
        import logging

        from fabric_deploy import __version__
        from fabric_deploy.workspace import log_banner

        with caplog.at_level("INFO"):
            log_banner(logging.getLogger("fabric_deploy.test"), "Options: (defaults)")
        assert any(f"fabdep v{__version__}" in r.message for r in caplog.records)
        assert caplog.records[-1].message == "Options: (defaults)"
