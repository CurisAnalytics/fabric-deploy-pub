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
    return TenantPin.from_config(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


@dataclass
class FabdepConfig:
    """Resolved configuration for a workspace deployment.

    Construct via `FabdepConfig.from_path()`. All fields are populated up
    front so the rest of the deployer can read them without any I/O.
    """

    repo_dir: Path  # resolved absolute path
    branch: str  # branch name (after override / git lookup)
    workspace_name: str  # f"{base} [{BRANCH_UPPER}]"
    workspace_base_name: str | None  # None when fabdep.yml is absent
    capacity_id: str | None = None
    manual_parameters: list[dict] = field(default_factory=list)
    tenant: TenantPin | None = None  # fabdep.yml's identity keys; None when unset
    from_yaml: bool = False  # True when fabdep.yml existed; False on dirname fallback

    @classmethod
    def from_path(cls, repo_dir: Path | str, branch_override: str | None = None) -> FabdepConfig:
        """Build a FabdepConfig from a workspace repository directory.

        - If `fabdep.yml` is present, parses it and uses `workspace_base_name`.
        - If absent, falls back to using the directory name as the workspace
          base; `from_yaml` is set False so the caller can decide whether to
          accept the fallback (e.g. via Fabric API lookup) or error out.

        Args:
            repo_dir: Path to the workspace repo.
            branch_override: Skip git branch lookup and use this branch name.

        Raises:
            FileNotFoundError: If `repo_dir` doesn't exist.
            ValueError: If fabdep.yml is present but missing `workspace_base_name`.
            RuntimeError: If git branch lookup fails (no override + not in a repo).
        """
        resolved = Path(repo_dir).resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"Directory does not exist: {resolved}")

        config_file = resolved / "fabdep.yml"
        config: dict = {}
        from_yaml = False
        if config_file.exists():
            with open(config_file) as f:
                config = yaml.safe_load(f) or {}
            from_yaml = True
            _warn_unknown_keys(config)

        branch = branch_override or current_branch(resolved)

        base = config.get("workspace_base_name")
        if from_yaml and not base:
            raise ValueError(
                "No workspace_base_name found in fabdep.yml.\n\n"
                "fabdep.yml must contain:\n"
                '  workspace_base_name: "your_workspace"\n\n'
                "The workspace will be resolved from: {base_name} [{branch}]"
            )

        # When fabdep.yml is absent, the directory name acts as a tentative
        # base — the deployer attempts a workspace lookup against it and
        # raises FileNotFoundError if the workspace doesn't exist either.
        effective_base = base or resolved.name
        workspace_name = f"{effective_base} [{branch.upper()}]"

        return cls(
            repo_dir=resolved,
            branch=branch,
            workspace_name=workspace_name,
            workspace_base_name=base,
            capacity_id=config.get("capacity_id"),
            manual_parameters=config.get("manual_parameters") or [],
            tenant=TenantPin.from_config(config),
            from_yaml=from_yaml,
        )


def _warn_unknown_keys(config: dict) -> None:
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
