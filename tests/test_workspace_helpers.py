"""Unit tests for workspace.py helpers that didn't have direct coverage:
_resolve_item_scope and _add_domain_substitutions.

Both are pure logic on inputs the deployer holds in memory, so the tests
build a deployer via the standard fixtures, then call the methods directly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from fabric_deploy.detect import DetectedParameter, ParameterDetector
from fabric_deploy.workspace import WorkspaceDeployer


def _seed(repo: Path) -> None:
    lh = repo / "lh.Lakehouse"
    lh.mkdir()
    (lh / ".platform").write_text(
        json.dumps({"metadata": {"displayName": "lh", "type": "Lakehouse"}})
    )
    (repo / "fabdep.yml").write_text('workspace_base_name: "ws"\n')


@pytest.fixture
def deployer(tmp_path: Path, mock_fabric_api) -> WorkspaceDeployer:
    _seed(tmp_path)
    return WorkspaceDeployer(tmp_path, branch_override="dev")


# --- _resolve_item_scope --------------------------------------------------


class TestResolveItemScope:
    def test_no_filters_returns_none(self, deployer):
        assert deployer._resolve_item_scope(None, None, None) is None

    def test_item_types_returned_directly(self, deployer):
        scope = deployer._resolve_item_scope(["Notebook", "Report"], None, None)
        assert scope == ["Notebook", "Report"]

    def test_item_types_normalized_to_canonical_case(self, deployer):
        scope = deployer._resolve_item_scope(["notebook", "REPORT"], None, None)
        assert scope == ["Notebook", "Report"]

    def test_exclude_types_subtracts_from_accepted(self, deployer):
        from fabric_cicd.constants import ACCEPTED_ITEM_TYPES

        scope = deployer._resolve_item_scope(None, ["Notebook"], None)
        assert "Notebook" not in scope
        assert "Report" in scope
        assert len(scope) == len(ACCEPTED_ITEM_TYPES) - 1

    def test_item_types_takes_priority_over_exclude(self, deployer):
        """When both are given, --item-types wins."""
        scope = deployer._resolve_item_scope(
            ["Notebook"], ["Notebook", "Report"], None
        )
        assert scope == ["Notebook"]

    def test_include_items_derives_scope_from_suffixes(self, deployer):
        scope = deployer._resolve_item_scope(
            None, None, ["Foo.Notebook", "Bar.Report"]
        )
        assert sorted(scope) == ["Notebook", "Report"]

    def test_include_items_ignored_when_item_types_provided(self, deployer):
        scope = deployer._resolve_item_scope(
            ["SemanticModel"], None, ["Foo.Notebook"]
        )
        assert scope == ["SemanticModel"]

    def test_unknown_item_type_logged_as_warning(self, deployer, caplog):
        with caplog.at_level("WARNING"):
            deployer._resolve_item_scope(["Notebook", "BogusType"], None, None)
        assert any("BogusType" in rec.message for rec in caplog.records)

    def test_include_items_skips_entries_without_known_suffix(self, deployer):
        scope = deployer._resolve_item_scope(
            None, None, ["Foo.Notebook", "PlainName", "Bar.NotAType"]
        )
        assert scope == ["Notebook"]


# --- _add_domain_substitutions -------------------------------------------


def _write_param_file(path: Path, find_replace: list[dict]) -> None:
    yaml.dump({"find_replace": find_replace}, path.open("w"), sort_keys=False)


class TestAddDomainSubstitutions:
    """Pin the second-pass FQDN rewrite that handles non-default SemanticModel
    connection domains (database / pbidedicated)."""

    def _seed_param_file(self, repo: Path, host: str) -> Path:
        param_file = repo / "parameter.yml"
        _write_param_file(
            param_file,
            [
                {
                    "find_value": host,
                    "replace_value": {"fabdep": "$items.Lakehouse.lh.$sqlendpoint"},
                    "item_type": ["SemanticModel"],
                }
            ],
        )
        return param_file

    def _detector_with(self, **metadata) -> ParameterDetector:
        d = ParameterDetector(repo_dir=Path("/tmp"))
        d.parameters = {
            metadata.get("host", "h"): DetectedParameter(
                find_value=metadata.get("host", "h"),
                description="SQL Database Connection String",
                metadata={
                    "domain_type": metadata.get("domain_type", "database"),
                    "domain_suffix": metadata.get("domain_suffix", "fabric.microsoft.com"),
                    "db_name": metadata.get("db_name", "mydb"),
                    "host_prefix": metadata.get("host_prefix", "HOST"),
                },
            )
        }
        return d

    def test_database_domain_appends_database_rewrite(self, deployer, tmp_path):
        param_file = self._seed_param_file(tmp_path, "HOST.datawarehouse.fabric.microsoft.com")
        detector = self._detector_with(domain_type="database", db_name="mydb")

        deployer._add_domain_substitutions(param_file, detector)

        with open(param_file) as f:
            data = yaml.safe_load(f)
        appended = data["find_replace"][-1]
        assert appended["find_value"] == '.datawarehouse.fabric.microsoft.com","mydb'
        assert appended["replace_value"]["fabdep"] == '.database.fabric.microsoft.com","mydb'
        assert appended["item_type"] == ["SemanticModel"]

    def test_pbidedicated_suffix_appends_suffix_rewrite(self, deployer, tmp_path):
        param_file = self._seed_param_file(tmp_path, "HOST.datawarehouse.pbidedicated.windows.net")
        detector = self._detector_with(
            domain_type="datawarehouse",
            domain_suffix="pbidedicated.windows.net",
            db_name="lh",
        )

        deployer._add_domain_substitutions(param_file, detector)

        with open(param_file) as f:
            data = yaml.safe_load(f)
        appended = data["find_replace"][-1]
        assert appended["find_value"] == '.datawarehouse.fabric.microsoft.com","lh'
        assert appended["replace_value"]["fabdep"] == '.datawarehouse.pbidedicated.windows.net","lh'

    def test_no_qualifying_params_leaves_file_unchanged(self, deployer, tmp_path):
        original = [
            {
                "find_value": "anything",
                "replace_value": {"fabdep": "x"},
                "item_type": ["Notebook"],
            }
        ]
        param_file = tmp_path / "parameter.yml"
        _write_param_file(param_file, original)
        # Default-domain entry: standard datawarehouse + fabric.microsoft.com → no substitution needed.
        detector = self._detector_with(
            domain_type="datawarehouse", domain_suffix="fabric.microsoft.com"
        )

        deployer._add_domain_substitutions(param_file, detector)

        with open(param_file) as f:
            data = yaml.safe_load(f)
        assert data["find_replace"] == original

    def test_param_without_db_name_is_skipped(self, deployer, tmp_path):
        param_file = self._seed_param_file(tmp_path, "HOST.database.fabric.microsoft.com")
        # database domain but no db_name → can't construct the rewrite.
        detector = self._detector_with(domain_type="database", db_name="")

        deployer._add_domain_substitutions(param_file, detector)

        with open(param_file) as f:
            data = yaml.safe_load(f)
        # Only the seeded entry, no appended rewrite.
        assert len(data["find_replace"]) == 1
