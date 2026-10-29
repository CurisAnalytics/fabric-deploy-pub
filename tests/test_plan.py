"""fabdep plan / deploy --dry-run: what a deploy would do, worked out with read calls only.

The Fabric REST API is faked at the transport: it serves reads and fails the
test on any request that could change something, as do fabric-cicd's publish
and unpublish entry points.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from fabric_deploy.cli import main
from fabric_deploy.plan import Plan, format_plan, plan_deploy, plan_json
from fabric_deploy.rls import MEMBERS_NOTE
from fabric_deploy.workspace import WorkspaceDeployer

from .conftest import FAKE_CAPACITY_ID, SAMPLE_DEV_WORKSPACE_ID, SAMPLE_LAKEHOUSE_ID, make_item

TARGET_ID = "00000000-0000-0000-0000-000000000001"
MODEL_ID = "00000000-0000-0000-0000-0000000000b0"
ROLE = "role Reader\n\tmodelPermission: read\n"


class FakeFabric:
    """The Fabric REST API, as fabdep reads it.

    `items` maps a workspace id to its items, `definitions` a semantic model id
    to its definition parts. Any request other than a GET, or a getDefinition
    (a POST that only reads), fails the test.
    """

    def __init__(self) -> None:
        self.workspaces = [
            {"id": TARGET_ID, "displayName": "Fabdep Test [MAIN]"},
            # The sample repo's content was exported from it.
            {"id": SAMPLE_DEV_WORKSPACE_ID, "displayName": "Fabdep Test [DEV]"},
        ]
        self.items: dict[str, list[dict[str, str]]] = {
            TARGET_ID: [
                _item("fabdep_lakehouse", "Lakehouse"),
                _item("fabdep_lakehouse", "SQLEndpoint"),  # never unpublished
                {**_item("SQL DB", "SemanticModel"), "id": MODEL_ID},
                _item("Old Report", "Report"),
                _item("old_lh", "Lakehouse"),
            ]
        }
        self.definitions: dict[str, dict[str, str]] = {MODEL_ID: {"definition/model.tmdl": ""}}
        self.capacities = [
            {"id": FAKE_CAPACITY_ID, "displayName": "F2", "sku": "F2", "state": "Active"}
        ]
        self.requests: list[str] = []

    def send(self, req: Any, _context: str, _idempotent: bool) -> tuple[int, dict[str, str], str]:
        method, url = req.get_method(), urlsplit(req.full_url)
        parts = url.path.strip("/").split("/")
        self.requests.append(f"{method} {url.path}")
        if method == "POST" and parts[-1] == "getDefinition":
            definition = self.definitions[parts[-2]]
            encoded = [
                {"path": p, "payload": base64.b64encode(t.encode()).decode()}
                for p, t in definition.items()
            ]
            return 200, {}, json.dumps({"definition": {"parts": encoded}})
        if method != "GET":
            pytest.fail(f"the plan sent {method} {req.full_url}")
        if parts == ["v1", "workspaces"]:
            return _page(self.workspaces)
        if parts == ["v1", "capacities"]:
            return _page(self.capacities)
        if parts[:2] == ["v1", "workspaces"] and parts[3:] == ["items"]:
            wanted = parse_qs(url.query).get("type")
            items = self.items.get(parts[2], [])
            return _page([i for i in items if not wanted or i["type"] in wanted])
        pytest.fail(f"unexpected request: GET {req.full_url}")


def _item(name: str, item_type: str) -> dict[str, str]:
    return {"id": f"id-{item_type}-{name}", "displayName": name, "type": item_type}


def _page(values: list[Any]) -> tuple[int, dict[str, str], str]:
    return 200, {}, json.dumps({"value": values})


@pytest.fixture
def fabric(monkeypatch, credential) -> FakeFabric:
    """The fake API; fabric-cicd's deploy entry points fail if called."""
    fake = FakeFabric()
    monkeypatch.setattr("fabric_deploy.transport._send", fake.send)
    for name in ("FabricWorkspace", "publish_all_items", "unpublish_all_orphan_items"):
        monkeypatch.setattr(
            f"fabric_deploy.workspace.{name}",
            lambda *_a, _n=name, **_k: pytest.fail(f"the plan called fabric-cicd's {_n}"),
        )
    return fake


