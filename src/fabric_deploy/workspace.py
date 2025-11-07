"""Workspace deployment abstraction for Microsoft Fabric workspaces."""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from json import loads
from pathlib import Path

import yaml
from fabric_cicd import (
    FabricWorkspace,
    append_feature_flag,
    publish_all_items,
    unpublish_all_orphan_items,
)
from fabric_cicd.constants import ACCEPTED_ITEM_TYPES

from .api import get_workspace_id_by_name
from .detect import ParameterDetector
from .log import get_logger, print_header


@dataclass
class WorkspaceDeployer:
    """
    Main deployer class for Fabric workspace items.

    Encapsulates configuration extraction, validation, and deployment logic.
    Configuration is loaded lazily on first deploy() call.
    """

    repo_dir: Path | str
    logger_name: str | None = None

    # Configuration fields - auto-loaded in __post_init__ if not provided
    workspace_id: str | None = None
    environment: str | None = None
    lakehouse_name: str | None = None
    auto_detect: bool = False  # Enable auto-detection mode

    # Private fields
    _repo_dir_resolved: Path = field(init=False, repr=False)
    logger: logging.Logger = field(init=False, repr=False)

    def __post_init__(self):
        """Initialize derived fields and auto-load config if not provided."""
        self._repo_dir_resolved = Path(self.repo_dir).resolve()
        self.logger = get_logger(self.logger_name or __name__)

        # Auto-load configuration if not provided by user
        if self.auto_detect:
            # Generate parameter file dynamically
            self._generate_dynamic_params()

        if self.workspace_id is None:
            # No workspace ID provided - must load from parameter.yml
            self._load_config()
        elif self.environment is None:
            # Workspace ID provided but no environment - default to prod
            self.environment = "prod"

        if self.lakehouse_name is None:
            self._detect_lakehouse()

    def _get_git_branch(self) -> str:
        """
        Get current git branch name.

        Returns:
            Branch name

        Raises:
            RuntimeError: If not in a git repository or git command fails
        """
        import subprocess

        try:
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=self._repo_dir_resolved,
                capture_output=True,
                text=True,
                check=True,
            )
            branch = result.stdout.strip()
            if not branch:
                raise RuntimeError("Not on a git branch (detached HEAD state)")
            return branch
        except subprocess.CalledProcessError as e:
            error_msg = e.stderr.strip() if e.stderr else "Unknown error"
            raise RuntimeError(
                f"Failed to get git branch.\n{error_msg}\n\n"
                "Make sure you're in a git repository and git is installed."
            ) from e

    def _resolve_workspace_id_from_name(self, base_name: str, branch: str) -> str:
        """
        Resolve workspace ID from base name and branch.

        Combines base_name with branch suffix (case-insensitive) and looks up
        workspace ID via Fabric API.

        Args:
            base_name: Workspace base name from parameter.yml
            branch: Current git branch name

        Returns:
            Workspace ID (GUID)

        Raises:
            ValueError: If workspace not found
            RuntimeError: If API call fails
        """
        workspace_name = f"{base_name} [{branch}]"
        self.logger.info(f"Resolving workspace: '{workspace_name}'")
        self.logger.info(f"  Base name: {base_name}")
        self.logger.info(f"  Branch: {branch}")

        try:
            workspace_id = get_workspace_id_by_name(workspace_name)
            self.logger.info(f"  Workspace ID: {workspace_id}")
            return workspace_id
        except ValueError as e:
            # Re-raise with additional context
            raise ValueError(
                f"Failed to resolve workspace ID.\n{e}\n\n"
                f"Expected workspace name: '{workspace_name}' "
                f"(base: '{base_name}', branch: '{branch}')"
            ) from e

    def _load_config(self) -> None:
        """Load and validate workspace configuration from parameter.yml or parameter.yml.bak."""
        if not self._repo_dir_resolved.exists():
            raise FileNotFoundError(
                f"Directory does not exist: {self._repo_dir_resolved}"
            )

        param_file = self._repo_dir_resolved / "parameter.yml"
        bak_file = self._repo_dir_resolved / "parameter.yml.bak"

        # Check for parameter.yml, fallback to .bak if not found
        if not param_file.exists():
            if bak_file.exists():
                self.logger.info(f"parameter.yml not found, using {bak_file.name}")
                param_file = bak_file
            else:
                raise FileNotFoundError(
                    f"parameter.yml not found in {self._repo_dir_resolved}. "
                    "Make sure you're running this from a Fabric workspace directory"
                )

        # Load workspace_base_name from parameter.yml
        with open(param_file) as f:
            params = yaml.safe_load(f)

        workspace_base_name = params.get("workspace_base_name")

        if not workspace_base_name:
            raise ValueError(
                f"No workspace_base_name found in {param_file.name}.\n\n"
                "parameter.yml must contain:\n"
                '  workspace_base_name: "your_workspace"\n\n'
                "The workspace will be resolved from: {base_name} [{branch}]"
            )

        # Resolve workspace ID from base name + branch
        self.logger.info("Using workspace base name from parameter.yml")
        branch = self._get_git_branch()
        workspace_id = self._resolve_workspace_id_from_name(workspace_base_name, branch)
        self.workspace_id = workspace_id
        self.environment = "default"

    def _generate_dynamic_params(self) -> None:
        """
        Generate parameter file dynamically using auto-detection.

        Requires workspace_id to be set beforehand.
        Creates parameter.yml.auto and backs up existing parameter.yml if present.
        """
        if not self.workspace_id:
            raise ValueError(
                "workspace_id must be provided when using auto-detect mode"
            )

        print()
        self.logger.info("Auto-detecting source IDs from workspace files...")

        # Detect parameters
        detector = ParameterDetector(repo_dir=self._repo_dir_resolved)
        _ = detector.detect_all()

        # Backup existing parameter.yml if it exists
        param_file = self._repo_dir_resolved / "parameter.yml"
        backup_file = self._repo_dir_resolved / "parameter.yml.backup"
        if param_file.exists():
            _ = param_file.rename(backup_file)
            self.logger.info(f"Backed up existing parameter.yml to {backup_file.name}")

        # Generate parameter file for deployment (using dynamic variables)
        detector.save_parameter_yml(
            output_path=param_file,
            use_dynamic=True,
            environment="prod",
            skip_empty=True,
        )

        # Count what was generated
        with_dynamic = sum(
            1 for p in detector.parameters.values() if p.dynamic_variable
        )
        total = len(detector.parameters)

        self.logger.info(f"Generated parameter file with {with_dynamic} parameter(s)")
        if with_dynamic < total:
            self.logger.info(
                f"Skipped {total - with_dynamic} parameter(s) without dynamic variables"
            )
        self.logger.info("Using dynamic variables in replace_value")

    def _cleanup_dynamic_params(self) -> None:
        """
        Cleanup auto-generated parameter file and restore backup if present.
        """
        param_file = self._repo_dir_resolved / "parameter.yml"
        backup_file = self._repo_dir_resolved / "parameter.yml.backup"

        # Remove auto-generated file
        if param_file.exists():
            param_file.unlink()

        # Restore backup
        if backup_file.exists():
            _ = backup_file.rename(param_file)
            self.logger.info("Restored original parameter.yml")

    def _detect_lakehouse(self) -> None:
        """
        Detect lakehouse name from .Lakehouse directory.

        Sets self.lakehouse_name from metadata.
        """
        lakehouses = list(self._repo_dir_resolved.glob("*.Lakehouse"))
        if len(lakehouses) == 0:
            raise ValueError("No lakehouse found in repository directory")
        elif len(lakehouses) > 1:
            self.logger.warning(
                f"Multiple lakehouses found: {[d.name for d in lakehouses]}"
            )
        platform_file = lakehouses[0] / ".platform"
        try:
            metadata = loads(platform_file.read_text())
        except Exception as e:
            raise RuntimeError(f"Failed to read lakehouse metadata: {e}") from e
        self.lakehouse_name = metadata["metadata"]["displayName"]

    def deploy(
        self,
        no_cleanup: bool = False,
        item_types: list[str] | None = None,
        exclude_types: list[str] | None = None,
        include_items: list[str] | None = None,
    ) -> None:
        """
        Deploy Fabric workspace items.

        Args:
            no_cleanup: Skip unpublishing orphaned items
            item_types: Specific item types to deploy
            exclude_types: Item types to exclude from deployment
            include_items: Include only specific items (format: "ItemName.ItemType")
        """
        # Ensure configuration is loaded (helps type checker)
        assert isinstance(self.environment, str), "environment must be a str"
        assert isinstance(self.lakehouse_name, str), "lakehouse_name must be a str"

        print_header("Fabric CI/CD")

        # Enable experimental feature flags if name-based filtering is requested
        if include_items:
            append_feature_flag("enable_experimental_features")
            append_feature_flag("enable_items_to_include")
            self.logger.warning(
                "Name-based item filtering is experimental. "
                "Selective deployment may cause dependency issues. Use at your own risk."
            )

        # Validate item type filters
        if item_types:
            invalid_types = [t for t in item_types if t not in ACCEPTED_ITEM_TYPES]
            if invalid_types:
                self.logger.warning(
                    f"Unrecognized item types in filter: {', '.join(invalid_types)}"
                )
                self.logger.warning(
                    f"Supported types: {', '.join(ACCEPTED_ITEM_TYPES)}"
                )

        if exclude_types:
            invalid_types = [t for t in exclude_types if t not in ACCEPTED_ITEM_TYPES]
            if invalid_types:
                self.logger.warning(
                    f"Unrecognized item types in exclusion filter: {', '.join(invalid_types)}"
                )
                self.logger.warning(
                    f"Supported types: {', '.join(ACCEPTED_ITEM_TYPES)}"
                )

        # Build item_type_in_scope based on include/exclude flags
        item_type_in_scope = None
        if item_types:
            item_type_in_scope = item_types
        elif exclude_types:
            item_type_in_scope = [
                t for t in ACCEPTED_ITEM_TYPES if t not in exclude_types
            ]

        # self.logger.info(f"Lakehouse name: {self.lakehouse_name}")
        # self.logger.info(f"Input directory: {self._repo_dir_resolved}")
        self.logger.info(f"Workspace ID: {self.workspace_id}")
        if item_type_in_scope:
            self.logger.info(f"Item types in scope: {', '.join(item_type_in_scope)}")
        if include_items:
            self.logger.info(f"Including items: {', '.join(include_items)}")

        # Display parameter.yml last modified time
        param_file = self._repo_dir_resolved / "parameter.yml"
        bak_file = param_file.with_suffix(".yml.bak")

        if bak_file.exists() and not param_file.exists():
            # Using .bak file
            mtime = datetime.fromtimestamp(bak_file.stat().st_mtime)
            self.logger.info(
                f"Parameter file (.bak) modified: {mtime.strftime('%d/%m/%Y %H:%M:%S')}"
            )
        elif param_file.exists():
            mtime = datetime.fromtimestamp(param_file.stat().st_mtime)
            self.logger.info(
                f"Parameter file modified: {mtime.strftime('%d/%m/%Y %H:%M:%S')}"
            )

        target_workspace = FabricWorkspace(
            workspace_id=self.workspace_id,
            repository_directory=str(self._repo_dir_resolved),
            environment=self.environment,
            item_type_in_scope=item_type_in_scope,
        )

        try:
            _ = publish_all_items(target_workspace, items_to_include=include_items)

            if not no_cleanup:
                unpublish_all_orphan_items(
                    target_workspace, item_name_exclude_regex=self.lakehouse_name
                )

            print()
            self.logger.info("Deployment complete!")
            print()

        except Exception:
            # Re-raise exception to be handled by caller
            # CLI will handle error message formatting via handle_command_error()
            # Programmatic users can catch and handle exceptions themselves
            raise
        finally:
            # Cleanup auto-generated parameter file if in auto-detect mode
            if self.auto_detect:
                self._cleanup_dynamic_params()
