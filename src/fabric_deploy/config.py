"""Configuration loading for fabric-deploy.

Reads `fabdep.yml`, resolves the git branch, and picks the target workspace.
Pure of network calls: `WorkspaceDeployer` turns the workspace name into an ID.

The keys that choose the target workspace:

    workspace_base_name: Contoso Sales            # {base} in templates
    workspace_name_template: "{base} [{BRANCH}]"  # optional; this is the default
    capacity_id: <capacity GUID>                  # optional; for workspaces fabdep creates
    environments:                                 # optional; keyed by branch name or glob
      main:
        workspace: Contoso Sales                  # a literal name or a template
        environment: PROD                         # fabric-cicd environment (default: the branch)
      dev:
        workspace: "{base} (Dev)"
      "feature/*":
        create: true                              # create the workspace when it is missing

A branch uses the entry keyed by its exact name, else the first glob key that
matches it (`*` matches any characters, `?` one; case-sensitive), else none. An entry without
`workspace`, and a branch without an entry, use workspace_name_template.
Templates may use {base}, {branch} and {BRANCH} (the branch upper-cased);
`{{` and `}}` are literal braces. workspace_base_name is required when a
template fabdep uses needs {base}.

The repo's workspaces are the ones `environments` names (for a glob key, the
names its branches would get) or, without `environments`, every name
workspace_name_template produces. A deploy rewrites ids of the repo's other
workspaces found in the content to the target's; ids of any other workspace
deploy unchanged.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from difflib import get_close_matches
from pathlib import Path
from string import Formatter
from typing import cast

import yaml

from .git import current_branch

__all__ = ["AUTH_MODES", "AuthSettings", "FabdepConfig", "load_auth_settings", "load_tenant_pin"]

# Credential modes for `auth:`, --auth and FABDEP_AUTH (see `identity`).
AUTH_MODES = ("cli", "env", "workload", "managed", "default")

FABDEP_YML_KEYS = frozenset(
    {
        "workspace_base_name",
        "workspace_name_template",
        "environments",
        "capacity_id",
        "tenant",
        "auth",
        "require_spn",
        "manual_parameters",
    }
)
DEFAULT_WORKSPACE_NAME_TEMPLATE = "{base} [{BRANCH}]"
ENVIRONMENT_KEYS = ("workspace", "environment", "create")
_TEMPLATE_FIELDS = ("base", "branch", "BRANCH")
# Any branch, in a pattern for workspace names. Excluding "]" keeps the default
# template from matching "{base} [DEV] [OLD]".
_ANY_BRANCH = r"[^\]]+"
_GLOB = {"*": r"[^\]]*", "?": r"[^\]]"}
_GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


@dataclass(frozen=True)
class AuthSettings:
    """What fabdep.yml requires of the deploying identity.

    `tenant` is the Microsoft Entra tenant every token must come from, `mode` the
    credential mode (`auth:`, see `identity`), and `require_spn` rules out user
    sign-ins.
    """

    tenant: str | None = None  # lower-case tenant GUID
    mode: str | None = None
    require_spn: bool = False

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> AuthSettings | None:
        """Read the identity keys of a parsed fabdep.yml; None when it sets none of them."""
        tenant, auth = config.get("tenant"), config.get("auth")
        require_spn = config.get("require_spn", False)
        if tenant is not None and not (isinstance(tenant, str) and _GUID.fullmatch(tenant)):
            raise ValueError(
                f"fabdep.yml 'tenant' must be a Microsoft Entra tenant ID (a GUID), got {tenant!r}.\n"
                "Find yours with `az account show --query tenantId --output tsv`, or in the "
                "Azure portal under Microsoft Entra ID > Overview."
            )
        if auth is not None and not (isinstance(auth, str) and auth in AUTH_MODES):
            raise ValueError(
                f"fabdep.yml 'auth' must be one of: {', '.join(AUTH_MODES)} (got {auth!r})."
            )
        if not isinstance(require_spn, bool):
            raise ValueError(
                f"fabdep.yml 'require_spn' must be true or false (got {require_spn!r})."
            )
        if tenant is None and auth is None and not require_spn:
            return None
        return cls(tenant=tenant.lower() if tenant else None, mode=auth, require_spn=require_spn)


def load_auth_settings(repo_dir: Path | str) -> AuthSettings | None:
    """The identity keys of `repo_dir`'s fabdep.yml, if it has one."""
    path = Path(repo_dir) / "fabdep.yml"
    if not path.exists():
        return None
    return AuthSettings.from_config(_read_yaml(path))


load_tenant_pin = load_auth_settings  # the name cli.py imports


def _read_yaml(path: Path) -> dict[str, object]:
    """A YAML file's top-level mapping; an empty file is an empty mapping."""
    data: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must be a mapping of keys to values.")
    return cast(dict[str, object], data)


