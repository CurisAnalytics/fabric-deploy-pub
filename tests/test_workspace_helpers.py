"""Unit tests for scope.resolve_item_scope and the params target-workspace resolvers.

Pure functions (no deployer state), so the tests call them directly with a
plain logger / inputs.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fabric_deploy.detect import DetectedParameter, ParameterDetector
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
        assert resolve_item_scope(["Notebook"], ["Notebook", "Report"], None, LOG) == ["Notebook"]

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
        scope = resolve_item_scope(None, None, ["Foo.Notebook", "PlainName", "Bar.NotAType"], LOG)
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
        assert validate_include_items(["foo.Notebook"], tmp_path, LOG) == ["foo.Notebook"]

    def test_unaccepted_platform_type_is_ignored(self, tmp_path: Path):
        _make_item(tmp_path, "foo.Notebook", "foo", "NotARealType")
        assert repo_item_types(tmp_path) == set()
        assert _repo_items(tmp_path) == {}


# --- resolve_warehouse_endpoint_params -------------------------------------


class TestResolveWarehouseEndpointParams:
    EP_GUID = "66666666-6666-6666-6666-666666666666"
    TARGET_EP = "77777777-7777-7777-7777-777777777777"
    WS = "00000000-0000-0000-0000-000000000001"

    def _detector_with_binding(self, tmp_path: Path) -> ParameterDetector:
        d = ParameterDetector(tmp_path)
        d._names["SQLDatabase"] = ["mydb"]
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

    def test_several_sql_databases_are_not_guessed_between(self, tmp_path: Path, monkeypatch):
        from fabric_deploy import params as params_mod

        d = self._detector_with_binding(tmp_path)
        d._names["SQLDatabase"] = ["db1", "db2"]

        def boom(*a, **k):
            raise AssertionError("should not pick one of several databases")

        monkeypatch.setattr("fabric_deploy.client.find_item_by_name", boom)
        assert params_mod.resolve_warehouse_endpoint_params(d, self.WS, LOG) == 0
        assert [b[1] for b in d.unresolved_warehouse_bindings] == [self.EP_GUID]

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


# --- resolve_sqldatabase_hostname_params ------------------------------------


class TestResolveSqlDatabaseHostnameParams:
    HOST = "abc123-def456.database.fabric.microsoft.com"
    WS = "00000000-0000-0000-0000-000000000001"

    def _detector_with_hostname(self, tmp_path: Path) -> ParameterDetector:
        d = ParameterDetector(tmp_path)
        d._names["SQLDatabase"] = ["mydb"]
        d.parameters = {
            self.HOST: DetectedParameter(
                find_value=self.HOST,
                description="SQL Database Connection String",
                dynamic_variable="$items.SQLDatabase.mydb.$sqlendpoint",
                source_files={"m1.SemanticModel/definition/tables/t.tmdl"},
            )
        }
        return d

    def test_strips_port_suffix_from_server_fqdn(self, tmp_path: Path, monkeypatch):
        from fabric_deploy import params as params_mod

        d = self._detector_with_hostname(tmp_path)

        def fake_host(workspace_id, name):
            assert (workspace_id, name) == (self.WS, "mydb")
            # client strips the ",1433" the Fabric API appends to serverFqdn
            return "target-host.database.fabric.microsoft.com"

        monkeypatch.setattr("fabric_deploy.client.get_sqldatabase_server_host", fake_host)
        count = params_mod.resolve_sqldatabase_hostname_params(d, self.WS, LOG)

        assert count == 1
        assert (
            d.parameters[self.HOST].dynamic_variable == "target-host.database.fabric.microsoft.com"
        )

    def test_manual_override_wins(self, tmp_path: Path, monkeypatch):
        from fabric_deploy import params as params_mod

        d = self._detector_with_hostname(tmp_path)
        d.parameters[self.HOST].dynamic_variable = "manually-set-host"

        def boom(*a, **k):
            raise AssertionError("should not hit the API for resolved params")

        monkeypatch.setattr("fabric_deploy.client.get_sqldatabase_server_host", boom)
        assert params_mod.resolve_sqldatabase_hostname_params(d, self.WS, LOG) == 0
        assert d.parameters[self.HOST].dynamic_variable == "manually-set-host"

    def test_lookup_failure_keeps_dynamic_fallback(self, tmp_path: Path, monkeypatch):
        from fabric_deploy import params as params_mod

        d = self._detector_with_hostname(tmp_path)

        def fail(*a, **k):
            raise ValueError("No SQLDatabase matching 'mydb' found")

        monkeypatch.setattr("fabric_deploy.client.get_sqldatabase_server_host", fail)
        assert params_mod.resolve_sqldatabase_hostname_params(d, self.WS, LOG) == 0
        assert d.parameters[self.HOST].dynamic_variable == "$items.SQLDatabase.mydb.$sqlendpoint"

    def test_no_sqldatabase_params_is_noop(self, tmp_path: Path, monkeypatch):
        from fabric_deploy import params as params_mod

        d = ParameterDetector(tmp_path)
        d._names["SQLDatabase"] = ["mydb"]
        d.parameters = {
            "some-host": DetectedParameter(
                find_value="some-host",
                description="SQL Analytics Endpoint Connection String",
                dynamic_variable="$items.Lakehouse.lh.$sqlendpoint",
            )
        }

        def boom(*a, **k):
            raise AssertionError("should not hit the API without SQLDatabase params")

        monkeypatch.setattr("fabric_deploy.client.get_sqldatabase_server_host", boom)
        assert params_mod.resolve_sqldatabase_hostname_params(d, self.WS, LOG) == 0
