"""Configuration loading for fabric-deploy.

Reads `fabdep.yml`, resolves the git branch, and builds the workspace name.
Pure of network calls — `WorkspaceDeployer` performs the Fabric API lookup
that turns the workspace name into a workspace ID.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import NotRequired, TypedDict

import yaml

from .git import current_branch


__all__ = ["FabdepConfig", "TenantPin", "TenantRegistryEntry"]


class TenantRegistryEntry(TypedDict):
    """A machine-local tenant registry entry (alias -> this).

    Written by `fabdep auth login <alias>` as it logs each tenant's SPN into its
    isolated config dir, and read by fabdep to resolve a `fabdep.yml` alias. That
    command is the single source of truth — see the multi-tenant-auth recipe.
    """

    id: str  # Entra tenant GUID asserted before deploy
    config_dir: NotRequired[str]  # AZURE_CONFIG_DIR selecting this tenant's login
    require_spn: NotRequired[bool]  # reject user creds, require an SPN (default off)
    app_id: NotRequired[str]  # SPN client id, for flag-free `fabdep auth login` re-login
    cert_path: NotRequired[str]  # SPN cert path, for flag-free re-login


@dataclass(frozen=True)
class TenantPin:
    """Identity guard from fabdep.yml's `tenant:` alias.

    `fabdep.yml` carries a portable alias (`tenant: contoso`) — no machine paths or
    GUIDs in the committed repo. `resolve()` looks the alias up in the local
    registry to get the tenant `id` and isolated `config_dir`.

    Tenant-centric by design: a deploy is allowed as long as the active identity
    lives in `id` — a user credential and a service principal in the same tenant
    both pass, unless the registry entry sets `require_spn`.
    """

    alias: str  # registry key from fabdep.yml; resolved to id/config_dir before deploy
    id: str | None = None  # expected Entra tenant GUID (filled by resolve())
    config_dir: str | None = None  # AZURE_CONFIG_DIR to select this tenant's login
    require_spn: bool = False  # reject user credentials, require an SPN

    @classmethod
    def from_yaml_value(cls, value: object) -> TenantPin:
        """Parse the `tenant:` alias string, raising on anything else."""
        if isinstance(value, str) and value:
            return cls(alias=value)
        raise ValueError(
            "fabdep.yml 'tenant' must be a registry alias string, e.g.:\n\n"
            "  tenant: contoso\n"
        )

    def resolve(self, registry: dict[str, TenantRegistryEntry]) -> TenantPin:
        """Fill `id`/`config_dir`/`require_spn` from the registry entry.

        Raises ValueError on an unknown alias or a registry entry missing `id`.
        """
        if self.alias not in registry:
            known = ", ".join(sorted(registry)) or "(none registered)"
            raise ValueError(
                f"Unknown tenant alias '{self.alias}' in fabdep.yml.\n"
                f"Known aliases: {known}.\n"
                "Log the tenant in with `fabdep auth login <alias>`, which registers "
                "it under 'tenants' in ~/.azure/azureProfile.fabdep.json."
            )
        entry = registry[self.alias]
        if not entry.get("id"):
            raise ValueError(
                f"Tenant alias '{self.alias}' is registered without an 'id'; "
                f"re-run `fabdep auth login {self.alias}`."
            )
        return TenantPin(
            alias=self.alias,
            id=entry["id"],
            config_dir=entry.get("config_dir"),
            require_spn=bool(entry.get("require_spn", False)),
        )


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
    tenant: TenantPin | None = None  # identity guard; None when unset
    from_yaml: bool = False  # True when fabdep.yml existed; False on dirname fallback

    @classmethod
    def from_path(
        cls, repo_dir: Path | str, branch_override: str | None = None
    ) -> FabdepConfig:
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

        tenant_value = config.get("tenant")
        tenant = TenantPin.from_yaml_value(tenant_value) if tenant_value else None

        return cls(
            repo_dir=resolved,
            branch=branch,
            workspace_name=workspace_name,
            workspace_base_name=base,
            capacity_id=config.get("capacity_id"),
            manual_parameters=config.get("manual_parameters") or [],
            tenant=tenant,
            from_yaml=from_yaml,
        )
