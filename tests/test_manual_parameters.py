"""Unit tests for fabdep.yml manual_parameters handling.

Overrides keep an auto-detected `find_value` while supplying a custom
`replace_value` (e.g. for external references), optionally scoped to item
types. Injections cover find_values detection can't see (e.g. a PowerApp app
id in report JSON) — honored only when the value literally occurs in an item
file, so a typo'd or stale entry still warns instead of silently no-oping.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fabric_deploy.workspace import WorkspaceDeployer

DETECTED_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
UNRELATED_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _build_repo(repo: Path, fabdep_yml: str, detected_uuid: str = DETECTED_UUID) -> None:
    """Seed a repo with one Lakehouse, one Notebook (which makes
    `detected_uuid` a detected Workspace ID), and the given fabdep.yml."""
    lh = repo / "lh.Lakehouse"
    lh.mkdir()
    (lh / ".platform").write_text(
        json.dumps({"metadata": {"displayName": "lh", "type": "Lakehouse"}})
    )

    nb = repo / "n1.Notebook"
    nb.mkdir()
    (nb / ".platform").write_text(
        json.dumps({"metadata": {"displayName": "n1", "type": "Notebook"}})
    )
    meta = {
        "default_lakehouse": "cccccccc-cccc-cccc-cccc-cccccccccccc",
        "default_lakehouse_name": "lh",
        "default_lakehouse_workspace_id": detected_uuid,
    }
    (nb / "notebook-content.py").write_text(
        "# META " + json.dumps({"dependencies": {"lakehouse": meta}}) + "\n"
    )

    (repo / "fabdep.yml").write_text(fabdep_yml)


class TestManualParameters:
    def test_override_matches_detected_replaces_dynamic_variable(
        self, tmp_path: Path, mock_fabric_api
    ):
        _build_repo(
            tmp_path,
            'workspace_base_name: "ws"\n'
            "manual_parameters:\n"
            f'  - find_value: "{DETECTED_UUID}"\n'
            '    replace_value: "kept-as-external"\n'
            '    description: "external workspace"\n',
        )

        d = WorkspaceDeployer(tmp_path, branch_override="dev")

        param = d._detector.parameters[DETECTED_UUID]
        assert param.dynamic_variable == "kept-as-external"
        assert "fabdep.yml" in param.source_files
        assert d._manual_count == 1

    def test_value_absent_from_repo_logs_warning_and_does_not_add(
        self, tmp_path: Path, mock_fabric_api, caplog
    ):
        """Undetected AND not present in any item file -> typo/stale, skipped."""
        _build_repo(
            tmp_path,
            'workspace_base_name: "ws"\n'
            "manual_parameters:\n"
            f'  - find_value: "{UNRELATED_UUID}"\n'
            '    replace_value: "anything"\n',
        )

        with caplog.at_level("WARNING"):
            d = WorkspaceDeployer(tmp_path, branch_override="dev")

        assert UNRELATED_UUID not in d._detector.parameters
        assert d._manual_count == 0
        assert any(UNRELATED_UUID in rec.message for rec in caplog.records)

    def test_undetected_value_in_item_file_is_injected(self, tmp_path: Path, mock_fabric_api):
        """A find_value detection didn't see, but which exists in an item file
        (e.g. a PowerApp app id in report JSON), becomes a new parameter."""
        _build_repo(
            tmp_path,
            'workspace_base_name: "ws"\n'
            "manual_parameters:\n"
            f'  - find_value: "{UNRELATED_UUID}"\n'
            '    replace_value: "new-app-guid"\n'
            '    item_type: ["Report"]\n'
            '    description: "PowerApp link"\n',
        )
        rpt = tmp_path / "r1.Report"
        (rpt / "definition" / "pages" / "p1").mkdir(parents=True)
        (rpt / ".platform").write_text(
            json.dumps({"metadata": {"displayName": "r1", "type": "Report"}})
        )
        (rpt / "definition" / "pages" / "p1" / "visual.json").write_text(
            json.dumps({"appId": f"/providers/Microsoft.PowerApps/apps/{UNRELATED_UUID}"})
        )

        d = WorkspaceDeployer(tmp_path, branch_override="dev")

        param = d._detector.parameters[UNRELATED_UUID]
        assert param.dynamic_variable == "new-app-guid"
        assert param.item_types == {"Report"}
        assert param.description == "PowerApp link"
        assert "r1.Report/definition/pages/p1/visual.json" in param.source_files
        assert "fabdep.yml" in param.source_files
        assert d._manual_count == 1

        # And it survives into the emitted find_replace structure.
        entry = next(
            e
            for e in d._detector.generate_parameter_yml()["find_replace"]
            if e["find_value"] == UNRELATED_UUID
        )
        assert entry["item_type"] == ["Report"]
        assert entry["replace_value"] == {"_ALL_": "new-app-guid"}

    def test_value_only_in_fabdep_yml_is_not_injected(
        self, tmp_path: Path, mock_fabric_api, caplog
    ):
        """The scan covers item files only — an entry must not match itself in
        fabdep.yml (or any other repo-root file)."""
        _build_repo(
            tmp_path,
            'workspace_base_name: "ws"\n'
            "manual_parameters:\n"
            '  - find_value: "only-lives-in-fabdep-yml"\n'
            '    replace_value: "x"\n',
        )

        with caplog.at_level("WARNING"):
            d = WorkspaceDeployer(tmp_path, branch_override="dev")

        assert "only-lives-in-fabdep-yml" not in d._detector.parameters
        assert d._manual_count == 0

    @pytest.mark.parametrize(
        ("item_type_yaml", "expected"),
        [
            ('["SemanticModel", "Report"]', {"SemanticModel", "Report"}),
            ('"SemanticModel"', {"SemanticModel"}),  # string normalizes to a set
        ],
    )
    def test_item_type_normalizes_to_set(
        self, tmp_path: Path, mock_fabric_api, item_type_yaml: str, expected: set[str]
    ):
        _build_repo(
            tmp_path,
            'workspace_base_name: "ws"\n'
            "manual_parameters:\n"
            f'  - find_value: "{DETECTED_UUID}"\n'
            '    replace_value: "x"\n'
            f"    item_type: {item_type_yaml}\n",
        )

        d = WorkspaceDeployer(tmp_path, branch_override="dev")

        assert d._detector.parameters[DETECTED_UUID].item_types == expected

    @pytest.mark.parametrize(
        ("branch", "expected"),
        [("dev", "dev-value"), ("prod", "prod-value"), ("test", "any-value")],
    )
    def test_environment_keyed_replace_value(
        self, tmp_path: Path, mock_fabric_api, branch: str, expected: str
    ):
        """A mapping picks the deploy environment's value, then `_ALL_`."""
        _build_repo(
            tmp_path,
            'workspace_base_name: "ws"\n'
            "manual_parameters:\n"
            f'  - find_value: "{DETECTED_UUID}"\n'
            "    replace_value:\n"
            '      dev: "dev-value"\n'
            '      prod: "prod-value"\n'
            '      _ALL_: "any-value"\n',
        )

        d = WorkspaceDeployer(tmp_path, branch_override=branch)

        assert d.environment == branch
        assert d._detector.parameters[DETECTED_UUID].dynamic_variable == expected

    def test_override_without_a_value_for_the_environment_keeps_the_detected_rule(
        self, tmp_path: Path, mock_fabric_api
    ):
        _build_repo(
            tmp_path,
            'workspace_base_name: "ws"\n'
            "manual_parameters:\n"
            f'  - find_value: "{DETECTED_UUID}"\n'
            "    replace_value:\n"
            '      prod: "prod-value"\n',
        )

        d = WorkspaceDeployer(tmp_path, branch_override="dev")

        assert d._detector.parameters[DETECTED_UUID].dynamic_variable == "$workspace.$id"
        assert d._manual_count == 0

    def test_injection_without_a_value_for_the_environment_is_skipped(
        self, tmp_path: Path, mock_fabric_api
    ):
        _build_repo(
            tmp_path,
            'workspace_base_name: "ws"\n'
            "manual_parameters:\n"
            f'  - find_value: "{UNRELATED_UUID}"\n'
            "    replace_value:\n"
            '      prod: "prod-value"\n',
        )
        (tmp_path / "n1.Notebook" / "notebook-content.py").write_text(f"x = '{UNRELATED_UUID}'\n")

        d = WorkspaceDeployer(tmp_path, branch_override="dev")

        assert UNRELATED_UUID not in d._detector.parameters

    def test_explicit_environment_overrides_the_branch(self, tmp_path: Path, mock_fabric_api):
        _build_repo(
            tmp_path,
            'workspace_base_name: "ws"\n'
            "manual_parameters:\n"
            f'  - find_value: "{DETECTED_UUID}"\n'
            "    replace_value:\n"
            '      Production: "prod-value"\n',
        )

        d = WorkspaceDeployer(tmp_path, branch_override="main", environment="Production")

        assert d._detector.parameters[DETECTED_UUID].dynamic_variable == "prod-value"

    def test_missing_find_value_or_replace_value_is_skipped(
        self, tmp_path: Path, mock_fabric_api, caplog
    ):
        _build_repo(
            tmp_path,
            'workspace_base_name: "ws"\n'
            "manual_parameters:\n"
            '  - replace_value: "no-find"\n'
            f'  - find_value: "{DETECTED_UUID}"\n'
            '  - find_value: "valid-but-unmatched"\n'
            '    replace_value: "x"\n',
        )

        with caplog.at_level("WARNING"):
            d = WorkspaceDeployer(tmp_path, branch_override="dev")

        # Only the third entry was complete, but its find_value is neither
        # detected nor present in any item file — so the manual count is 0.
        assert d._manual_count == 0

        # The two malformed entries should produce skip warnings.
        skip_warnings = [
            r for r in caplog.records if "missing find_value or replace_value" in r.message
        ]
        assert len(skip_warnings) >= 2
