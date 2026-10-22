"""Configuration loading for fabric-deploy.

Reads `fabdep.yml`, resolves the git branch, and builds the workspace name.
Pure of network calls — `WorkspaceDeployer` performs the Fabric API lookup
that turns the workspace name into a workspace ID.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from difflib import get_close_matches
from pathlib import Path
from typing import cast

import yaml

from .git import current_branch

__all__ = ["AUTH_MODES", "FabdepConfig", "TenantPin", "load_tenant_pin"]

# Credential modes for `auth:`, --auth and FABDEP_AUTH (see `identity`).
AUTH_MODES = ("cli", "env", "workload", "managed", "default")

FABDEP_YML_KEYS = frozenset(
    {"workspace_base_name", "capacity_id", "tenant", "auth", "require_spn", "manual_parameters"}
)
_GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


@dataclass(frozen=True)
class TenantPin:
    """What fabdep.yml requires of the deploying identity.

    `id` is the Microsoft Entra tenant every token must come from, `auth` the
    credential mode (see `identity`), and `require_spn` rules out user sign-ins.
    """

    id: str | None = None  # lower-case tenant GUID
    auth: str | None = None
    require_spn: bool = False

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> TenantPin | None:
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
        return cls(id=tenant.lower() if tenant else None, auth=auth, require_spn=require_spn)


def load_tenant_pin(repo_dir: Path | str) -> TenantPin | None:
    """The identity keys of `repo_dir`'s fabdep.yml, if it has one."""
    path = Path(repo_dir) / "fabdep.yml"
    if not path.exists():
        return None
    return TenantPin.from_config(_read_yaml(path))


def _read_yaml(path: Path) -> dict[str, object]:
    """A YAML file's top-level mapping; an empty file is an empty mapping."""
    data: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must be a mapping of keys to values.")
    return cast(dict[str, object], data)


@dataclass
class FabdepConfig:
    """Resolved configuration for a workspace deployment.

    Construct via `FabdepConfig.from_path()`. All fields are populated up
    front so the rest of the deployer can read them without any I/O.
    """

    repo_dir: Path  # resolved absolute path
    branch: str  # branch name (after override / git lookup)
    workspace_name: str  # f"{base} [{BRANCH_UPPER}]"
    workspace_base_name: str
    capacity_id: str | None = None
    manual_parameters: list[object] = field(default_factory=list)
    tenant: TenantPin | None = None  # fabdep.yml's identity keys; None when unset

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
        if not base or not isinstance(base, str):
            raise ValueError(
                "fabdep.yml must set workspace_base_name to the workspace name without "
                "its branch suffix:\n"
                '  workspace_base_name: "Contoso Sales"\n\n'
                "The workspace will be resolved from: {base_name} [{BRANCH}]"
            )

        manual_parameters = config.get("manual_parameters") or []
        if not isinstance(manual_parameters, list):
            raise ValueError("fabdep.yml manual_parameters must be a list of entries.")
        capacity_id = config.get("capacity_id")

        branch = branch_override or current_branch(resolved)
        return cls(
            repo_dir=resolved,
            branch=branch,
            workspace_name=f"{base} [{branch.upper()}]",
            workspace_base_name=base,
            capacity_id=str(capacity_id) if capacity_id else None,
            manual_parameters=cast(list[object], manual_parameters),
            tenant=TenantPin.from_config(config),
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