def _plan(repo: Path, **options: Any) -> Plan:
    return plan_deploy(repo, branch_override=options.pop("branch", "MAIN"), **options)


def _actions(plan: Plan) -> list[tuple[str, str, str]]:
    return [(i.action, i.type, i.name) for i in plan.items]


class TestTarget:
    def test_existing_workspace(self, sample_ws, fabric):
        target = _plan(sample_ws).target

        assert (target.branch, target.environment) == ("MAIN", "MAIN")
        assert (target.workspace, target.workspace_id) == ("Fabdep Test [MAIN]", TARGET_ID)
        assert (target.create, target.capacity_id) == (False, None)
        assert target.credential == "cli (default)"
        assert target.tenant is None

    def test_pinned_tenant_and_environment(self, sample_ws, fabric, credential):
        tenant = credential.claims["tid"]
        (sample_ws / "fabdep.yml").write_text(
            f'workspace_base_name: "Fabdep Test"\ntenant: "{tenant}"\n'
            "environments:\n  MAIN:\n    environment: PROD\n"
        )
        target = _plan(sample_ws).target
        assert (target.tenant, target.environment) == (tenant, "PROD")

    def test_explicit_environment(self, sample_ws, fabric):
        assert _plan(sample_ws, environment="QA").target.environment == "QA"

    @pytest.mark.parametrize(
        ("config", "new_workspace"),
        [
            pytest.param("", True, id="-N"),
            pytest.param("environments:\n  MAIN:\n    create: true\n", False, id="create-entry"),
        ],
    )
    def test_missing_workspace_would_be_created(self, sample_ws, fabric, config, new_workspace):
        (sample_ws / "fabdep.yml").write_text('workspace_base_name: "Fabdep Test"\n' + config)
        fabric.workspaces = []

        plan = _plan(sample_ws, new_workspace=new_workspace)

        assert (plan.target.create, plan.target.workspace_id) == (True, None)
        assert plan.target.capacity_id == FAKE_CAPACITY_ID
        assert {action for action, _, _ in _actions(plan)} == {"create"}
        assert [m.status for m in plan.guards["rls"].models] == ["new"]
        assert not any("/items" in r or "getDefinition" in r for r in fabric.requests)

    @pytest.mark.parametrize(
        ("config", "new_workspace"),
        [
            pytest.param("", True, id="-N"),
            pytest.param("environments:\n  MAIN:\n    create: true\n", False, id="create-entry"),
        ],
    )
    def test_pr_that_touched_no_item_creates_no_workspace(
        self, sample_ws, fabric, monkeypatch, config, new_workspace
    ):
        """The deploy stops before creating the workspace, so the plan says it wouldn't."""
        (sample_ws / "fabdep.yml").write_text('workspace_base_name: "Fabdep Test"\n' + config)
        fabric.workspaces = []
        monkeypatch.setattr(
            "fabric_deploy.workspace.changed_paths_in", lambda _r, _c: ["README.md"]
        )

        plan = _plan(sample_ws, new_workspace=new_workspace, pr="abc1234")

        target = plan.target
        assert (target.create, target.workspace_id, target.capacity_id) == (False, None, None)
        assert plan.items == ()
        text = format_plan(plan)
        assert "would be created" not in text
        assert "Fabdep Test [MAIN] (does not exist; nothing to deploy, so not created)" in text
        assert plan_json(plan)["target"]["create"] is False

    def test_new_workspace_flag_refuses_an_existing_one(self, sample_ws, fabric):
        with pytest.raises(ValueError, match="remove the -N flag"):
            _plan(sample_ws, new_workspace=True)

    def test_unknown_workspace_fails(self, sample_ws, fabric):
        fabric.workspaces = []
        with pytest.raises(ValueError, match="Workspace not found"):
            _plan(sample_ws)


