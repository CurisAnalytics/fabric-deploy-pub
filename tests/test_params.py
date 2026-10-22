"""params.py: fabdep.yml manual_parameters and the replacements resolved from the target.

Overrides keep an auto-detected `find_value` while supplying a custom
`replace_value` (e.g. for external references), optionally scoped to item
types. Injections cover find_values detection can't see (e.g. a PowerApp app
id in report JSON) — honored only when the value literally occurs in an item
file, so a typo'd or stale entry still warns instead of silently no-oping.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from fabric_deploy import params
from fabric_deploy.detect import DetectedParameter, Kind, ParameterDetector
from fabric_deploy.workspace import WorkspaceDeployer

from .conftest import make_item

DETECTED_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
UNRELATED_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
NOTEBOOK = "# META " + json.dumps(
    {
        "dependencies": {
            "lakehouse": {
                "default_lakehouse": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "default_lakehouse_name": "lh",
                "default_lakehouse_workspace_id": DETECTED_UUID,
            }
        }
    }
)
LOG = logging.getLogger("test.params")


def _seed(repo: Path, entries: str, notebook_code: str = "") -> None:
    """A lakehouse `lh`, a notebook `n1` whose metadata makes DETECTED_UUID a
    detected workspace id, and fabdep.yml with these manual_parameters entries."""
    make_item(repo, "lh", "Lakehouse")
    make_item(repo, "n1", "Notebook", files={"notebook-content.py": f"{NOTEBOOK}\n{notebook_code}"})
    (repo / "fabdep.yml").write_text('workspace_base_name: "ws"\nmanual_parameters:\n' + entries)


@pytest.mark.usefixtures("mock_fabric_api")
class TestManualParameters:
    def test_override_replaces_the_detected_rule(self, tmp_path: Path):
        _seed(
            tmp_path,
            f'  - find_value: "{DETECTED_UUID}"\n'
            '    replace_value: "kept-as-external"\n'
            '    description: "external workspace"\n',
        )

        d = WorkspaceDeployer(tmp_path, branch_override="dev")

        param = d._detector.parameters[DETECTED_UUID]
        assert param.dynamic_variable == "kept-as-external"
        assert "fabdep.yml" in param.source_files
        assert d._manual_count == 1

    @pytest.mark.parametrize("find_value", [UNRELATED_UUID, "only-lives-in-fabdep-yml"])
    def test_value_in_no_item_file_warns_and_is_skipped(self, tmp_path: Path, caplog, find_value):
        """A typo or a stale entry; fabdep.yml itself is not an item file."""
        _seed(tmp_path, f'  - find_value: "{find_value}"\n    replace_value: "anything"\n')

        with caplog.at_level("WARNING"):
            d = WorkspaceDeployer(tmp_path, branch_override="dev")

        assert find_value not in d._detector.parameters
        assert d._manual_count == 0
        assert find_value in caplog.text

    def test_undetected_value_in_item_file_is_injected(self, tmp_path: Path):
        _seed(
            tmp_path,
            f'  - find_value: "{UNRELATED_UUID}"\n'
            '    replace_value: "new-app-guid"\n'
            '    item_type: ["Report"]\n'
            '    description: "PowerApp link"\n',
        )
        visual = {"appId": f"/providers/Microsoft.PowerApps/apps/{UNRELATED_UUID}"}
        make_item(tmp_path, "r1", "Report", files={"definition/pages/p1/visual.json": visual})

        d = WorkspaceDeployer(tmp_path, branch_override="dev")

        param = d._detector.parameters[UNRELATED_UUID]
        assert param.dynamic_variable == "new-app-guid"
        assert param.item_types == {"Report"}
        assert param.description == "PowerApp link"
        assert param.source_files == {"r1.Report/definition/pages/p1/visual.json", "fabdep.yml"}
        assert d._manual_count == 1
        entry = next(
            e
            for e in d._detector.generate_parameter_yml()["find_replace"]
            if e["find_value"] == UNRELATED_UUID
        )
        assert entry["item_type"] == ["Report"]
        assert entry["replace_value"] == {"_ALL_": "new-app-guid"}

    @pytest.mark.parametrize(
        ("item_type_yaml", "expected"),
        [('["SemanticModel", "Report"]', {"SemanticModel", "Report"}), ('"Report"', {"Report"})],
    )
    def test_item_type_is_a_string_or_a_list(self, tmp_path: Path, item_type_yaml, expected):
        _seed(
            tmp_path,
            f'  - find_value: "{DETECTED_UUID}"\n'
            '    replace_value: "x"\n'
            f"    item_type: {item_type_yaml}\n",
        )
        d = WorkspaceDeployer(tmp_path, branch_override="dev")
        assert d._detector.parameters[DETECTED_UUID].item_types == expected

    @pytest.mark.parametrize(
        ("branch", "replace_value", "expected"),
        [
            ("dev", '      dev: "dev-value"\n      prod: "prod-value"\n', "dev-value"),
            ("prod", '      dev: "dev-value"\n      prod: "prod-value"\n', "prod-value"),
            ("test", '      _ALL_: "any-value"\n', "any-value"),
        ],
    )
    def test_environment_keyed_replace_value(
        self, tmp_path: Path, branch: str, replace_value: str, expected: str
    ):
        """A mapping picks the deploy environment's value; `_ALL_` applies to any."""
        _seed(
            tmp_path,
            f'  - find_value: "{DETECTED_UUID}"\n    replace_value:\n{replace_value}',
        )
        d = WorkspaceDeployer(tmp_path, branch_override=branch)
        assert d.environment == branch
        assert d._detector.parameters[DETECTED_UUID].dynamic_variable == expected

    def test_no_value_for_the_environment_leaves_the_rule_alone(self, tmp_path: Path):
        """An override keeps the detected rule; an injection is skipped."""
        entries = (
            f'  - find_value: ["{DETECTED_UUID}", "{UNRELATED_UUID}"]\n'
            '    replace_value:\n      prod: "prod-value"\n'
        )
        _seed(tmp_path, entries, notebook_code=f"x = '{UNRELATED_UUID}'\n")

        d = WorkspaceDeployer(tmp_path, branch_override="dev")

        assert d._detector.parameters[DETECTED_UUID].dynamic_variable == "$workspace.$id"
        assert UNRELATED_UUID not in d._detector.parameters
        assert d._manual_count == 0

    def test_explicit_environment_overrides_the_branch(self, tmp_path: Path):
        _seed(
            tmp_path,
            f'  - find_value: "{DETECTED_UUID}"\n'
            '    replace_value:\n      Production: "prod-value"\n',
        )
        d = WorkspaceDeployer(tmp_path, branch_override="main", environment="Production")
        assert d._detector.parameters[DETECTED_UUID].dynamic_variable == "prod-value"

    def test_find_value_list_applies_the_entry_to_each(self, tmp_path: Path):
        _seed(
            tmp_path,
            f'  - find_value: ["{DETECTED_UUID}", "{UNRELATED_UUID}"]\n'
            '    replace_value: "shared"\n',
            notebook_code=f"x = '{UNRELATED_UUID}'\n",
        )

        d = WorkspaceDeployer(tmp_path, branch_override="dev")

        assert d._detector.parameters[DETECTED_UUID].dynamic_variable == "shared"
        assert d._detector.parameters[UNRELATED_UUID].dynamic_variable == "shared"
        assert d._manual_count == 2

    @pytest.mark.parametrize(
        ("entry_yaml", "problem"),
        [
            pytest.param('  - "not-a-mapping"\n', "mapping", id="not-a-mapping"),
            pytest.param('  - replace_value: "x"\n', "find_value", id="no-find"),
            pytest.param('  - find_value: 12345\n    replace_value: "x"\n', "find_value", id="int"),
            pytest.param('  - find_value: ""\n    replace_value: "x"\n', "find_value", id="empty"),
            pytest.param(f'  - find_value: "{DETECTED_UUID}"\n', "replace_value", id="no-replace"),
            pytest.param(
                f'  - find_value: "{DETECTED_UUID}"\n    replace_value: ["a"]\n',
                "replace_value",
                id="list-replace",
            ),
            pytest.param(
                f'  - find_value: "{DETECTED_UUID}"\n    replace_value:\n      prod: 5\n',
                "replace_value",
                id="int-env-value",
            ),
            pytest.param(
                f'  - find_value: "{DETECTED_UUID}"\n    replace_value:\n'
                '      _ALL_: "a"\n      prod: "b"\n',
                "_ALL_.*only key",
                id="all-with-an-environment",
            ),
            pytest.param(
                f'  - find_value: "{DETECTED_UUID}"\n    replace_value: "x"\n'
                "    item_type: {a: b}\n",
                "item_type",
                id="mapping-item-type",
            ),
            pytest.param(
                f'  - find_value: "{DETECTED_UUID}"\n    replace_value: "x"\n'
                "    item_types: [Notebook]\n",
                "unknown key 'item_types' — did you mean 'item_type'",
                id="misspelt-key",
            ),
            pytest.param(
                f'  - find_value: "{DETECTED_UUID}"\n    replace_value: "x"\n'
                "    file_path: Report/definition.pbir\n",
                "unknown key 'file_path'",
                id="parameter-yml-only-key",
            ),
            pytest.param(
                f'  - find_value: "{DETECTED_UUID}"\n    replace_value: "x"\n    description: 5\n',
                "description",
                id="int-description",
            ),
        ],
    )
    def test_malformed_entry_fails_naming_the_problem(self, tmp_path: Path, entry_yaml, problem):
        """A malformed entry stops the deploy before anything is published,
        rather than being skipped or crashing on an unhashable value."""
        _seed(
            tmp_path, f'  - find_value: "{DETECTED_UUID}"\n    replace_value: "fine"\n' + entry_yaml
        )

        with pytest.raises(ValueError, match=rf"manual_parameters entry 2: .*{problem}"):
            WorkspaceDeployer(tmp_path, branch_override="dev")


