"""Unit tests for WorkspaceDeployer that mock fabric-cicd internals.

These tests exercise the deployment call site with the Fabric API and
fabric-cicd mocked, so they need no sign-in.

The key contract pinned here is the fabric-cicd 1.0 API: `FabricWorkspace`
must be constructed with a keyword-only `token_credential` argument.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from azure.core.credentials import TokenCredential

from fabric_deploy.params import ParameterFile
from fabric_deploy.scope import validate_include_items
from fabric_deploy.workspace import WorkspaceDeployer

from .conftest import SAMPLE_DEV_WORKSPACE_ID, SAMPLE_LAKEHOUSE_ID

FAKE_WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"


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
        # The target branch is the fabric-cicd environment (Variable Library
        # value sets, environment-keyed parameter.yml entries).
        assert kwargs.get("environment") == "MAIN"

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


class TestOrphanCleanup:
    def _deploy(self, deployer, **kwargs) -> MagicMock:
        with (
            patch("fabric_deploy.workspace.FabricWorkspace") as fake_fw,
            patch("fabric_deploy.workspace.publish_all_items"),
            patch("fabric_deploy.workspace.unpublish_all_orphan_items") as unpublish,
            patch("fabric_deploy.workspace.patch_fabric_cicd_logger"),
        ):
            deployer.deploy(**kwargs)
        assert not unpublish.called or unpublish.call_args.args == (fake_fw.return_value,)
        return unpublish

    def test_repo_without_a_lakehouse_deploys(self, tmp_path: Path, mock_fabric_api):
        (tmp_path / "fabdep.yml").write_text('workspace_base_name: "Base"\n')
        nb = tmp_path / "nb.Notebook"
        nb.mkdir()
        (nb / ".platform").write_text('{"metadata": {"displayName": "nb", "type": "Notebook"}}')

        unpublish = self._deploy(WorkspaceDeployer(tmp_path, branch_override="dev"))

        assert unpublish.called

    def test_no_item_is_kept_by_name(self, deployer):
        """Every item missing from the repo is an orphan, whatever its name."""
        unpublish = self._deploy(deployer, item_types=["Notebook"])
        assert unpublish.call_args.kwargs == {}

    def test_skipped_for_include_items(self, deployer):
        unpublish = self._deploy(deployer, include_items=["Fill Lakehouse.Notebook"])
        assert not unpublish.called


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
            "export_summary",
            "33333333-4444-5555-6666-777777777777",
            "no .UserDataFunction item in the repo declares 'export_summary'",
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
    instead of silently deploying zero items."""

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


