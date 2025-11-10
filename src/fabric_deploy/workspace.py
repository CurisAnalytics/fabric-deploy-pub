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
from .auth import get_current_account
from .detect import ParameterDetector
from .log import get_logger, print_header


@dataclass
class WorkspaceDeployer:
    """
    Main deployer class for Fabric workspace items.

    Encapsulates configuration extraction, validation, and deployment logic.

    IMPORTANT: Initialization has side effects!
    - Reads parameter.yml from disk
    - Makes network calls to Fabric API (for workspace ID resolution)
    - Generates parameter files (if auto_detect=True)
    - Auto-detects lakehouse metadata

    These operations occur in __post_init__ if workspace_id or lakehouse_name
    are not explicitly provided.
    """

    repo_dir: Path | str
    logger_name: str | None = None

    # Configuration fields - auto-loaded in __post_init__ if not provided
    workspace_id: str | None = None
    workspace_name: str | None = None  # Resolved workspace name (base_name [branch])
    environment: str | None = None
    lakehouse_name: str | None = None
    auto_detect: bool = False  # Enable auto-detection mode
    save_detected_params: bool = False  # Save detected parameters to .detected file
    branch_override: str | None = None  # Override git branch for workspace resolution

    # Private fields
    _repo_dir_resolved: Path = field(init=False, repr=False)
    logger: logging.Logger = field(init=False, repr=False)

    def __post_init__(self):
        """
        Initialize derived fields and auto-load configuration.

        WARNING: This method has side effects and performs I/O operations:
        1. Resolves repository directory path
        2. Initializes logger
        3. If auto_detect=True:
           - Scans workspace files to detect source IDs
           - Checks if source/target workspaces match
           - Generates parameter.yml (if workspaces differ)
           - Optionally saves parameter.yml.detected
        4. If workspace_id is None:
           - Reads fabdep.yml from disk
           - Makes network call to Fabric REST API to resolve workspace ID
        5. If lakehouse_name is None:
           - Scans for .Lakehouse directories and reads .platform metadata

        These operations happen automatically during object construction,
        not during the deploy() call.
        """
        self._repo_dir_resolved = Path(self.repo_dir).resolve()
        self.logger = get_logger(self.logger_name or __name__)

        # Load workspace ID first (either from fabdep.yml or provided via CLI)
        if self.workspace_id is None:
            # No workspace ID provided - must load from fabdep.yml
            self._load_config()
        elif self.environment is None:
            # Workspace ID provided but no environment - default value
            self.environment = "env"

        # Auto-detect parameters if requested (now that workspace_id is set)
        if self.auto_detect:
            # Generate parameter file dynamically
            self._generate_dynamic_params()

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

    def _load_config(self) -> None:
        """Load and validate workspace configuration from fabdep.yml."""
        if not self._repo_dir_resolved.exists():
            raise FileNotFoundError(
                f"Directory does not exist: {self._repo_dir_resolved}"
            )

        config_file = self._repo_dir_resolved / "fabdep.yml"

        # Check for fabdep.yml
        if not config_file.exists():
            raise FileNotFoundError(
                f"fabdep.yml not found in {self._repo_dir_resolved}.\n\n"
                "Create a fabdep.yml file with:\n"
                '  workspace_base_name: "your_workspace"\n\n'
                "The workspace will be resolved from: {base_name} [{branch}]\n\n"
                "Note: This is separate from parameter.yml which is used by fabric-cicd."
            )

        # Load workspace_base_name from fabdep.yml
        with open(config_file) as f:
            config = yaml.safe_load(f)

        workspace_base_name = config.get("workspace_base_name")

        if not workspace_base_name:
            raise ValueError(
                "No workspace_base_name found in fabdep.yml.\n\n"
                "fabdep.yml must contain:\n"
                '  workspace_base_name: "your_workspace"\n\n'
                "The workspace will be resolved from: {base_name} [{branch}]"
            )

        # Resolve workspace ID from base name + branch
        if self.branch_override:
            branch = self.branch_override
        else:
            branch = self._get_git_branch()
        self.workspace_name = f"{workspace_base_name} [{branch}]"
        try:
            self.workspace_id = get_workspace_id_by_name(self.workspace_name)
        except ValueError:
            account = get_current_account()  # some keys available: name, id, tenantId
            assert account  # Should be set
            account_name: str = account["user"]["name"]
            self.logger.info(f"Current account: {account_name}")
            self.logger.error(
                f"Workspace '{self.workspace_name}' not found for account: {account}"
            )
            raise
        self.environment = "fabdep"

    def _generate_dynamic_params(self) -> None:
        """
        Generate parameter file dynamically using auto-detection.

        Requires workspace_id to be set beforehand.
        Checks if source and target workspaces are the same - if so, skips generation.
        Creates parameter.yml and backs up existing parameter.yml if present.
        Optionally saves a copy to parameter.yml.detected if save_detected_params is True.
        """
        if not self.workspace_id:
            raise ValueError(
                "workspace_id must be provided when using auto-detect mode"
            )

        self.logger.info("Auto-detecting source IDs from workspace files...")

        # Detect parameters
        detector = ParameterDetector(
            repo_dir=self._repo_dir_resolved, logger_name=self.logger.name
        )
        _ = detector.detect_all()

        # Check if source and target workspaces are the same
        if detector.should_skip_replacement(self.workspace_id):
            self.logger.info(
                f"Source and target workspace are the same ({self.workspace_id})"
            )
            self.logger.info(
                "No parameter file will be used -- deploying directly without replacements"
            )
            # Disable auto_detect so we don't try to clean up files later
            self.auto_detect = False
            return

        # Save detected parameters if requested
        assert self.environment  # for typing
        if self.save_detected_params:
            detected_file = self._repo_dir_resolved / "parameter.yml.detected"
            detector.save_parameter_yml(
                output_path=detected_file,
                use_dynamic=True,
                environment=self.environment,
                skip_empty=True,
            )
            # Count what was saved
            with_dynamic = sum(
                1 for p in detector.parameters.values() if p.dynamic_variable
            )
            self.logger.info(
                f"Saved {with_dynamic} detected parameter(s) to {detected_file.name}"
            )

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
            environment=self.environment,
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

        self.logger.info(f"Workspace Name: '{self.workspace_name}'")
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

        assert self.environment  # for typing
        target_workspace = FabricWorkspace(
            workspace_id=self.workspace_id,
            repository_directory=str(self._repo_dir_resolved),
            environment=self.environment,
            item_type_in_scope=item_type_in_scope,
        )

        try:
            _ = publish_all_items(target_workspace, items_to_include=include_items)
            if not no_cleanup:
                assert self.lakehouse_name  # for typing
                unpublish_all_orphan_items(
                    target_workspace, item_name_exclude_regex=self.lakehouse_name
                )
            self.logger.info("Deployment complete!")

        except Exception:  # Re-raise exception to be handled by caller
            raise  # CLI will handle error message formatting via handle_command_error()
        finally:  # Cleanup auto-generated parameter file if in auto-detect mode
            print()
            if self.auto_detect:
                self._cleanup_dynamic_params()