# --- replacements resolved from the target workspace ---------------------------

WORKSPACE = "00000000-0000-0000-0000-000000000001"
ENDPOINT = "66666666-6666-6666-6666-666666666666"
TARGET_ENDPOINT = "77777777-7777-7777-7777-777777777777"
HOST = "abc123-def456.database.fabric.microsoft.com"


def _detector(tmp_path: Path, databases: list[str], parameter: DetectedParameter):
    d = ParameterDetector(tmp_path)
    d._names["SQLDatabase"] = databases
    d.parameters = {parameter.find_value: parameter}
    return d


def _raising(error: Exception):
    def fail(*_args, **_kwargs):
        raise error

    return fail


_no_api = _raising(AssertionError("looked up the target workspace"))


class TestResolveWarehouseEndpointParams:
    """A notebook bound to a SQL database's endpoint gets the target endpoint's id."""

    def _binding(self, tmp_path: Path, databases=("mydb",), dynamic: str = ""):
        return _detector(
            tmp_path,
            list(databases),
            DetectedParameter(
                find_value=ENDPOINT,
                description=Kind.SQLDB_ENDPOINT_ID,
                dynamic_variable=dynamic,
                source_files={"n1.Notebook/notebook-content.py"},
            ),
        )

    def test_resolves_the_target_endpoint_id(self, tmp_path: Path, monkeypatch):
        d = self._binding(tmp_path)

        def find(workspace_id, item_type, name):
            assert (workspace_id, item_type, name) == (WORKSPACE, "SQLEndpoint", "mydb")
            return {"id": TARGET_ENDPOINT, "displayName": name, "type": item_type}

        monkeypatch.setattr("fabric_deploy.client.find_item_by_name", find)

        assert params.resolve_warehouse_endpoint_params(d, WORKSPACE, LOG) == 1
        assert d.parameters[ENDPOINT].dynamic_variable == TARGET_ENDPOINT
        assert d.unresolved_warehouse_bindings == []

    def test_manual_override_wins(self, tmp_path: Path, monkeypatch):
        d = self._binding(tmp_path, dynamic="manually-set")
        monkeypatch.setattr("fabric_deploy.client.find_item_by_name", _no_api)

        assert params.resolve_warehouse_endpoint_params(d, WORKSPACE, LOG) == 0
        assert d.parameters[ENDPOINT].dynamic_variable == "manually-set"

    @pytest.mark.parametrize(
        ("databases", "lookup"),
        [
            pytest.param(["db1", "db2"], _no_api, id="several-databases-not-guessed-between"),
            pytest.param(
                ["mydb"], _raising(ValueError("No SQLEndpoint matching 'mydb'")), id="not-found"
            ),
        ],
    )
    def test_unresolvable_binding_is_recorded(self, tmp_path: Path, monkeypatch, databases, lookup):
        d = self._binding(tmp_path, databases)
        monkeypatch.setattr("fabric_deploy.client.find_item_by_name", lookup)

        assert params.resolve_warehouse_endpoint_params(d, WORKSPACE, LOG) == 0
        assert d.parameters[ENDPOINT].dynamic_variable == ""
        assert [b[1] for b in d.unresolved_warehouse_bindings] == [ENDPOINT]