class TestParameterFile:
    """The parameter file fabric-cicd gets: the repo's parameter.yml plus the
    detected rules, written outside the repo. The sample repo's notebook
    yields two detected rules: its DEV workspace id and its lakehouse id."""

    USER_FILE = (
        "find_replace:\n"
        f'  - find_value: "{SAMPLE_LAKEHOUSE_ID}"\n'
        "    replace_value:\n"
        '      MAIN: "user-lakehouse"\n'
        '  - find_value: "user-only"\n'
        "    replace_value:\n"
        '      _ALL_: "x"\n'
        "key_value_replace:\n"
        '  - find_key: "$.properties.x"\n'
        "    replace_value:\n"
        '      MAIN: "y"\n'
    )

    def _deploy(self, deployer) -> dict:
        """Deploy with fabric-cicd mocked; record what FabricWorkspace got and
        what the repo held at that moment."""
        seen: dict = {}
        repo = deployer._repo_dir_resolved

        def fake_fabric_workspace(**kwargs):
            path = kwargs.get("parameter_file_path")
            seen["path"] = path
            seen["environment"] = kwargs.get("environment")
            seen["document"] = yaml.safe_load(Path(path).read_text()) if path else None
            seen["repo_files"] = sorted(f.name for f in repo.iterdir())
            return MagicMock()

        with (
            patch("fabric_deploy.workspace.FabricWorkspace", side_effect=fake_fabric_workspace),
            patch("fabric_deploy.workspace.publish_all_items"),
            patch("fabric_deploy.workspace.unpublish_all_orphan_items"),
            patch("fabric_deploy.workspace.patch_fabric_cicd_logger"),
        ):
            deployer.deploy(item_types=["Notebook"])
        return seen

    def test_detected_rules_go_to_a_temporary_file_outside_the_repo(self, deployer):
        repo_files = sorted(f.name for f in deployer._repo_dir_resolved.iterdir())
        seen = self._deploy(deployer)

        assert isinstance(seen["path"], str)
        assert not Path(seen["path"]).is_relative_to(deployer._repo_dir_resolved)
        assert not Path(seen["path"]).exists(), "removed after the deploy"
        assert seen["repo_files"] == repo_files
        assert seen["document"] == {
            "find_replace": [
                {
                    "find_value": SAMPLE_LAKEHOUSE_ID,
                    "replace_value": {"MAIN": "$items.Lakehouse.fabdep_lakehouse.$id"},
                },
                {
                    "find_value": SAMPLE_DEV_WORKSPACE_ID,
                    "replace_value": {"MAIN": "$workspace.$id"},
                },
            ]
        }

    def test_user_file_is_merged_and_wins(self, deployer):
        user_file = deployer._repo_dir_resolved / "parameter.yml"
        user_file.write_text(self.USER_FILE)
        repo_files = sorted(f.name for f in deployer._repo_dir_resolved.iterdir())

        seen = self._deploy(deployer)

        user = yaml.safe_load(self.USER_FILE)
        assert seen["document"] == {
            # The user's entries first; the detected lakehouse rule they cover dropped.
            "find_replace": [
                *user["find_replace"],
                {
                    "find_value": SAMPLE_DEV_WORKSPACE_ID,
                    "replace_value": {"MAIN": "$workspace.$id"},
                },
            ],
            "key_value_replace": user["key_value_replace"],
        }
        assert seen["repo_files"] == repo_files
        assert user_file.read_text() == self.USER_FILE

    def test_template_paths_resolve_from_the_repo(self, deployer):
        repo = deployer._repo_dir_resolved
        (repo / "params").mkdir()
        (repo / "params" / "ws.yml").write_text(
            f'find_replace:\n  - find_value: "{SAMPLE_DEV_WORKSPACE_ID}"\n'
            '    replace_value:\n      _ALL_: "from-template"\n'
        )
        (repo / "parameter.yml").write_text('extend:\n  - "params/ws.yml"\n')

        seen = self._deploy(deployer)

        assert seen["document"]["extend"] == [str(repo / "params" / "ws.yml")]
        assert [e["find_value"] for e in seen["document"]["find_replace"]] == [SAMPLE_LAKEHOUSE_ID]

    def test_malformed_user_file_fails_the_deploy(self, deployer):
        (deployer._repo_dir_resolved / "parameter.yml").write_text('extend: "one.yml"\n')
        with pytest.raises(ValueError, match="parameter.yml"):
            self._deploy(deployer)

    def test_nothing_to_add_leaves_fabric_cicd_on_the_repo_file(self, deployer):
        deployer._detector.parameters = {}
        seen = self._deploy(deployer)
        assert seen["path"] is None
        assert seen["environment"] == "MAIN"

    @pytest.mark.parametrize(
        ("item_types", "expected"), [(set(), None), ({"SemanticModel"}, ["SemanticModel"])]
    )
    def test_item_type_only_for_scoped_entries(self, tmp_path, item_types, expected):
        """Detected entries carry no item_type, so fabric-cicd applies them to
        every in-scope file; a manual_parameters scope round-trips."""
        from fabric_deploy.detect import DetectedParameter, ParameterDetector

        detector = ParameterDetector(tmp_path)
        detector.detect_all()
        detector.parameters = {
            "v": DetectedParameter("v", item_types=item_types, dynamic_variable="$workspace.$id")
        }
        param_file = ParameterFile(tmp_path)
        param_file.write(detector, FAKE_WORKSPACE_ID, "MAIN", 0, 0)
        try:
            entry = yaml.safe_load(Path(param_file.path).read_text())["find_replace"][0]
        finally:
            param_file.cleanup()

        assert entry.get("item_type") == expected


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

    def test_one_bracketed_suffix_marks_a_sibling(self, monkeypatch, sample_ws):
        d, _, _ = _anchored_deployer(
            monkeypatch,
            sample_ws,
            workspaces=[
                {"id": QA_WORKSPACE_ID, "displayName": "Fabdep Test [ARCHIVE] Copy [DEV]"},
                {"id": DEV_WORKSPACE_ID, "displayName": "Fabdep Test [DEV] [OLD]"},
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

    def test_environments_map_names_the_siblings(self, monkeypatch, sample_ws):
        (sample_ws / "fabdep.yml").write_text(
            'workspace_base_name: "Fabdep Test"\n'
            "environments:\n  MAIN:\n  dev:\n    workspace: Fabdep Dev\n"
        )
        d, _, _ = _anchored_deployer(
            monkeypatch,
            sample_ws,
            workspaces=[
                {"id": DEV_WORKSPACE_ID, "displayName": "Fabdep Dev"},
                {"id": QA_WORKSPACE_ID, "displayName": "Fabdep Test [QA]"},
            ],
        )

        assert d.source_workspace_ids == {FAKE_WORKSPACE_ID, DEV_WORKSPACE_ID}

    def test_listing_failure_warns_and_keeps_target_anchor(self, monkeypatch, sample_ws, caplog):
        d, _, _ = _anchored_deployer(monkeypatch, sample_ws, workspaces=[], list_error="403")

        assert d.source_workspace_ids == {FAKE_WORKSPACE_ID}
        assert "Could not list workspaces (403)" in caplog.text

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
    def test_deploy_names_source_and_target_without_a_banner(self, deployer, caplog):
        deployer.source_workspaces = {"00000000-0000-0000-0000-00000000000d": "Fabdep Test [DEV]"}
        with (
            patch("fabric_deploy.workspace.FabricWorkspace"),
            patch("fabric_deploy.workspace.publish_all_items"),
            patch("fabric_deploy.workspace.unpublish_all_orphan_items"),
            patch("fabric_deploy.workspace.patch_fabric_cicd_logger"),
            caplog.at_level("INFO"),
        ):
            deployer.deploy(item_types=["Notebook"])

        messages = [r.message for r in caplog.records]
        assert not any("fabdep v" in m for m in messages)
        assert (
            "Source Workspace: Fabdep Test [DEV] (00000000-0000-0000-0000-00000000000d)" in messages
        )
        assert f"Target Workspace: 'Fabdep Test [MAIN]' ({FAKE_WORKSPACE_ID})" in messages
        assert not any(
            m.startswith("Workspace Name") or m.startswith("Parameter file") for m in messages
        )
