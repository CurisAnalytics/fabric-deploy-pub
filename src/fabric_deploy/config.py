"""Configuration loading for fabric-deploy.

Reads `fabdep.yml`, resolves the git branch, and builds the workspace name.
Pure of network calls — `WorkspaceDeployer` performs the Fabric API lookup
that turns the workspace name into a workspace ID.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .git import current_branch


__all__ = ["FabdepConfig"]


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

        return cls(
            repo_dir=resolved,
            branch=branch,
            workspace_name=workspace_name,
            workspace_base_name=base,
            capacity_id=config.get("capacity_id"),
            manual_parameters=config.get("manual_parameters") or [],
            from_yaml=from_yaml,
        )
