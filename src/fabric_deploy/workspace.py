"""Workspace deployment abstraction for Microsoft Fabric workspaces."""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from json import loads
from pathlib import Path
from time import sleep

import yaml
from fabric_cicd import (
    FabricWorkspace,
    append_feature_flag,
    publish_all_items,
    unpublish_all_orphan_items,
)
from fabric_cicd.constants import ACCEPTED_ITEM_TYPES

from .api import (
    create_workspace,
    get_default_capacity_id,
    get_workspace_id_by_name,
    workspace_exists,
)
from .auth import get_current_account
from .detect import ParameterDetector
from .log import get_logger, print_header

# Item types that create workspace objects (foundational)
# These must be deployed first in bootstrap mode
FOUNDATIONAL_ITEMS = [
    "Lakehouse",
    "SemanticModel",
    "SQLDatabase",
    "Warehouse",
    "MirroredDatabase",
    "Eventhouse",
]

# Item types that reference workspace objects (dependent)
# These can be deployed after foundational items exist
DEPENDENT_ITEMS = [
    item_type
    for item_type in ACCEPTED_ITEM_TYPES
    if item_type not in FOUNDATIONAL_ITEMS
]


@dataclass
class WorkspaceDeployer:
    """
    Main deployer class for Fabric workspace items.
    Encapsulates configuration extraction, validation, and deployment logic.

    Attributes:
            repo_dir: Path to the repository directory containing Fabric workspace items
            logger_name: Optional custom logger name (defaults to module name)
            auto_detect: Enable auto-detection mode to generate parameter.yml dynamically
            save_detected_params: Save detected parameters to parameter.yml.detected file
            branch_override: Override git branch for workspace name resolution (defaults to current branch)
            bootstrap: Enable two-phase deployment (foundational items first, then all items)
            create_workspace: Create workspace if it doesn't exist (requires capacity_id in fabdep.yml)

    IMPORTANT: Initialization has side effects!
    - Reads fabdep.yml from disk
    - Makes network calls to Fabric API (for workspace ID resolution)
    - Generates parameter files (if auto_detect=True)
    - Auto-detects lakehouse metadata

    All configuration is loaded from fabdep.yml in __post_init__.
    """

    repo_dir: Path | str
    logger_name: str | None = None
    auto_detect: bool = False  # Enable auto-detection mode
    save_detected_params: bool = False  # Save detected parameters to .detected file
    branch_override: str | None = None  # Override git branch for workspace resolution
    bootstrap: bool = False  # Enable two-phase deployment (foundational items first)
    create_workspace: bool = False  # Create workspace if it doesn't exist

    # Configuration fields - auto-loaded in __post_init__
    workspace_id: str = field(init=False, repr=False)
    workspace_name: str = field(
        init=False, repr=False
    )  # Resolved workspace name (base_name [branch])
    lakehouse_name: str = field(init=False, repr=False)

    # Private fields
    _repo_dir_resolved: Path = field(init=False, repr=False)
    _auto_detect_summary: list[str] | None = field(default=None, init=False, repr=False)
    logger: logging.Logger = field(init=False, repr=False)

    def __post_init__(self):
        """
        Initialize derived fields and auto-load configuration.

        WARNING: This method has side effects and performs I/O operations:
        1. Resolves repository directory path
        2. Initializes logger
        3. Reads fabdep.yml from disk
        4. Makes network call to Fabric REST API to resolve workspace ID
        5. If auto_detect=True:
           - Scans workspace files to detect source IDs
           - Generates parameter.yml with dynamic variables
           - Optionally saves parameter.yml.detected
        6. Scans for .Lakehouse directories and reads .platform metadata

        These operations happen automatically during object construction,
        not during the deploy() call.
        """
        self._repo_dir_resolved = Path(self.repo_dir).resolve()
        self.logger = get_logger(self.logger_name or __name__)

        # Always load workspace ID from fabdep.yml
        self._load_config()

        # Auto-detect parameters if requested
        if self.auto_detect:
            self._generate_dynamic_params()

        # Always detect lakehouse
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
                '  workspace_base_name: "your_workspace"\n'
                '  capacity_id: "your_capacity_id"  # Optional, required for --create-workspace\n\n'
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

        # Create workspace if requested
        if self.create_workspace:
            capacity_id = config.get("capacity_id")
            if not capacity_id:
                # Use default capacity (first active capacity)
                self.logger.info("No capacity_id specified, using default capacity")
                capacity_id = get_default_capacity_id()
                self.logger.info(f"Using capacity: {capacity_id}")

            self._create_workspace_if_needed(capacity_id)

        # Resolve workspace ID
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

    def _load_manual_parameters(self, detector: ParameterDetector) -> int:
        """
        Load manual parameter overrides from fabdep.yml and update auto-detected parameters.

        This allows users to override the replace_value for parameters that were auto-detected
        but reference external resources (e.g., SQL endpoint IDs from other workspaces).

        The find_value is still auto-detected from source files. Users can specify:
        - replace_value: Custom replacement value or keep external reference
        - item_type (optional): Scope replacement to specific item types (e.g., ["SemanticModel"])

        Args:
            detector: ParameterDetector instance with auto-detected parameters

        Returns:
            Number of manual parameter overrides applied
        """
        config_file = self._repo_dir_resolved / "fabdep.yml"
        if not config_file.exists():
            return 0

        with open(config_file) as f:
            config = yaml.safe_load(f)

        manual_params = config.get("manual_parameters", [])
        if not manual_params:
            return 0

        count = 0
        for param in manual_params:
            find_value = param.get("find_value")
            replace_value = param.get("replace_value")
            description = param.get("description")
            item_type = param.get("item_type")

            if not find_value or not replace_value:
                self.logger.warning(
                    f"Skipping manual parameter with missing find_value or replace_value"
                )
                continue

            # Check if this parameter was auto-detected
            if find_value in detector.parameters:
                # Override the dynamic variable with the manual replace_value
                param_obj = detector.parameters[find_value]

                # Handle both dict (with environment key) and string formats
                if isinstance(replace_value, dict):
                    # Extract the value from the environment-keyed dict
                    # (e.g., {env: "value"} -> "value")
                    replace_value = next(iter(replace_value.values()))

                param_obj.dynamic_variable = replace_value

                # Override item_types if specified in manual parameter
                if item_type:
                    if isinstance(item_type, list):
                        param_obj.item_types = set(item_type)
                    else:
                        param_obj.item_types = {item_type}
                    self.logger.debug(
                        f"Manual item_type override for {find_value}: {item_type}"
                    )

                if description:
                    param_obj.description = (
                        f"{param_obj.description} (manual override: {description})"
                    )
                param_obj.source_files.add("fabdep.yml (manual override)")
                count += 1
                self.logger.debug(f"Manual override for {find_value}: {replace_value}")

            else:
                # Parameter wasn't auto-detected, log warning
                self.logger.warning(
                    f"Manual parameter '{find_value}' was not auto-detected. "
                    "Make sure the find_value matches exactly what's in the source files."
                )

        # if count > 0:
        #     self.logger.info(
        #         f"Applied {count} manual parameter override(s) from fabdep.yml"
        #     )

        return count

    def _create_workspace_if_needed(self, capacity_id: str) -> None:
        """
        Create workspace if it doesn't exist.

        Args:
            capacity_id: Fabric capacity ID for workspace creation

        Logs:
            - Info if workspace already exists
            - Info when creating new workspace
            - Success message with workspace ID
        """
        # Check if workspace already exists
        if workspace_exists(self.workspace_name):
            self.logger.info(f"Workspace already exists: {self.workspace_name}")
            return

        # Create workspace
        self.logger.info(f"Creating workspace: {self.workspace_name}")
        try:
            workspace_id = create_workspace(
                workspace_name=self.workspace_name,
                capacity_id=capacity_id,
            )
            self.logger.info(f"✓ Workspace created: {workspace_id}")
        except Exception as e:
            self.logger.error(f"Failed to create workspace: {e}")
            raise

    def _generate_dynamic_params(self, item_types: list[str] | None = None) -> None:
        """
        Generate parameter file dynamically using auto-detection.

        Checks if source and target workspaces are the same - if so, skips generation.
        Creates parameter.yml and backs up existing parameter.yml if present.
        Optionally saves a copy to parameter.yml.detected if save_detected_params is True.

        Also merges manual_parameters from fabdep.yml if specified.

        Args:
            item_types: Filter parameters to only those used by specified item types
        """
        # Detect parameters (suppress logs, we'll show summary later)
        detector = ParameterDetector(
            repo_dir=self._repo_dir_resolved, logger_name=self.logger.name
        )
        _ = detector.detect_all()

        # Load manual parameters from fabdep.yml if specified
        manual_count = self._load_manual_parameters(detector)

        # Filter parameters based on item_types if specified
        # This prevents warnings about unused parameters when deploying only specific item types
        if item_types:
            filtered_params = {}
            for find_value, param in detector.parameters.items():
                # Include parameter if its item_types overlap with filtered types
                if param.item_types.intersection(set(item_types)):
                    filtered_params[find_value] = param
            detector.parameters = filtered_params

        # Check if source and target workspaces are the same
        if detector.should_skip_replacement(self.workspace_id):
            self._auto_detect_summary = [
                f"Source and target workspace are the same ({self.workspace_id})",
                "No parameter file will be used -- deploying directly without replacements",
            ]
            # Disable auto_detect so we don't try to clean up files later
            self.auto_detect = False
            return

        # Save detected parameters if requested
        if self.save_detected_params:
            detected_file = self._repo_dir_resolved / "parameter.yml.detected"
            detector.save_parameter_yml(
                output_path=detected_file,
                use_dynamic=True,
                environment="fabdep",
                skip_empty=True,
            )
            # Count what was saved
            with_dynamic = sum(
                1 for p in detector.parameters.values() if p.dynamic_variable
            )
            self.logger.info(
                f"Saved {with_dynamic} detected parameter(s) to {detected_file.name}"
            )

        # Count what was detected
        dyns = sum(1 for p in detector.parameters.values() if p.dynamic_variable)
        total = len(detector.parameters)
        connection_count = detector.count_connection_guids()

        # Check if we have any parameters to replace
        if dyns == 0 and manual_count == 0:
            # No parameters to replace - skip parameter.yml creation
            self._auto_detect_summary = [
                "No parameters detected for replacement",
                "Deploying without parameter.yml file",
            ]
            # Disable auto_detect so we don't try to clean up files later
            self.auto_detect = False
            return

        # Backup existing parameter.yml if it exists
        param_file = self._repo_dir_resolved / "parameter.yml"
        backup_file = self._repo_dir_resolved / "parameter.yml.backup"
        summary = []
        if param_file.exists():
            _ = param_file.rename(backup_file)
            summary.append(f"Backed up existing parameter.yml to {backup_file.name}")

        # Generate parameter file for deployment (using dynamic variables)
        detector.save_parameter_yml(
            output_path=param_file,
            use_dynamic=True,
            environment="fabdep",
            skip_empty=True,
        )

        # Store summary to display after header
        paran = (
            "" if dyns >= total else f" ({total - dyns} external connections skipped)"
        )
        plr = "" if dyns == 1 else "s"
        # Add manual parameter count if any
        if manual_count > 0:
            manual_plr = "" if manual_count == 1 else "s"
            man = f" + {manual_count} manual parameter{manual_plr}"
        else:
            man = ""
        summary.append(f"Using {dyns} dynamic parameter{plr}{paran}{man}")

        self._auto_detect_summary = summary

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

    def _get_repo_item_types(self) -> set[str]:
        """
        Scan repository to detect which item types are present.

        Returns:
            Set of item type names found in the repository
        """
        item_types = set()

        # Scan all directories in repo for .platform files
        for item_dir in self._repo_dir_resolved.iterdir():
            if not item_dir.is_dir():
                continue

            # Check if directory follows ItemName.ItemType pattern
            if "." in item_dir.name:
                parts = item_dir.name.rsplit(".", 1)
                if len(parts) == 2:
                    item_type = parts[1]
                    # Verify it has a .platform file
                    platform_file = item_dir / ".platform"
                    if platform_file.exists() and item_type in ACCEPTED_ITEM_TYPES:
                        item_types.add(item_type)

        return item_types

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

        # Detect which item types are present in repository
        repo_item_types = self._get_repo_item_types()

        # Build item_type_in_scope based on include/exclude flags
        item_type_in_scope = None
        if item_types:
            item_type_in_scope = item_types
        elif exclude_types:
            item_type_in_scope = [
                t for t in ACCEPTED_ITEM_TYPES if t not in exclude_types
            ]

        # Regenerate parameters with filtering if auto-detect is enabled and item filtering is active
        # This ensures only parameters relevant to the filtered item types are included,
        # preventing warnings from fabric-cicd about unused find_replace entries
        if self.auto_detect and item_type_in_scope:
            self._generate_dynamic_params(item_types=item_type_in_scope)

        # Print header with repo info
        self.logger.info(f"Workspace Name: '{self.workspace_name}'")
        self.logger.info(f"Workspace ID: {self.workspace_id}")
        # Print auto-detect summary if available (on same line as workspace info)
        if self._auto_detect_summary:
            for line in self._auto_detect_summary:
                self.logger.info(line)

        # self.logger.info(f"Lakehouse name: {self.lakehouse_name}")
        # self.logger.info(f"Input directory: {self._repo_dir_resolved}")
        if item_type_in_scope:
            # Only show item types that are both in scope AND present in repo
            types_to_deploy = [t for t in item_type_in_scope if t in repo_item_types]
            if types_to_deploy:
                self.logger.info(
                    f"Item types in scope: {', '.join(sorted(types_to_deploy))}"
                )
            else:
                self.logger.warning(f"No matching item types found in repository")
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

        try:
            # Bootstrap mode: two-phase deployment
            if self.bootstrap:
                self._deploy_bootstrap(
                    no_cleanup=no_cleanup,
                    item_types=item_types,
                    exclude_types=exclude_types,
                    include_items=include_items,
                )
            else:
                # Normal single-phase deployment
                self._deploy_items(
                    no_cleanup=no_cleanup,
                    item_types=item_type_in_scope,
                    include_items=include_items,
                )

            self.logger.info("Deployment complete!")

        except Exception:  # Re-raise exception to be handled by caller
            raise  # CLI will handle error message formatting via handle_command_error()
        finally:  # Cleanup auto-generated parameter file if in auto-detect mode
            if self.auto_detect:
                self._cleanup_dynamic_params()
            print()

    def _deploy_items(
        self,
        no_cleanup: bool = False,
        item_types: list[str] | None = None,
        include_items: list[str] | None = None,
    ) -> None:
        """
        Deploy items to workspace using fabric-cicd with retry logic.

        Args:
            no_cleanup: Skip unpublishing orphaned items
            item_types: Specific item types to deploy
            include_items: Include only specific items (format: "ItemName.ItemType")
        """
        max_retries = 3
        retry_delay = 2  # seconds

        target_workspace = FabricWorkspace(
            workspace_id=self.workspace_id,
            repository_directory=str(self._repo_dir_resolved),
            environment="fabdep",
            item_type_in_scope=item_types,
        )

        # Deploy with retry logic
        for attempt in range(1, max_retries + 1):
            try:
                _ = publish_all_items(target_workspace, items_to_include=include_items)
                break  # Success, exit retry loop
            except Exception as e:
                error_msg = str(e)
                # Check if this is a connection error that should be retried
                if (
                    "Connection aborted" in error_msg
                    or "RemoteDisconnected" in error_msg
                ):
                    if attempt < max_retries:
                        self.logger.warning(
                            f"Connection error on attempt {attempt}/{max_retries}: {error_msg}"
                        )
                        self.logger.info(f"Retrying in {retry_delay} seconds...")
                        sleep(retry_delay)
                    else:
                        self.logger.error(
                            f"Deployment failed after {max_retries} attempts: {error_msg}"
                        )
                        raise
                else:
                    # Not a connection error, don't retry
                    raise

        if not no_cleanup:
            unpublish_all_orphan_items(
                target_workspace, item_name_exclude_regex=self.lakehouse_name
            )

    def _deploy_bootstrap(
        self,
        no_cleanup: bool = False,
        item_types: list[str] | None = None,
        exclude_types: list[str] | None = None,
        include_items: list[str] | None = None,
    ) -> None:
        """
        Two-phase deployment: foundational items first, then all items.

        Phase 1: Deploy Lakehouse, SemanticModel, SQLDatabase, etc.
        Phase 2: Deploy everything (Notebook, DataPipeline, etc. can now reference Phase 1 items)

        Args:
            no_cleanup: Skip unpublishing orphaned items
            item_types: User-specified item types filter (overrides bootstrap logic)
            exclude_types: User-specified exclusion filter
            include_items: Include only specific items
        """
        # Phase 1: Foundational items
        self.logger.info("Bootstrap mode enabled - two-phase deployment")
        self.logger.info("Phase 1: Deploying foundational items")

        # Determine Phase 1 item types (respect user filters if provided)
        if item_types:
            # User provided explicit filter - deploy only matching foundational items
            phase1_types = [t for t in item_types if t in FOUNDATIONAL_ITEMS]
        elif exclude_types:
            # User excluded types - deploy foundational items not in exclusion list
            phase1_types = [t for t in FOUNDATIONAL_ITEMS if t not in exclude_types]
        else:
            # No filters - deploy all foundational items
            phase1_types = FOUNDATIONAL_ITEMS

        if phase1_types:
            # Only show foundational types that are actually in the repo
            repo_item_types = self._get_repo_item_types()
            types_in_repo = [t for t in phase1_types if t in repo_item_types]
            if types_in_repo:
                self.logger.info(
                    f"Foundational item types: {', '.join(sorted(types_in_repo))}"
                )
            self._deploy_items(
                no_cleanup=True,  # Don't cleanup in Phase 1
                item_types=phase1_types,
                include_items=include_items,
            )
            self.logger.info("✓ Phase 1 complete")
        else:
            self.logger.info("No foundational items to deploy")

        # Phase 2: All items
        self.logger.info("Phase 2: Deploying all items")

        # Determine Phase 2 item types (respect user filters)
        if item_types:
            phase2_types = item_types
        elif exclude_types:
            phase2_types = [t for t in ACCEPTED_ITEM_TYPES if t not in exclude_types]
        else:
            phase2_types = None  # Deploy everything

        if phase2_types:
            # Only show types that are actually in the repo
            repo_item_types = self._get_repo_item_types()
            types_in_repo = [t for t in phase2_types if t in repo_item_types]
            if types_in_repo:
                self.logger.info(f"Item types: {', '.join(sorted(types_in_repo))}")

        self._deploy_items(
            no_cleanup=no_cleanup,
            item_types=phase2_types,
            include_items=include_items,
        )
        self.logger.info("✓ Phase 2 complete")
