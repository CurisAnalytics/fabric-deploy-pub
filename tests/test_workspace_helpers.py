"""Unit tests for scope.resolve_item_scope and params._apply_domain_substitutions.

Both are now pure functions (no deployer state), so the tests call them
directly with a plain logger / inputs.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from fabric_deploy.detect import DetectedParameter, ParameterDetector
from fabric_deploy.params import _apply_domain_substitutions
from fabric_deploy.scope import resolve_item_scope

LOG = logging.getLogger("test.scope")


# --- resolve_item_scope ---------------------------------------------------


class TestResolveItemScope:
    def test_no_filters_returns_none(self):
        assert resolve_item_scope(None, None, None, LOG) is None

    def test_item_types_returned_directly(self):
        assert resolve_item_scope(["Notebook", "Report"], None, None, LOG) == [
            "Notebook",
            "Report",
        ]

    def test_item_types_normalized_to_canonical_case(self):
        assert resolve_item_scope(["notebook", "REPORT"], None, None, LOG) == [
            "Notebook",
            "Report",
        ]

    def test_exclude_types_subtracts_from_accepted(self):
        from fabric_cicd.constants import ACCEPTED_ITEM_TYPES

        scope = resolve_item_scope(None, ["Notebook"], None, LOG)
        assert "Notebook" not in scope
        assert "Report" in scope
        assert len(scope) == len(ACCEPTED_ITEM_TYPES) - 1

    def test_item_types_takes_priority_over_exclude(self):
        """When both are given, --item-types wins."""
        assert resolve_item_scope(["Notebook"], ["Notebook", "Report"], None, LOG) == [
            "Notebook"
        ]

    def test_include_items_derives_scope_from_suffixes(self):
        scope = resolve_item_scope(None, None, ["Foo.Notebook", "Bar.Report"], LOG)
        assert sorted(scope) == ["Notebook", "Report"]

    def test_include_items_ignored_when_item_types_provided(self):
        assert resolve_item_scope(["SemanticModel"], None, ["Foo.Notebook"], LOG) == [
            "SemanticModel"
        ]

    def test_unknown_item_type_logged_as_warning(self, caplog):
        with caplog.at_level("WARNING"):
            resolve_item_scope(["Notebook", "BogusType"], None, None, LOG)
        assert any("BogusType" in rec.message for rec in caplog.records)

    def test_include_items_skips_entries_without_known_suffix(self):
        scope = resolve_item_scope(
            None, None, ["Foo.Notebook", "PlainName", "Bar.NotAType"], LOG
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

    def test_database_domain_appends_database_rewrite(self, tmp_path):
        param_file = self._seed_param_file(tmp_path, "HOST.datawarehouse.fabric.microsoft.com")
        detector = self._detector_with(domain_type="database", db_name="mydb")

        _apply_domain_substitutions(param_file, detector)

        with open(param_file) as f:
            data = yaml.safe_load(f)
        appended = data["find_replace"][-1]
        assert appended["find_value"] == '.datawarehouse.fabric.microsoft.com","mydb'
        assert appended["replace_value"]["fabdep"] == '.database.fabric.microsoft.com","mydb'
        assert appended["item_type"] == ["SemanticModel"]

    def test_pbidedicated_suffix_appends_suffix_rewrite(self, tmp_path):
        param_file = self._seed_param_file(tmp_path, "HOST.datawarehouse.pbidedicated.windows.net")
        detector = self._detector_with(
            domain_type="datawarehouse",
            domain_suffix="pbidedicated.windows.net",
            db_name="lh",
        )

        _apply_domain_substitutions(param_file, detector)

        with open(param_file) as f:
            data = yaml.safe_load(f)
        appended = data["find_replace"][-1]
        assert appended["find_value"] == '.datawarehouse.fabric.microsoft.com","lh'
        assert appended["replace_value"]["fabdep"] == '.datawarehouse.pbidedicated.windows.net","lh'

    def test_no_qualifying_params_leaves_file_unchanged(self, tmp_path):
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

        _apply_domain_substitutions(param_file, detector)

        with open(param_file) as f:
            data = yaml.safe_load(f)
        assert data["find_replace"] == original

    def test_param_without_db_name_is_skipped(self, tmp_path):
        param_file = self._seed_param_file(tmp_path, "HOST.database.fabric.microsoft.com")
        # database domain but no db_name → can't construct the rewrite.
        detector = self._detector_with(domain_type="database", db_name="")

        _apply_domain_substitutions(param_file, detector)

        with open(param_file) as f:
            data = yaml.safe_load(f)
        # Only the seeded entry, no appended rewrite.
        assert len(data["find_replace"]) == 1
