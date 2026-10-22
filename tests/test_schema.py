"""fabdep.schema.json: the JSON Schema of fabdep.yml, kept in step with the loader."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from fabric_deploy.cli import main
from fabric_deploy.config import AUTH_MODES, ENVIRONMENT_KEYS, FABDEP_YML_KEYS, FabdepConfig
from fabric_deploy.params import MANUAL_PARAMETER_KEYS, _manual_entries
from fabric_deploy.scaffold import schema_text

SCHEMA = json.loads(schema_text())
VALIDATOR = Draft202012Validator(SCHEMA)
GUID = "00000000-0000-0000-0000-0000000000a1"
BASE = "workspace_base_name: Contoso Sales\n"
ENTRY = f"manual_parameters:\n  - find_value: {GUID}\n    replace_value: {GUID}\n"


def schema_accepts(body: str) -> bool:
    data: object = yaml.safe_load(body)
    return VALIDATOR.is_valid({} if data is None else data)


def loader_accepts(repo: Path, body: str, branch: str = "main") -> bool:
    """Whether fabdep reads `body` as fabdep.yml for `branch`, manual_parameters included."""
    (repo / "fabdep.yml").write_text(body, encoding="utf-8")
    try:
        config = FabdepConfig.from_path(repo, branch_override=branch)
        _ = _manual_entries(config.manual_parameters)
    except ValueError:
        return False
    return True


def test_is_a_valid_draft_2020_12_schema():
    Draft202012Validator.check_schema(SCHEMA)


def test_names_the_keys_the_loader_reads():
    assert set(SCHEMA["properties"]) == FABDEP_YML_KEYS
    assert set(SCHEMA["$defs"]["environment"]["properties"]) == set(ENVIRONMENT_KEYS)
    assert set(SCHEMA["$defs"]["manual_parameter"]["properties"]) == set(MANUAL_PARAMETER_KEYS)
    assert SCHEMA["properties"]["auth"]["enum"] == [*AUTH_MODES, None]


@pytest.mark.parametrize(
    ("body", "valid"),
    [
        pytest.param(BASE, True, id="base"),
        pytest.param("", False, id="empty"),
        pytest.param("workspace_base_name:\n", False, id="null-base"),
        pytest.param("workspace_base_name: ''\n", False, id="empty-base"),
        pytest.param("workspace_base_name: 3\n", False, id="int-base"),
        # workspace_base_name is needed exactly when a template in use has {base}.
        pytest.param('workspace_name_template: "Analytics {BRANCH}"\n', True, id="no-base"),
        pytest.param('workspace_name_template: "{{base}} {branch}"\n', True, id="literal-base"),
        pytest.param('workspace_name_template: "{base} {branch}"\n', False, id="needs-base"),
        pytest.param('workspace_name_template: "{{{base}}}"\n', False, id="braced-base"),
        pytest.param("environments:\n  main:\n    workspace: Sales\n", True, id="named"),
        pytest.param(
            "workspace_base_name:\nenvironments:\n  main:\n    workspace: Sales\n",
            True,
            id="named-null-base",
        ),
        pytest.param("environments:\n  main:\n", False, id="unnamed-entry"),
        pytest.param("environments:\n  main:\n    workspace:\n", False, id="null-workspace"),
        pytest.param("environments: {}\n", False, id="no-entries"),
        pytest.param('environments:\n  main:\n    workspace: "{base}"\n', False, id="entry-base"),
        pytest.param(
            "environments:\n  main:\n    workspace: Sales\n  'feature/*':\n    create: true\n",
            False,
            id="glob-uses-template",
        ),
        pytest.param(
            'workspace_name_template: "Sales {branch}"\n'
            "environments:\n  main:\n    workspace: Sales\n  'feature/*':\n    create: true\n",
            True,
            id="glob-template-without-base",
        ),
        # Templates and environments
        pytest.param(BASE + 'workspace_name_template: "{base} - {branch}"\n', True, id="tmpl"),
        pytest.param(BASE + 'workspace_name_template: "{base} {brnch}"\n', False, id="tmpl-typo"),
        pytest.param(BASE + 'workspace_name_template: "{base} {branch!r}"\n', False, id="tmpl-r"),
        pytest.param(BASE + 'workspace_name_template: "{base"\n', False, id="tmpl-open"),
        pytest.param(BASE + 'workspace_name_template: "{base}}"\n', False, id="tmpl-close"),
        pytest.param(BASE + 'workspace_name_template: "{}"\n', False, id="tmpl-positional"),
        pytest.param(BASE + "workspace_name_template: ''\n", False, id="tmpl-empty"),
        pytest.param(BASE + "workspace_name_template:\n", False, id="tmpl-null"),
        pytest.param(BASE + "workspace_name_template: 3\n", False, id="tmpl-int"),
        pytest.param(
            BASE + "environments:\n  main:\n    workspace: '{base}'\n    environment: PROD\n"
            "  dev:\n  'feature/*':\n    create: true\n  'hotfix-?':\n"
            "    workspace: '{base} - {branch}'\n    environment:\n",
            True,
            id="environments",
        ),
        pytest.param(BASE + "environments:\n", True, id="null-environments"),
        pytest.param(BASE + "environments: [main]\n", False, id="environments-list"),
        pytest.param(BASE + "environments:\n  main: Sales\n", False, id="entry-string"),
        pytest.param(BASE + "environments:\n  main:\n    workpsace: S\n", False, id="entry-typo"),
        pytest.param(BASE + "environments:\n  main:\n    workspace: 3\n", False, id="ws-int"),
        pytest.param(BASE + "environments:\n  main:\n    workspace: ''\n", False, id="ws-empty"),
        pytest.param(BASE + "environments:\n  main:\n    workspace: '{x}'\n", False, id="ws-tmpl"),
        pytest.param(BASE + "environments:\n  main:\n    environment: ''\n", False, id="env-empty"),
        pytest.param(BASE + "environments:\n  main:\n    create: 'yes'\n", False, id="create-str"),
        pytest.param(BASE + "environments:\n  main:\n    create:\n", False, id="create-null"),
        # Identity
        pytest.param(BASE + f"tenant: {GUID}\n", True, id="tenant"),
        pytest.param(BASE + f"tenant: {GUID.upper()}\n", True, id="tenant-upper"),
        pytest.param(BASE + "tenant:\n", True, id="tenant-null"),
        pytest.param(BASE + "tenant: contoso\n", False, id="tenant-alias"),
        pytest.param(BASE + f"tenant: '{{{GUID}}}'\n", False, id="tenant-braced"),
        *(pytest.param(BASE + f"auth: {mode}\n", True, id=f"auth-{mode}") for mode in AUTH_MODES),
        pytest.param(BASE + "auth:\n", True, id="auth-null"),
        pytest.param(BASE + "auth: azcli\n", False, id="auth-unknown"),
        pytest.param(BASE + "require_spn: true\n", True, id="require-spn"),
        pytest.param(BASE + "require_spn: 'yes'\n", False, id="require-spn-str"),
        pytest.param(BASE + "require_spn:\n", False, id="require-spn-null"),
        pytest.param(BASE + f"capacity_id: {GUID}\n", True, id="capacity"),
        pytest.param(BASE + "capacity_id:\n", True, id="capacity-null"),
        pytest.param(BASE + "capacity_id: F64\n", False, id="capacity-name"),
        # manual_parameters
        pytest.param(BASE + ENTRY, True, id="manual"),
        pytest.param(BASE + "manual_parameters:\n", True, id="manual-null"),
        pytest.param(BASE + "manual_parameters: x\n", False, id="manual-string"),
        pytest.param(BASE + "manual_parameters:\n  - x\n", False, id="manual-entry-string"),
        pytest.param(
            BASE + ENTRY + "    item_type: [Notebook, Report]\n    description: Shared\n",
            True,
            id="manual-full",
        ),
        pytest.param(BASE + ENTRY + "    item_type: Notebook\n", True, id="item-type-string"),
        pytest.param(BASE + ENTRY + "    item_type:\n", True, id="item-type-null"),
        pytest.param(BASE + ENTRY + "    item_type: ['']\n", False, id="item-type-empty"),
        pytest.param(BASE + ENTRY + "    item_type: {a: b}\n", False, id="item-type-mapping"),
        pytest.param(BASE + ENTRY + "    description:\n", True, id="description-null"),
        pytest.param(BASE + ENTRY + "    description: 5\n", False, id="description-int"),
        pytest.param(BASE + ENTRY + "    file_path: x\n", False, id="manual-unknown-key"),
        pytest.param(
            BASE + f"manual_parameters:\n  - find_value: [{GUID}, b]\n"
            f"    replace_value:\n      PROD: {GUID}\n      DEV: c\n",
            True,
            id="find-list-replace-map",
        ),
        pytest.param(
            BASE + "manual_parameters:\n  - find_value: a\n    replace_value:\n      _all_: c\n",
            True,
            id="replace-map-all",
        ),
        pytest.param(
            BASE + "manual_parameters:\n  - find_value: a\n"
            "    replace_value:\n      PROD: b\n      _ALL_: c\n",
            False,
            id="replace-map-all-and-environment",
        ),
        pytest.param(
            BASE + "manual_parameters:\n  - find_value: []\n    replace_value: x\n",
            False,
            id="find-empty-list",
        ),
        pytest.param(
            BASE + "manual_parameters:\n  - find_value: ''\n    replace_value: x\n",
            False,
            id="find-empty",
        ),
        pytest.param(
            BASE + "manual_parameters:\n  - find_value: 3\n    replace_value: x\n",
            False,
            id="find-int",
        ),
        pytest.param(BASE + "manual_parameters:\n  - find_value: a\n", False, id="no-replace"),
        pytest.param(BASE + "manual_parameters:\n  - replace_value: a\n", False, id="no-find"),
        pytest.param(
            BASE + "manual_parameters:\n  - find_value: a\n    replace_value: {}\n",
            False,
            id="replace-empty-map",
        ),
        pytest.param(
            BASE + "manual_parameters:\n  - find_value: a\n    replace_value: [b]\n",
            False,
            id="replace-list",
        ),
        pytest.param(
            BASE + "manual_parameters:\n  - find_value: a\n    replace_value:\n      PROD: 5\n",
            False,
            id="replace-map-int",
        ),
    ],
)
def test_agrees_with_the_loader(tmp_path: Path, body: str, valid: bool):
    assert (schema_accepts(body), loader_accepts(tmp_path, body)) == (valid, valid)


def test_unknown_top_level_keys_are_invalid_but_only_warned_about(tmp_path: Path, caplog):
    """The loader warns about an unknown key rather than failing; an editor flags it."""
    body = BASE + f"tenan: {GUID}\n"
    with caplog.at_level(logging.WARNING, logger="fabric_deploy"):
        assert loader_accepts(tmp_path, body)
    assert "did you mean 'tenant'" in caplog.text
    assert not schema_accepts(body)


def test_fabdep_schema_prints_it(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["fabdep", "schema"])
    main()
    out = capsys.readouterr().out
    assert out == schema_text()
    assert json.loads(out) == SCHEMA


def test_fabdep_schema_writes_it(monkeypatch, tmp_path: Path):
    target = tmp_path / "fabdep.schema.json"
    monkeypatch.setattr(sys, "argv", ["fabdep", "schema", "-o", str(target)])
    main()
    assert target.read_text(encoding="utf-8") == schema_text()
