"""Help for writing fabdep.yml: a starter file (`fabdep init`) and its JSON Schema (`fabdep schema`)."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

__all__ = ["STARTER_EXAMPLES", "schema_text", "starter_fabdep_yml", "write_starter"]

_HEADER = """\
# fabdep.yml: the Microsoft Fabric workspace each branch of this repo deploys to.
# Every other setting follows as a commented-out example: uncomment one to use it.
#
# Editors can check this file against fabdep's schema: save it next to this file with
#   fabdep schema -o fabdep.schema.json
# and make this the file's first line (for the YAML language server, as in VS Code):
#   # yaml-language-server: $schema=./fabdep.schema.json

# By default branch <name> deploys to the workspace "<workspace_base_name> [<NAME>]".
"""

# (explanation, example) for each optional key, written out commented.
STARTER_EXAMPLES: tuple[tuple[str, str], ...] = (
    (
        "The workspace name of a branch whose `environments` entry names none; the value\n"
        "shown is the default. {base} is workspace_base_name, {branch} the branch name,\n"
        "{BRANCH} the branch name upper-cased; {{ and }} are literal braces.",
        'workspace_name_template: "{base} [{BRANCH}]"',
    ),
    (
        "Settings per branch, keyed by branch name or glob (* any characters, ? one): an\n"
        "exact name wins, then the first glob that matches. `workspace` is a name or a\n"
        "template; `environment` is the fabric-cicd environment that selects parameter.yml\n"
        "values and Variable Library value sets (default: the branch name); `create: true`\n"
        "creates the workspace if it doesn't exist.",
        "environments:\n"
        "  main:\n"
        '    workspace: "{base}"\n'
        "    environment: PROD\n"
        "  dev:\n"
        '    workspace: "{base} [DEV]"\n'
        '  "feature/*":\n'
        "    create: true",
    ),
    (
        "The capacity of workspaces fabdep creates (-N, or create: true). Without it fabdep\n"
        "uses the only active capacity, and fails when several are active.",
        "capacity_id: 00000000-0000-0000-0000-000000000000",
    ),
    (
        "Who may deploy. tenant: the Microsoft Entra tenant ID every token must come from\n"
        "(az account show --query tenantId --output tsv). auth: how to sign in - cli (az\n"
        "login, the default), env (AZURE_* variables), workload (a federated OIDC token),\n"
        "managed (managed identity) or default (DefaultAzureCredential); --auth and\n"
        "FABDEP_AUTH override it. require_spn: refuse user sign-ins.",
        "tenant: 00000000-0000-0000-0000-000000000000\nauth: cli\nrequire_spn: true",
    ),
    (
        "Find/replace rules on top of the IDs fabdep detects (`fabdep detect` lists them).\n"
        "A detected find_value gets this replace_value instead; any other find_value is\n"
        "added as a rule if it occurs in an item file. replace_value is one value, or one\n"
        "per environment, by its exact name (_ALL_, alone, for every one); item_type limits\n"
        "it to item types.",
        "manual_parameters:\n"
        "  - find_value: 00000000-0000-0000-0000-0000000000aa\n"
        "    replace_value: 00000000-0000-0000-0000-0000000000aa\n"
        "    item_type: SemanticModel\n"
        "    description: Leave the shared SQL analytics endpoint unchanged\n"
        "  - find_value:\n"
        "      - 00000000-0000-0000-0000-0000000000bb\n"
        "      - 00000000-0000-0000-0000-0000000000cc\n"
        "    replace_value:\n"
        "      PROD: 00000000-0000-0000-0000-0000000000dd\n"
        "      dev: 00000000-0000-0000-0000-0000000000ee",
    ),
)


def schema_text() -> str:
    """fabdep.yml's JSON Schema (draft 2020-12), as shipped in the package."""
    return files("fabric_deploy").joinpath("fabdep.schema.json").read_text(encoding="utf-8")


def starter_fabdep_yml(base_name: str) -> str:
    """A fabdep.yml that sets workspace_base_name and shows every other key commented out."""
    # A JSON string is a valid YAML double-quoted scalar, whatever the name holds.
    lines = [_HEADER + f"workspace_base_name: {json.dumps(base_name, ensure_ascii=False)}\n"]
    for explanation, example in STARTER_EXAMPLES:
        lines.append("\n" + "".join(f"# {line}\n" for line in explanation.splitlines()))
        lines.append("".join(f"# {line}\n" for line in example.splitlines()))
    return "".join(lines)


def write_starter(directory: Path, base_name: str | None = None, force: bool = False) -> Path:
    """Write a starter fabdep.yml into `directory`.

    Args:
        directory: The workspace folder.
        base_name: workspace_base_name; default: the directory's name.
        force: Overwrite an existing fabdep.yml.

    Returns:
        The file written.

    Raises:
        FileExistsError: fabdep.yml exists and `force` is not set.
        ValueError: The base name is empty.
    """
    path = directory / "fabdep.yml"
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; pass --force to overwrite it.")
    base_name = directory.resolve().name if base_name is None else base_name
    if not base_name:
        raise ValueError("The workspace base name can't be empty; pass --base-name.")
    _ = path.write_text(starter_fabdep_yml(base_name), encoding="utf-8")
    return path