class TestItems:
    def test_create_update_and_unpublish(self, sample_ws, fabric):
        plan = _plan(sample_ws)

        assert _actions(plan) == [
            ("update", "Lakehouse", "fabdep_lakehouse"),
            ("create", "Notebook", "Fill Lakehouse"),
            ("update", "SemanticModel", "SQL DB"),
            ("unpublish", "Report", "Old Report"),
        ]
        assert plan.cleanup is None

    def test_orphans_fabric_cicd_keeps_are_noted_not_unpublished(self, sample_ws, fabric):
        """fabric-cicd unpublishes Lakehouse items only with a feature flag."""
        warnings = _plan(sample_ws).warnings
        assert any("old_lh" in w and "enable_lakehouse_unpublish" in w for w in warnings)

    @pytest.mark.parametrize(
        ("options", "cleanup"),
        [
            ({"no_cleanup": True}, "--no-cleanup"),
            ({"include_items": ["SQL DB.SemanticModel"]}, "--include-items"),
            ({"pr": "abc1234"}, "--pr"),
        ],
    )
    def test_no_orphans_without_cleanup(self, sample_ws, fabric, monkeypatch, options, cleanup):
        monkeypatch.setattr(
            "fabric_deploy.workspace.changed_paths_in",
            lambda _repo, _commit: ["SQL DB.SemanticModel/definition/model.tmdl"],
        )
        plan = _plan(sample_ws, **options)

        assert plan.cleanup == cleanup
        assert "unpublish" not in {action for action, _, _ in _actions(plan)}

    def test_pr_that_touched_no_item_plans_nothing(self, sample_ws, fabric, monkeypatch):
        """The deploy stops before its guards and parameter rules, so the plan has
        none: a stale warehouse attachment --fail-on-stale blocks on doesn't block it."""
        make_item(
            sample_ws,
            "Query",
            "Notebook",
            files={
                "notebook-content.sql": "-- META "
                + json.dumps({"dependencies": {"warehouse": {"default_warehouse": MODEL_ID}}})
            },
        )
        monkeypatch.setattr(
            "fabric_deploy.workspace.changed_paths_in", lambda _r, _c: ["README.md"]
        )
        plan = _plan(sample_ws, pr="abc1234", fail_on_stale=True)

        assert (plan.items, plan.parameters, plan.cleanup) == ((), (), "--pr")
        assert {name: v.status for name, v in plan.guards.items()} == {
            "fail_on_stale": "pass",
            "unbound_udf": "pass",
            "rls": "pass",
        }
        assert any("nothing to deploy" in w for w in plan.warnings)
        assert _plan(sample_ws, fail_on_stale=True).guards["fail_on_stale"].status == "block"

    def test_scope_without_items_fails_like_the_deploy(self, sample_ws, fabric):
        """The deploy would publish nothing, then unpublish the target's reports."""
        fabric.items[TARGET_ID].append(_item("Sales Report", "Report"))
        with pytest.raises(RuntimeError, match="Nothing to deploy.*type Report"):
            _plan(sample_ws, item_types=["Report"])

    def test_type_scope_limits_publishing_and_cleanup(self, sample_ws, fabric):
        fabric.items[TARGET_ID].append(_item("Old Notebook", "Notebook"))

        plan = _plan(sample_ws, item_types=["Notebook"])

        assert _actions(plan) == [
            ("create", "Notebook", "Fill Lakehouse"),
            ("unpublish", "Notebook", "Old Notebook"),
        ]