@dataclass(frozen=True)
class _Template:
    """A workspace name template, checked for unknown placeholders up front."""

    text: str
    where: str  # the fabdep.yml key it came from, for error messages
    parts: tuple[tuple[str, str | None], ...]  # (literal text, placeholder or None)

    @classmethod
    def parse(cls, text: str, where: str) -> _Template:
        try:
            parsed = list(Formatter().parse(text))
        except ValueError as e:
            raise ValueError(f"fabdep.yml {where}: {e} in {text!r}.") from None
        for _, name, spec, conversion in parsed:
            if name is not None and (name not in _TEMPLATE_FIELDS or spec or conversion):
                written = (
                    name + (f"!{conversion}" if conversion else "") + (f":{spec}" if spec else "")
                )
                raise ValueError(
                    f"fabdep.yml {where}: unknown placeholder {{{written}}} in {text!r}; "
                    "use {base}, {branch} or {BRANCH}."
                )
        return cls(text, where, tuple((literal, name) for literal, name, _, _ in parsed))

    def _check_base(self, base: str | None) -> None:
        if base is None and any(name == "base" for _, name in self.parts):
            raise ValueError(
                f"fabdep.yml must set workspace_base_name: {self.where} {self.text!r} uses "
                '{base}.\n  workspace_base_name: "Contoso Sales"'
            )

    def render(self, base: str | None, branch: str) -> str:
        self._check_base(base)
        values = {"base": base or "", "branch": branch, "BRANCH": branch.upper()}
        return "".join(literal + (values[name] if name else "") for literal, name in self.parts)

    def pattern(self, base: str | None, branch: str) -> str:
        """A regex for the names this template gives the branches regex `branch` matches."""
        self._check_base(base)
        return "".join(
            re.escape(literal)
            + ("" if name is None else re.escape(base or "") if name == "base" else f"(?:{branch})")
            for literal, name in self.parts
        )


@dataclass(frozen=True)
class _Environment:
    """One `environments:` entry of fabdep.yml: where and how its branches deploy."""

    key: str  # branch name or glob
    glob: str | None  # the key as a regex when it is a glob
    template: _Template  # its `workspace`, else workspace_name_template
    environment: str | None  # fabric-cicd environment; None: the branch name
    create: bool  # create the workspace when it doesn't exist

    @classmethod
    def parse(cls, key: object, raw: object, default: _Template) -> _Environment:
        """Validate one entry. Unknown keys are errors, unlike at the top level:
        a misspelled `workspace` would deploy to a different workspace."""
        if not isinstance(key, str):
            raise ValueError(
                f"fabdep.yml environments key {key!r} must be a branch name; quote it: '{key}'."
            )
        where = f"fabdep.yml environments '{key}'"
        if raw is not None and not isinstance(raw, dict):
            raise ValueError(
                f"{where} must be a mapping with any of: {', '.join(ENVIRONMENT_KEYS)}."
            )
        entry = cast(dict[str, object], raw or {})
        for name in entry:
            if name not in ENVIRONMENT_KEYS:
                hint = get_close_matches(str(name), ENVIRONMENT_KEYS, n=1, cutoff=0.6)
                raise ValueError(
                    f"{where}: unknown key '{name}'"
                    + (f" — did you mean '{hint[0]}'?" if hint else "")
                    + f" (known keys: {', '.join(ENVIRONMENT_KEYS)})"
                )
        workspace, environment = entry.get("workspace"), entry.get("environment")
        for name, value in (("workspace", workspace), ("environment", environment)):
            if value is not None and not (isinstance(value, str) and value):
                raise ValueError(f"{where}: '{name}' must be a non-empty string.")
        create = entry.get("create", False)
        if not isinstance(create, bool):
            raise ValueError(f"{where}: 'create' must be true or false.")
        glob = None
        if "*" in key or "?" in key:
            glob = "".join(_GLOB.get(c) or re.escape(c) for c in key)
        return cls(
            key=key,
            glob=glob,
            template=_Template.parse(workspace, f"environments '{key}' workspace")
            if isinstance(workspace, str)
            else default,
            environment=cast(str | None, environment),
            create=create,
        )

    def matches(self, branch: str) -> bool:
        return re.fullmatch(self.glob, branch) is not None if self.glob else branch == self.key

    def name_pattern(self, base: str | None) -> str:
        """A regex for the workspace names this entry gives its branches."""
        if self.glob:
            return self.template.pattern(base, self.glob)
        return re.escape(self.template.render(base, self.key))


