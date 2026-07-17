"""Unit tests for WorkspaceDeployer._load_manual_parameters.

Manual parameter overrides let users keep the auto-detected `find_value` while
supplying a custom `replace_value` (e.g. for external references) and
optionally scoping to specific item types.
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
    (nb / "notebook-content.py").write_text(
        f'"default_lakehouse_workspace_id": "{detected_uuid}"\n'
    )

    (repo / "fabdep.yml").write_text(fabdep_yml)


class TestManualParameters:
    def test_override_matches_detected_replaces_dynamic_variable(
        self, tmp_path: Path, mock_fabric_api
    ):
        _build_repo(
            tmp_path,
            'workspace_base_name: "ws"\n'
            'manual_parameters:\n'
            f'  - find_value: "{DETECTED_UUID}"\n'
            '    replace_value: "kept-as-external"\n'
            '    description: "external workspace"\n',
        )

        d = WorkspaceDeployer(tmp_path, branch_override="dev")

        param = d._detector.parameters[DETECTED_UUID]
        assert param.dynamic_variable == "kept-as-external"
        assert "manual override: external workspace" in param.description
        assert d._manual_count == 1

    def test_override_unmatched_logs_warning_and_does_not_add(
        self, tmp_path: Path, mock_fabric_api, caplog
    ):
        _build_repo(
            tmp_path,
            'workspace_base_name: "ws"\n'
            'manual_parameters:\n'
            f'  - find_value: "{UNRELATED_UUID}"\n'
            '    replace_value: "anything"\n',
        )

        with caplog.at_level("WARNING"):
            d = WorkspaceDeployer(tmp_path, branch_override="dev")

        assert UNRELATED_UUID not in d._detector.parameters
        assert d._manual_count == 0
        assert any(UNRELATED_UUID in rec.message for rec in caplog.records)

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
            'manual_parameters:\n'
            f'  - find_value: "{DETECTED_UUID}"\n'
            '    replace_value: "x"\n'
            f'    item_type: {item_type_yaml}\n',
        )

        d = WorkspaceDeployer(tmp_path, branch_override="dev")

        assert d._detector.parameters[DETECTED_UUID].item_types == expected

    def test_dict_replace_value_extracts_first_value(
        self, tmp_path: Path, mock_fabric_api
    ):
        """When replace_value is given as {env: value}, take the value."""
        _build_repo(
            tmp_path,
            'workspace_base_name: "ws"\n'
            'manual_parameters:\n'
            f'  - find_value: "{DETECTED_UUID}"\n'
            '    replace_value:\n'
            '      prod: "prod-value"\n',
        )

        d = WorkspaceDeployer(tmp_path, branch_override="dev")

        assert d._detector.parameters[DETECTED_UUID].dynamic_variable == "prod-value"

    def test_missing_find_value_or_replace_value_is_skipped(
        self, tmp_path: Path, mock_fabric_api, caplog
    ):
        _build_repo(
            tmp_path,
            'workspace_base_name: "ws"\n'
            'manual_parameters:\n'
            '  - replace_value: "no-find"\n'
            f'  - find_value: "{DETECTED_UUID}"\n'
            '  - find_value: "valid-but-unmatched"\n'
            '    replace_value: "x"\n',
        )

        with caplog.at_level("WARNING"):
            d = WorkspaceDeployer(tmp_path, branch_override="dev")

        # Only the third entry was a complete override, but its find_value
        # wasn't detected — so the manual count is 0.
        assert d._manual_count == 0

        # The two malformed entries should produce skip warnings.
        skip_warnings = [
            r for r in caplog.records if "missing find_value or replace_value" in r.message
        ]
        assert len(skip_warnings) >= 2