class TestParameters:
    def test_detected_rules_with_the_items_they_touch(self, sample_ws, fabric):
        rules = {r.find_value: r for r in _plan(sample_ws).parameters}

        lakehouse = rules[SAMPLE_LAKEHOUSE_ID]
        assert lakehouse.replace_value == "$items.Lakehouse.fabdep_lakehouse.$id"
        assert (lakehouse.origin, lakehouse.kind) == ("detected", "Lakehouse ID")
        assert lakehouse.items == ("Fill Lakehouse.Notebook",)
        assert rules[SAMPLE_DEV_WORKSPACE_ID].replace_value == "$workspace.$id"

    def test_user_file_and_manual_parameters(self, sample_ws, fabric):
        (sample_ws / "fabdep.yml").write_text(
            'workspace_base_name: "Fabdep Test"\n'
            "manual_parameters:\n"
            f'  - find_value: "{SAMPLE_DEV_WORKSPACE_ID}"\n'
            '    replace_value: "manual-ws"\n'
        )
        (sample_ws / "parameter.yml").write_text(
            "find_replace:\n"
            f'  - find_value: "{SAMPLE_LAKEHOUSE_ID}"\n'
            "    replace_value:\n"
            '      MAIN: "user-lakehouse"\n'
            '  - find_value: "synapse_pyspark"\n'
            "    replace_value:\n"
            '      PROD: "prod-only"\n'
        )

        rules = [(r.find_value, r.replace_value, r.origin) for r in _plan(sample_ws).parameters]

        assert rules == [
            (SAMPLE_LAKEHOUSE_ID, "user-lakehouse", "parameter.yml"),
            ("synapse_pyspark", None, "parameter.yml"),  # no value for MAIN: not applied
            (SAMPLE_DEV_WORKSPACE_ID, "manual-ws", "fabdep.yml"),
        ]

    def test_skip_lakehouse_drops_the_lakehouse_rules(self, sample_ws, fabric):
        plan = _plan(sample_ws, skip_lakehouse=True)
        assert [r.find_value for r in plan.parameters] == [SAMPLE_DEV_WORKSPACE_ID]


class TestGuards:
    def test_all_pass(self, sample_ws, fabric):
        plan = _plan(sample_ws)

        assert {name: v.status for name, v in plan.guards.items()} == {
            "fail_on_stale": "pass",
            "unbound_udf": "pass",
            "rls": "pass",
        }
        assert [(m.model, m.status) for m in plan.guards["rls"].models] == [("SQL DB", "unchanged")]
        assert not plan.blocked

    def test_rls_change_blocks(self, sample_ws, fabric):
        fabric.definitions[MODEL_ID]["definition/roles/Reader.tmdl"] = ROLE

        plan = _plan(sample_ws)

        rls = plan.guards["rls"]
        assert rls.status == "block"
        assert "Deploy blocked" in rls.message
        assert rls.models[0].diff is not None
        assert rls.models[0].diff.only_remote == ["Reader"]
        assert plan.blocked

    def test_bim_model_is_not_compared(self, sample_ws, fabric):
        make_item(sample_ws, "Bim Model", "SemanticModel", files={"model.bim": "{}"})

        plan = _plan(sample_ws)

        rls = plan.guards["rls"]
        assert rls.status == "warn"
        assert [(m.model, m.status) for m in rls.models] == [
            ("Bim Model", "unsupported"),
            ("SQL DB", "unchanged"),
        ]
        assert "Bim Model: not compared (model.bim)" in format_plan(plan)

    def test_allow_rls_change_skips_the_read(self, sample_ws, fabric):
        plan = _plan(sample_ws, allow_rls_change=True)

        assert plan.guards["rls"].status == "warn"
        assert [m.status for m in plan.guards["rls"].models] == ["skipped"]
        assert not any("getDefinition" in r for r in fabric.requests)


class TestWarnings:
    def test_collected_into_the_plan_not_printed(self, sample_ws, fabric, capsys):
        (sample_ws / "fabdep.yml").write_text(
            'workspace_base_name: "Fabdep Test"\n'
            'manual_parameters:\n  - find_value: "nowhere"\n    replace_value: "x"\n'
        )
        plan = _plan(sample_ws)

        assert any("Manual parameter 'nowhere'" in w for w in plan.warnings)
        assert "nowhere" not in capsys.readouterr().err

    def test_fabdep_yml_warnings_are_in_the_plan(self, sample_ws, fabric, capsys):
        (sample_ws / "fabdep.yml").write_text('workspace_base_name: "Fabdep Test"\ncapacity: F64\n')

        warnings = plan_json(_plan(sample_ws))["warnings"]

        assert any("unknown key 'capacity'" in w for w in warnings)
        assert "capacity" not in capsys.readouterr().err

    def test_printed_when_the_plan_fails(self, sample_ws, fabric, capsys):
        (sample_ws / "fabdep.yml").write_text(
            'workspace_base_name: "Fabdep Test"\n'
            'manual_parameters:\n  - find_value: "nowhere"\n    replace_value: "x"\n'
        )
        with pytest.raises(RuntimeError, match="matched no items"):
            _plan(sample_ws, include_items=["Ghost.Notebook"])
        assert "Manual parameter 'nowhere'" in capsys.readouterr().err