def _environments(raw: object, default: _Template) -> list[_Environment]:
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise ValueError(
            "fabdep.yml environments must map branch names (or globs) to settings, e.g.\n"
            "  environments:\n    main:\n      workspace: Contoso Sales"
        )
    return [
        _Environment.parse(key, value, default)
        for key, value in cast(dict[object, object], raw).items()
    ]


@dataclass
class FabdepConfig:
    """Resolved configuration for a workspace deployment.

    Construct via `FabdepConfig.from_path()`. All fields are populated up
    front so the rest of the deployer can read them without any I/O.
    """

    repo_dir: Path  # resolved absolute path
    branch: str  # branch name (after override / git lookup)
    workspace_name: str  # the target workspace's display name
    workspace_base_name: str | None
    environment: str  # fabric-cicd environment name
    repo_workspaces: re.Pattern[str] = field(repr=False)  # see is_repo_workspace
    create_workspace: bool = False  # create the target when it doesn't exist
    capacity_id: str | None = None
    manual_parameters: list[object] = field(default_factory=list)
    auth: AuthSettings | None = None  # fabdep.yml's identity keys; None when unset

    def is_repo_workspace(self, name: str) -> bool:
        """Whether a workspace of this display name is one this fabdep.yml deploys to."""
        return self.repo_workspaces.fullmatch(name) is not None

    @classmethod
    def from_path(cls, repo_dir: Path | str, branch_override: str | None = None) -> FabdepConfig:
        """Build a FabdepConfig from a workspace repository directory.

        Args:
            repo_dir: Path to the workspace repo.
            branch_override: Skip git branch lookup and use this branch name.

        Raises:
            FileNotFoundError: If `repo_dir` or its fabdep.yml doesn't exist.
            ValueError: If fabdep.yml is invalid.
            RuntimeError: If git branch lookup fails (no override + not in a repo).
        """
        resolved = Path(repo_dir).resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"Directory does not exist: {resolved}")

        config_file = resolved / "fabdep.yml"
        if not config_file.is_file():
            raise FileNotFoundError(
                f"No fabdep.yml in {resolved}.\n\n"
                "fabdep reads the target workspace from it. The smallest one is:\n"
                '  workspace_base_name: "Contoso Sales"\n\n'
                "which deploys branch <name> to the workspace 'Contoso Sales [<NAME>]'."
            )
        config = _read_yaml(config_file)
        _warn_unknown_keys(config)

        base = config.get("workspace_base_name")
        if base is not None and not (isinstance(base, str) and base):
            raise ValueError("fabdep.yml workspace_base_name must be a non-empty string.")
        template = config.get("workspace_name_template", DEFAULT_WORKSPACE_NAME_TEMPLATE)
        if not (isinstance(template, str) and template):
            raise ValueError("fabdep.yml workspace_name_template must be a non-empty string.")
        default = _Template.parse(template, "workspace_name_template")
        environments = _environments(config.get("environments"), default)
        repo_workspaces = [env.name_pattern(base) for env in environments] or [
            default.pattern(base, _ANY_BRANCH)
        ]

        manual_parameters: object = config.get("manual_parameters") or []
        if not isinstance(manual_parameters, list):
            raise ValueError("fabdep.yml manual_parameters must be a list of entries.")
        capacity_id = config.get("capacity_id")

        branch = branch_override or current_branch(resolved)
        # An exact key wins; otherwise the first glob that matches.
        entry = next((e for e in environments if not e.glob and e.matches(branch)), None)
        entry = entry or next((e for e in environments if e.matches(branch)), None)
        return cls(
            repo_dir=resolved,
            branch=branch,
            workspace_name=(entry.template if entry else default).render(base, branch),
            workspace_base_name=base,
            environment=(entry and entry.environment) or branch,
            repo_workspaces=re.compile("|".join(f"(?:{p})" for p in repo_workspaces), re.I),
            create_workspace=bool(entry and entry.create),
            capacity_id=str(capacity_id) if capacity_id else None,
            manual_parameters=cast(list[object], manual_parameters),
            auth=AuthSettings.from_config(config),
        )


def _warn_unknown_keys(config: Mapping[str, object]) -> None:
    """Flag fabdep.yml keys fabdep doesn't read, so a typo like `tenan:` is loud.

    A warning rather than an error: a misspelled optional key silently dropping
    a feature is the failure mode here, not an unusable config.
    """
    for key in config:
        if key in FABDEP_YML_KEYS:
            continue
        hint = get_close_matches(str(key), FABDEP_YML_KEYS, n=1, cutoff=0.6)
        suggestion = f" — did you mean '{hint[0]}'?" if hint else ""
        logging.getLogger("fabric_deploy").warning(
            f"fabdep.yml: unknown key '{key}' ignored{suggestion} "
            f"(known keys: {', '.join(sorted(FABDEP_YML_KEYS))})"
        )
