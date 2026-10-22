"""WorkspaceDeployer with the Fabric API and fabric-cicd mocked.

What a deploy hands fabric-cicd, when it cleans up orphans, the guards that
stop it before anything is published, the parameter file, and the sibling
workspaces it anchors.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from azure.core.credentials import TokenCredential

from fabric_deploy.workspace import WorkspaceDeployer

from .conftest import FAKE_WORKSPACE_ID, SAMPLE_DEV_WORKSPACE_ID, SAMPLE_LAKEHOUSE_ID, make_item

# The sample workspace's items.
NOTEBOOK = "Fill Lakehouse.Notebook"
MODEL = "SQL DB.SemanticModel"
LAKEHOUSE = "fabdep_lakehouse.Lakehouse"


@pytest.fixture
def deployer(sample_ws, mock_fabric_api, monkeypatch) -> WorkspaceDeployer:
    """A deployer for the sample workspace's MAIN branch.

    The sample's semantic model is not in the target yet, so the RLS guard passes.
    """
    monkeypatch.setattr("fabric_deploy.rls._remote_roles", lambda _ws, _name: None)
    return WorkspaceDeployer(sample_ws, branch_override="MAIN")


def test_deploy_hands_fabric_cicd_the_target_and_the_checked_credential(deployer, fabric_cicd):
    deployer.deploy(item_types=["Notebook"])

    assert fabric_cicd.workspace.call_args.args == ()  # its arguments are keyword-only
    kwargs = fabric_cicd.workspace.call_args.kwargs
    assert isinstance(kwargs["token_credential"], TokenCredential)
    assert kwargs["workspace_id"] == FAKE_WORKSPACE_ID
    assert kwargs["repository_directory"] == str(deployer._repo_dir_resolved)
    # The target branch is the fabric-cicd environment.
    assert kwargs["environment"] == "MAIN"
    assert kwargs["item_type_in_scope"] == ["Notebook"]
    fabric_cicd.publish.assert_called_once_with(
        fabric_cicd.workspace.return_value, items_to_include=None
    )


def test_appended_feature_flags_exist_upstream():
    """Catch an upstream rename of the flags --include-items needs at test time."""
    from fabric_cicd import get_supported_feature_flags

    supported = set(get_supported_feature_flags())
    assert {"enable_experimental_features", "enable_items_to_include"} <= supported


class TestOrphanCleanup:
    @pytest.mark.parametrize(
        ("options", "cleans"),
        [
            ({}, True),
            ({"item_types": ["Notebook"]}, True),
            ({"exclude_types": ["Report"]}, True),
            ({"no_cleanup": True}, False),
            ({"include_items": [NOTEBOOK]}, False),
            ({"pr": "abc1234"}, False),  # --pr deploys the items the PR touched
        ],
    )
    def test_runs_unless_disabled_or_items_are_named(
        self, deployer, fabric_cicd, monkeypatch, options, cleans
    ):
        monkeypatch.setattr(
            "fabric_deploy.workspace.changed_paths_in",
            lambda _repo, _commit: [f"{NOTEBOOK}/notebook-content.py"],
        )
        deployer.deploy(**options)

        if cleans:
            # Every item missing from the repo is an orphan, whatever its name.
            fabric_cicd.unpublish.assert_called_once_with(fabric_cicd.workspace.return_value)
        else:
            fabric_cicd.unpublish.assert_not_called()

    def test_repo_without_a_lakehouse_deploys(self, tmp_path: Path, mock_fabric_api, fabric_cicd):
        (tmp_path / "fabdep.yml").write_text('workspace_base_name: "Base"\n')
        make_item(tmp_path, "nb", "Notebook")

        WorkspaceDeployer(tmp_path, branch_override="dev").deploy()

        fabric_cicd.publish.assert_called_once()
        fabric_cicd.unpublish.assert_called_once()


class TestIncludeItems:
    def test_publishes_the_matched_items_with_the_feature_flags(self, deployer, fabric_cicd):
        deployer.deploy(include_items=[NOTEBOOK, "Ghost.Notebook"])

        assert fabric_cicd.publish.call_args.kwargs["items_to_include"] == [NOTEBOOK]
        assert fabric_cicd.flags == ["enable_experimental_features", "enable_items_to_include"]

    def test_no_match_raises_before_publishing(self, deployer, fabric_cicd):
        with pytest.raises(RuntimeError, match="matched no items"):
            deployer.deploy(include_items=["Ghost.Notebook"])
        fabric_cicd.workspace.assert_not_called()

    def test_summary_leaves_out_items_of_excluded_types(self, deployer, fabric_cicd, caplog):
        """An item excluded with -x stays in include_items but is never published."""
        with caplog.at_level("INFO"):
            deployer.deploy(include_items=[NOTEBOOK, MODEL], exclude_types=["SemanticModel"])
        summary = [r.message for r in caplog.records if r.message.startswith("Deployed ")]
        assert summary == [f"Deployed 1 item(s): {NOTEBOOK}"]


@pytest.mark.parametrize(
    ("options", "published"),
    [
        ({}, {LAKEHOUSE, NOTEBOOK, MODEL}),
        ({"item_types": ["notebook"]}, {NOTEBOOK}),
        ({"exclude_types": ["SemanticModel"]}, {LAKEHOUSE, NOTEBOOK}),
        ({"include_items": [NOTEBOOK, "Ghost.Notebook"]}, {NOTEBOOK}),
        ({"include_items": [NOTEBOOK, MODEL], "exclude_types": ["SemanticModel"]}, {NOTEBOOK}),
        ({"item_types": ["Notebook"], "include_items": [MODEL]}, set()),
        ({"pr": "abc1234"}, {NOTEBOOK}),
    ],
)
def test_deployed_items_are_what_the_deploy_published(
    deployer, fabric_cicd, monkeypatch, options, published
):
    """After -i matching, the -t/-x type scope and --pr, not the raw -i list."""
    monkeypatch.setattr(
        "fabric_deploy.workspace.changed_paths_in",
        lambda _repo, _commit: [f"{NOTEBOOK}/notebook-content.py"],
    )
    deployer.deploy(**options)
    assert set(deployer.deployed_items) == published


STALE = [("nb.Notebook/notebook-content.py", "00000000-0000-0000-0000-0000000000f0", "Unknown")]
UNBOUND = [
    (
        "Finance Report",
        "export_summary",
        "00000000-0000-0000-0000-0000000000f1",
        "no .UserDataFunction item in the repo declares 'export_summary'",
    )
]


@pytest.mark.parametrize(
    ("finding", "found", "options", "error"),
    [
        pytest.param(
            "unresolved_warehouse_bindings",
            STALE,
            {"fail_on_stale": True},
            "fail-on-stale",
            id="stale-binding-with-fail-on-stale",
        ),
        pytest.param("unresolved_warehouse_bindings", STALE, {}, None, id="stale-binding"),
        pytest.param(
            "unresolved_warehouse_bindings", [], {"fail_on_stale": True}, None, id="fail-on-stale"
        ),
        pytest.param(
            "udf_skipped", UNBOUND, {"item_types": ["Report"]}, "allow-unbound-udf", id="udf"
        ),
        pytest.param(
            "udf_skipped",
            UNBOUND,
            {"item_types": ["Report"], "allow_unbound_udf": True},
            None,
            id="udf-allowed",
        ),
        pytest.param(
            "udf_skipped", UNBOUND, {"item_types": ["Notebook"]}, None, id="udf-report-not-in-types"
        ),
        pytest.param(
            "udf_skipped", UNBOUND, {"include_items": [NOTEBOOK]}, None, id="udf-report-not-named"
        ),
        pytest.param("udf_skipped", [], {"item_types": ["Report"]}, None, id="no-unbound-udf"),
    ],
)
def test_guards_block_before_anything_is_published(
    deployer, fabric_cicd, finding, found, options, error
):
    """--fail-on-stale blocks a warehouse binding that can't be remapped; an
    unbound UDF button blocks when its report deploys, unless allowed."""
    setattr(deployer._detector, finding, list(found))

    if error:
        with pytest.raises(RuntimeError, match=error):
            deployer.deploy(**options)
        fabric_cicd.workspace.assert_not_called()
    else:
        deployer.deploy(**options)
        fabric_cicd.publish.assert_called_once()


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

    def _deploy(self, deployer, fabric_cicd) -> dict[str, object]:
        """Deploy; record what FabricWorkspace got and what the repo held at that moment."""
        seen: dict[str, object] = {}
        repo = deployer._repo_dir_resolved

        def record(**kwargs):
            path = kwargs["parameter_file_path"]
            seen["path"] = path
            seen["environment"] = kwargs["environment"]
            seen["document"] = yaml.safe_load(Path(path).read_text()) if path else None
            seen["repo_files"] = sorted(f.name for f in repo.iterdir())
            return MagicMock()

        fabric_cicd.workspace.side_effect = record
        deployer.deploy(item_types=["Notebook"])
        return seen

    def test_detected_rules_go_to_a_temporary_file_outside_the_repo(self, deployer, fabric_cicd):
        repo_files = sorted(f.name for f in deployer._repo_dir_resolved.iterdir())
        seen = self._deploy(deployer, fabric_cicd)

        path = seen["path"]
        assert isinstance(path, str)
        assert not Path(path).is_relative_to(deployer._repo_dir_resolved)
        assert not Path(path).exists(), "removed after the deploy"
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

    def test_user_file_is_merged_and_wins(self, deployer, fabric_cicd):
        user_file = deployer._repo_dir_resolved / "parameter.yml"
        user_file.write_text(self.USER_FILE)
        repo_files = sorted(f.name for f in deployer._repo_dir_resolved.iterdir())

        seen = self._deploy(deployer, fabric_cicd)

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

    def test_template_paths_resolve_from_the_repo(self, deployer, fabric_cicd):
        repo = deployer._repo_dir_resolved
        (repo / "params").mkdir()
        (repo / "params" / "ws.yml").write_text(
            f'find_replace:\n  - find_value: "{SAMPLE_DEV_WORKSPACE_ID}"\n'
            '    replace_value:\n      _ALL_: "from-template"\n'
        )
        (repo / "parameter.yml").write_text('extend:\n  - "params/ws.yml"\n')

        document = self._deploy(deployer, fabric_cicd)["document"]

        assert isinstance(document, dict)
        assert document["extend"] == [str(repo / "params" / "ws.yml")]
        assert [e["find_value"] for e in document["find_replace"]] == [SAMPLE_LAKEHOUSE_ID]

    def test_malformed_user_file_fails_the_deploy(self, deployer, fabric_cicd):
        (deployer._repo_dir_resolved / "parameter.yml").write_text('extend: "one.yml"\n')
        with pytest.raises(ValueError, match="parameter.yml"):
            self._deploy(deployer, fabric_cicd)

    def test_nothing_to_add_leaves_fabric_cicd_on_the_repo_file(self, deployer, fabric_cicd):
        deployer._detector.parameters = {}
        seen = self._deploy(deployer, fabric_cicd)
        assert seen["path"] is None
        assert seen["environment"] == "MAIN"


# --- sibling-workspace anchors ------------------------------------------------

# The sample notebook's lakehouse metadata names this workspace.
DEV_WORKSPACE_ID = SAMPLE_DEV_WORKSPACE_ID
QA_WORKSPACE_ID = "00000000-0000-0000-0000-00000000000d"
SRC_LAKEHOUSE_ID = "00000000-0000-0000-0000-00000000001a"


def _returning(value: object) -> MagicMock:
    """A mock that raises `value` if it is an exception, else returns it."""
    if isinstance(value, Exception):
        return MagicMock(side_effect=value)
    return MagicMock(return_value=value)


def _with_listing(monkeypatch, repo: Path, workspaces: object, lakehouses: object = ()):
    """A deployer whose identity lists `workspaces`, and `lakehouses` in each one
    (either may be the exception the listing raises)."""
    list_items = _returning(lakehouses if isinstance(lakehouses, Exception) else list(lakehouses))
    monkeypatch.setattr("fabric_deploy.workspace.list_workspaces", _returning(workspaces))
    monkeypatch.setattr("fabric_deploy.workspace.list_workspace_items", list_items)
    return WorkspaceDeployer(repo, branch_override="MAIN"), list_items


def _ws(workspace_id: str, name: str) -> dict[str, str]:
    return {"id": workspace_id, "displayName": name}


@pytest.mark.usefixtures("mock_fabric_api")
class TestSiblingWorkspaceAnchors:
    def test_target_is_anchored_even_without_siblings(self, monkeypatch, sample_ws):
        d, list_items = _with_listing(monkeypatch, sample_ws, [])

        assert d.source_workspace_ids == {FAKE_WORKSPACE_ID}
        assert d.source_lakehouse_ids == {}
        list_items.assert_not_called()
        assert d._detector is not None
        assert FAKE_WORKSPACE_ID in d._detector._source_workspace_ids

    def test_referenced_sibling_anchors_and_lists_its_lakehouses(self, monkeypatch, sample_ws):
        """The content names DEV (in notebook metadata here; a pipeline
        workspaceId or an abfss literal elsewhere), so DEV's lakehouse ids are
        fetched to map alongside its workspace id."""
        d, list_items = _with_listing(
            monkeypatch,
            sample_ws,
            [
                _ws(FAKE_WORKSPACE_ID, "Fabdep Test [MAIN]"),
                _ws(DEV_WORKSPACE_ID, "Fabdep Test [DEV]"),
            ],
            [{"id": SRC_LAKEHOUSE_ID, "displayName": "fabdep_lakehouse", "type": "Lakehouse"}],
        )

        list_items.assert_called_once_with(DEV_WORKSPACE_ID, "Lakehouse")
        assert d.source_workspace_ids == {FAKE_WORKSPACE_ID, DEV_WORKSPACE_ID}
        assert d.source_lakehouse_ids == {SRC_LAKEHOUSE_ID: "fabdep_lakehouse"}
        assert d._detector is not None
        assert d._detector.parameters[DEV_WORKSPACE_ID].dynamic_variable == "$workspace.$id"

    def test_only_the_repos_workspaces_are_anchored(self, monkeypatch, sample_ws):
        """Siblings follow workspace_name_template (see test_config); an
        unreferenced one is anchored without listing its lakehouses."""
        siblings = [
            _ws(QA_WORKSPACE_ID, "Fabdep Test [QA]"),
            _ws(DEV_WORKSPACE_ID, "fabdep test [uat]"),
        ]
        others = [
            _ws("00000000-0000-0000-0000-0000000000e0", name)
            for name in (
                "Other [DEV]",
                "Fabdep Test Extra [DEV]",
                "Fabdep Test",
                "Fabdep Test [DEV] [OLD]",
                "Fabdep Test [ARCHIVE] Copy [DEV]",
            )
        ]
        d, list_items = _with_listing(monkeypatch, sample_ws, siblings + others)

        assert d.source_workspace_ids == {FAKE_WORKSPACE_ID, QA_WORKSPACE_ID, DEV_WORKSPACE_ID}
        # Only DEV's id appears in the content.
        list_items.assert_called_once_with(DEV_WORKSPACE_ID, "Lakehouse")

    def test_environments_map_names_the_siblings(self, monkeypatch, sample_ws):
        (sample_ws / "fabdep.yml").write_text(
            'workspace_base_name: "Fabdep Test"\n'
            "environments:\n  MAIN:\n  dev:\n    workspace: Fabdep Dev\n"
        )
        d, _ = _with_listing(
            monkeypatch,
            sample_ws,
            [_ws(DEV_WORKSPACE_ID, "Fabdep Dev"), _ws(QA_WORKSPACE_ID, "Fabdep Test [QA]")],
        )

        assert d.source_workspace_ids == {FAKE_WORKSPACE_ID, DEV_WORKSPACE_ID}

    def test_listing_failure_warns_and_keeps_target_anchor(self, monkeypatch, sample_ws, caplog):
        d, _ = _with_listing(monkeypatch, sample_ws, RuntimeError("403"))

        assert d.source_workspace_ids == {FAKE_WORKSPACE_ID}
        assert "Could not list workspaces (403)" in caplog.text

    def test_lakehouse_listing_failure_warns_and_continues(self, monkeypatch, sample_ws, caplog):
        d, _ = _with_listing(
            monkeypatch,
            sample_ws,
            [_ws(DEV_WORKSPACE_ID, "Fabdep Test [DEV]")],
            RuntimeError("403"),
        )

        assert DEV_WORKSPACE_ID in d.source_workspace_ids
        assert d.source_lakehouse_ids == {}
        assert "Fabdep Test [DEV]" in caplog.text


def test_skip_lakehouse_warns_that_auto_bound_notebooks_rebind_anyway(
    tmp_path: Path, mock_fabric_api, caplog
):
    """fabric-cicd resolves a lakehouse logicalId to the target's lakehouse
    before parameters apply, so -L cannot keep an auto-bound notebook on the
    source lakehouse; say so instead of silently not doing it."""
    logical_id = "00000000-0000-0000-0000-0000000000b1"
    (tmp_path / "fabdep.yml").write_text('workspace_base_name: "Base"\n')
    make_item(tmp_path, "lh", "Lakehouse", logical_id=logical_id)
    make_item(
        tmp_path,
        "nb",
        "Notebook",
        files={
            "notebook-content.py": (
                '# META {\n# META   "dependencies": {"lakehouse": {\n'
                f'# META     "default_lakehouse": "{logical_id}",\n'
                '# META     "default_lakehouse_workspace_id": "00000000-0000-0000-0000-000000000000"\n'
                "# META   }}\n# META }\n"
            ),
            "notebook-settings.json": {"auto-binding": {"lakehouse": "on"}},
        },
    )

    WorkspaceDeployer(tmp_path, branch_override="MAIN", skip_lakehouse=True)

    assert "--skip-lakehouse" in caplog.text
    assert "nb.Notebook" in caplog.text


def test_deploy_names_source_and_target_without_a_banner(deployer, fabric_cicd, caplog):
    deployer.source_workspaces = {QA_WORKSPACE_ID: "Fabdep Test [QA]"}
    with caplog.at_level("INFO"):
        deployer.deploy(item_types=["Notebook"])

    messages = [r.message for r in caplog.records]
    assert not any("fabdep v" in m for m in messages)
    assert f"Source Workspace: Fabdep Test [QA] ({QA_WORKSPACE_ID})" in messages
    assert f"Target Workspace: 'Fabdep Test [MAIN]' ({FAKE_WORKSPACE_ID})" in messages
    assert not any(m.startswith(("Workspace Name", "Parameter file")) for m in messages)