def test_a_dry_run_deployer_does_not_deploy(sample_ws, fabric):
    deployer = WorkspaceDeployer(sample_ws, branch_override="MAIN", dry_run=True)
    with pytest.raises(RuntimeError, match="dry run"):
        deployer.deploy()


class TestOutput:
    def test_json_shape(self, sample_ws, fabric):
        document = plan_json(_plan(sample_ws))

        assert list(document) == [
            "target",
            "items",
            "cleanup",
            "parameters",
            "guards",
            "warnings",
            "blocked",
        ]
        assert document["target"] == {
            "branch": "MAIN",
            "workspace": "Fabdep Test [MAIN]",
            "workspace_id": TARGET_ID,
            "create": False,
            "capacity_id": None,
            "environment": "MAIN",
            "credential": "cli (default)",
            "tenant": None,
        }
        assert document["items"][0] == {
            "name": "fabdep_lakehouse",
            "type": "Lakehouse",
            "action": "update",
        }
        assert document["cleanup"] == {"runs": True, "reason": None}
        assert document["parameters"][0] == {
            "find_value": SAMPLE_LAKEHOUSE_ID,
            "replace_value": "$items.Lakehouse.fabdep_lakehouse.$id",
            "origin": "detected",
            "kind": "Lakehouse ID",
            "items": ["Fill Lakehouse.Notebook"],
        }
        assert document["guards"]["fail_on_stale"] == {
            "verdict": "pass",
            "message": "",
            "findings": [],
        }
        assert document["guards"]["rls"]["models"] == [
            {
                "model": "SQL DB",
                "status": "unchanged",
                "adds": [],
                "removes": [],
                "changes": [],
                "recreates": [],
            }
        ]
        assert document["blocked"] is False
        json.dumps(document)  # serializable as is

    def test_text(self, sample_ws, fabric):
        fabric.definitions[MODEL_ID]["definition/roles/Reader.tmdl"] = ROLE
        text = format_plan(_plan(sample_ws))

        lines = text.splitlines()
        assert f"Workspace    Fabdep Test [MAIN] ({TARGET_ID})" in lines
        assert "  update     Lakehouse      fabdep_lakehouse" in lines
        assert "  unpublish  Report         Old Report" in lines
        assert f"  {SAMPLE_LAKEHOUSE_ID} -> $items.Lakehouse.fabdep_lakehouse.$id" in lines
        assert "      SQL DB: changed (removes Reader)" in lines
        assert MEMBERS_NOTE.split(".")[0] in text
        assert lines[-1].startswith("Blocked")
        assert "\033[" not in text

    def test_text_in_colour(self, sample_ws, fabric):
        assert "\033[" in format_plan(_plan(sample_ws), color=True)


