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
from fabric_deploy.scope import (
    _repo_items,
    repo_item_types,
    resolve_item_scope,
    validate_include_items,
)

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


# --- .platform-driven item discovery ---------------------------------------


def _make_item(repo: Path, dir_name: str, display: str, platform_type: str) -> None:
    import json

    item = repo / dir_name
    item.mkdir(parents=True)
    (item / ".platform").write_text(
        json.dumps({"metadata": {"displayName": display, "type": platform_type}})
    )


class TestPlatformTypeDiscovery:
    """Item types come from .platform metadata, not the directory suffix —
    a `*.SynapseNotebook/` dir with `type: Notebook` is a Notebook."""

    def test_repo_item_types_uses_platform_type(self, tmp_path: Path):
        _make_item(tmp_path, "foo.SynapseNotebook", "foo", "Notebook")
        assert repo_item_types(tmp_path) == {"Notebook"}

    def test_repo_items_keys_on_platform_type(self, tmp_path: Path):
        _make_item(tmp_path, "foo.SynapseNotebook", "foo", "Notebook")
        assert _repo_items(tmp_path) == {"foo.Notebook": "foo.SynapseNotebook"}

    def test_include_items_resolves_synapse_notebook(self, tmp_path: Path):
        _make_item(tmp_path, "foo.SynapseNotebook", "foo", "Notebook")
        assert validate_include_items(["foo.Notebook"], tmp_path, LOG) == [
            "foo.Notebook"
        ]

    def test_unaccepted_platform_type_is_ignored(self, tmp_path: Path):
        _make_item(tmp_path, "foo.Notebook", "foo", "NotARealType")
        assert repo_item_types(tmp_path) == set()
        assert _repo_items(tmp_path) == {}


# --- _add_domain_substitutions -------------------------------------------


def _write_param_file(path: Path, find_replace: list[dict]) -> None:
    yaml.dump({"find_replace": find_replace}, path.open("w"), sort_keys=False)


class TestAddDomainSubstitutions:
    """Pin the second-pass FQDN rewrite for pbidedicated SemanticModel
    connections. SQL Database (`.database.*`) endpoints get NO rewrite —
    their `$sqlendpoint` dynamic already resolves to the operational
    `.database.fabric.microsoft.com` host (serverFqdn)."""

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
                description="SQL Analytics Endpoint Connection String",
                metadata={
                    "domain_type": metadata.get("domain_type", "datawarehouse"),
                    "domain_suffix": metadata.get("domain_suffix", "fabric.microsoft.com"),
                    "db_name": metadata.get("db_name", "mydb"),
                    "host_prefix": metadata.get("host_prefix", "HOST"),
                },
            )
        }
        return d

    def test_pbidedicated_suffix_appends_regex_rewrite(self, tmp_path):
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
        assert appended["is_regex"] == "true"
        assert appended["replace_value"]["fabdep"] == "pbidedicated.windows.net"
        assert appended["item_type"] == ["SemanticModel"]

        # fabric-cicd replaces only the single captured group (the suffix), so
        # the pattern must have exactly one group and match BOTH arg spacings —
        # real TMDL serializes `", "` (the old literal-`","` find never matched).
        import re

        pattern = re.compile(appended["find_value"])
        assert pattern.groups == 1
        for line in (
            'Sql.Database("HOST.datawarehouse.fabric.microsoft.com", "lh")',
            'Sql.Database("HOST.datawarehouse.fabric.microsoft.com","lh")',
        ):
            m = pattern.search(line)
            assert m is not None, line
            assert m.group(1) == "fabric.microsoft.com"

    def test_database_domain_gets_no_rewrite(self, tmp_path):
        """SQL Database endpoints resolve correctly via $sqlendpoint (serverFqdn
        is already the .database host) — appending a rewrite would be a no-op
        rule at best."""
        original = [
            {
                "find_value": "HOST.database.fabric.microsoft.com",
                "replace_value": {"fabdep": "$items.SQLDatabase.mydb.$sqlendpoint"},
                "item_type": ["SemanticModel"],
            }
        ]
        param_file = tmp_path / "parameter.yml"
        _write_param_file(param_file, original)
        detector = self._detector_with(domain_type="database", db_name="mydb")

        _apply_domain_substitutions(param_file, detector)

        with open(param_file) as f:
            data = yaml.safe_load(f)
        assert data["find_replace"] == original

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
        param_file = self._seed_param_file(tmp_path, "HOST.datawarehouse.pbidedicated.windows.net")
        # pbidedicated domain but no db_name → can't construct the rewrite.
        detector = self._detector_with(
            domain_type="datawarehouse",
            domain_suffix="pbidedicated.windows.net",
            db_name="",
        )

        _apply_domain_substitutions(param_file, detector)

        with open(param_file) as f:
            data = yaml.safe_load(f)
        # Only the seeded entry, no appended rewrite.
        assert len(data["find_replace"]) == 1


# --- resolve_warehouse_endpoint_params -------------------------------------


class TestResolveWarehouseEndpointParams:
    EP_GUID = "66666666-6666-6666-6666-666666666666"
    TARGET_EP = "77777777-7777-7777-7777-777777777777"
    WS = "00000000-0000-0000-0000-000000000001"

    def _detector_with_binding(self, tmp_path: Path) -> ParameterDetector:
        d = ParameterDetector(tmp_path)
        d._sqldatabase_names = ["mydb"]
        d.parameters = {
            self.EP_GUID: DetectedParameter(
                find_value=self.EP_GUID,
                description="SQL Database SQL Endpoint Item ID",
                source_files={"n1.Notebook/notebook-content.py"},
            )
        }
        return d

    def test_resolves_literal_from_target_workspace(self, tmp_path: Path, monkeypatch):
        from fabric_deploy import params as params_mod

        d = self._detector_with_binding(tmp_path)

        def fake_find(workspace_id, item_type, name):
            assert (workspace_id, item_type, name) == (self.WS, "SQLEndpoint", "mydb")
            return {"id": self.TARGET_EP, "displayName": name, "type": item_type}

        monkeypatch.setattr("fabric_deploy.client.find_item_by_name", fake_find)
        count = params_mod.resolve_warehouse_endpoint_params(d, self.WS, LOG)

        assert count == 1
        assert d.parameters[self.EP_GUID].dynamic_variable == self.TARGET_EP
        assert d.unresolved_warehouse_bindings == []

    def test_manual_override_wins(self, tmp_path: Path, monkeypatch):
        from fabric_deploy import params as params_mod

        d = self._detector_with_binding(tmp_path)
        d.parameters[self.EP_GUID].dynamic_variable = "manually-set"

        def boom(*a, **k):
            raise AssertionError("should not hit the API for resolved params")

        monkeypatch.setattr("fabric_deploy.client.find_item_by_name", boom)
        assert params_mod.resolve_warehouse_endpoint_params(d, self.WS, LOG) == 0
        assert d.parameters[self.EP_GUID].dynamic_variable == "manually-set"

    def test_lookup_failure_records_unresolved(self, tmp_path: Path, monkeypatch):
        from fabric_deploy import params as params_mod

        d = self._detector_with_binding(tmp_path)

        def fail(*a, **k):
            raise ValueError("No SQLEndpoint matching 'mydb' found")

        monkeypatch.setattr("fabric_deploy.client.find_item_by_name", fail)
        assert params_mod.resolve_warehouse_endpoint_params(d, self.WS, LOG) == 0
        assert d.parameters[self.EP_GUID].dynamic_variable == ""
        assert len(d.unresolved_warehouse_bindings) == 1
        assert d.unresolved_warehouse_bindings[0][1] == self.EP_GUID