class TestResolveSqlDatabaseHostnameParams:
    """A SQL database host gets the target's bare host (the client strips the port)."""

    def _host(self, tmp_path: Path, dynamic: str = "$items.SQLDatabase.mydb.$sqlendpoint"):
        return _detector(
            tmp_path,
            ["mydb"],
            DetectedParameter(
                find_value=HOST,
                description=Kind.SQLDB_HOST,
                dynamic_variable=dynamic,
                source_files={"m1.SemanticModel/definition/tables/t.tmdl"},
            ),
        )

    def test_resolves_the_target_host(self, tmp_path: Path, monkeypatch):
        d = self._host(tmp_path)

        def host(workspace_id, name):
            assert (workspace_id, name) == (WORKSPACE, "mydb")
            return "target-host.database.fabric.microsoft.com"

        monkeypatch.setattr("fabric_deploy.client.get_sqldatabase_server_host", host)

        assert params.resolve_sqldatabase_hostname_params(d, WORKSPACE, LOG) == 1
        assert d.parameters[HOST].dynamic_variable == "target-host.database.fabric.microsoft.com"

    @pytest.mark.parametrize(
        "dynamic",
        ["manually-set-host", "$items.Lakehouse.lh.$sqlendpoint"],
        ids=["manual-override", "not-a-sql-database-host"],
    )
    def test_other_rules_are_left_alone(self, tmp_path: Path, monkeypatch, dynamic):
        d = self._host(tmp_path, dynamic)
        monkeypatch.setattr("fabric_deploy.client.get_sqldatabase_server_host", _no_api)

        assert params.resolve_sqldatabase_hostname_params(d, WORKSPACE, LOG) == 0
        assert d.parameters[HOST].dynamic_variable == dynamic

    def test_lookup_failure_keeps_the_dynamic_variable(self, tmp_path: Path, monkeypatch):
        d = self._host(tmp_path)

        monkeypatch.setattr(
            "fabric_deploy.client.get_sqldatabase_server_host",
            _raising(ValueError("No SQLDatabase matching 'mydb' found")),
        )

        assert params.resolve_sqldatabase_hostname_params(d, WORKSPACE, LOG) == 0
        assert d.parameters[HOST].dynamic_variable == "$items.SQLDatabase.mydb.$sqlendpoint"