class TestCommand:
    @staticmethod
    def _main(monkeypatch, *argv: str) -> int:
        monkeypatch.setattr(sys, "argv", ["fabdep", *argv])
        with pytest.raises(SystemExit) as exc:
            main()
            raise SystemExit(0)
        return int(exc.value.code or 0)

    @pytest.mark.parametrize("command", [["plan"], ["deploy", "--dry-run"], ["--dry-run"]])
    def test_json_is_all_stdout_holds(self, monkeypatch, capsys, sample_ws, fabric, command):
        code = self._main(monkeypatch, *command, str(sample_ws), "-b", "MAIN", "--output", "json")

        out = capsys.readouterr().out
        assert code == 0
        assert json.loads(out)["target"]["workspace_id"] == TARGET_ID

    def test_text_by_default(self, monkeypatch, capsys, sample_ws, fabric):
        assert self._main(monkeypatch, "plan", str(sample_ws), "-b", "MAIN") == 0
        assert "Fabdep Test [MAIN]" in capsys.readouterr().out

    def test_a_blocking_guard_exits_3(self, monkeypatch, capsys, sample_ws, fabric):
        fabric.definitions[MODEL_ID]["definition/roles/Reader.tmdl"] = ROLE

        code = self._main(monkeypatch, "plan", str(sample_ws), "-b", "MAIN", "--output", "json")

        assert code == 3
        assert json.loads(capsys.readouterr().out)["blocked"] is True

    def test_an_error_exits_1(self, monkeypatch, capsys, sample_ws, fabric):
        fabric.workspaces = []
        assert (
            self._main(monkeypatch, "plan", str(sample_ws), "-b", "MAIN", "--output", "json") == 1
        )
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Workspace not found" in captured.err

    def test_output_needs_dry_run(self, monkeypatch, capsys, sample_ws):
        assert self._main(monkeypatch, "deploy", str(sample_ws), "--output", "json") == 2
        assert "--dry-run" in capsys.readouterr().err

    def test_plan_takes_the_deploy_options(self, monkeypatch, capsys):
        with pytest.raises(SystemExit):
            monkeypatch.setattr(sys, "argv", ["fabdep", "plan", "-h"])
            main()
        out = capsys.readouterr().out
        for flag in (
            "--branch",
            "--include-items",
            "--item-types",
            "--exclude-types",
            "--pr",
            "--no-cleanup",
            "--new-workspace",
            "--skip-lakehouse",
            "--auth",
            "--allow-rls-change",
            "--allow-unbound-udf",
            "--fail-on-stale",
            "--output",
        ):
            assert flag in out
        assert "--frame" not in out


def test_only_reads(sample_ws, fabric):
    """Every request the plan sends is a GET, or a getDefinition (a POST that reads)."""
    fabric.definitions[MODEL_ID]["definition/roles/Reader.tmdl"] = ROLE
    _ = _plan(sample_ws, fail_on_stale=True)

    writes = [r for r in fabric.requests if not r.startswith("GET ")]
    assert writes == [f"POST /v1/workspaces/{TARGET_ID}/semanticModels/{MODEL_ID}/getDefinition"]


def test_sample_repo_items_are_untouched(sample_ws, fabric):
    before = sorted((p, p.read_bytes()) for p in sample_ws.rglob("*") if p.is_file())
    _ = _plan(sample_ws)
    after = sorted((p, p.read_bytes()) for p in sample_ws.rglob("*") if p.is_file())
    assert before == after


def test_a_rules_items_are_the_published_items_it_matches(tmp_path: Path, fabric):
    """Literal or regex, within the rule's item_type filter, whatever the folder names."""
    (tmp_path / "fabdep.yml").write_text('workspace_base_name: "Fabdep Test"\n')
    (tmp_path / "parameter.yml").write_text(
        'find_replace:\n  - find_value: "needle"\n    replace_value:\n      _ALL_: "x"\n'
        '  - find_value: "ne+dle"\n    is_regex: "true"\n    item_type: "Notebook"\n'
        '    replace_value:\n      _ALL_: "y"\n'
    )
    make_item(tmp_path, "a", "Notebook", folder="nb", files={"notebook-content.py": "needle"})
    make_item(tmp_path, "b", "DataPipeline", files={"pipeline-content.json": {"x": "needle"}})
    make_item(tmp_path, "c", "Notebook", files={"notebook-content.py": "haystack"})

    rules = _plan(tmp_path).parameters

    assert [(r.find_value, r.items) for r in rules] == [
        ("needle", ("a.Notebook", "b.DataPipeline")),
        ("ne+dle", ("a.Notebook",)),
    ]
