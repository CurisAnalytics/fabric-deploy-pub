"""Workspace deployment abstraction for Microsoft Fabric workspaces."""

# pyright: reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false

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
from fabric_cicd._common._logging import log_header
from fabric_cicd.constants import ACCEPTED_ITEM_TYPES

from .api import (
    create_workspace,
    get_default_capacity_id,
    get_workspace_id_by_name,
)
from .detect import ParameterDetector
from .log import get_logger, is_transient_error, patch_fabric_cicd_logger


@dataclass
class WorkspaceDeployer:
    """
    Main deployer class for Fabric workspace items.
    Encapsulates configuration extraction, validation, and deployment logic.

    Attributes:
            repo_dir: Path to the repository directory containing Fabric workspace items
            logger_name: Optional custom logger name (defaults to module name)
            branch_override: Override git branch for workspace name resolution (defaults to current branch)
            new_workspace: Create workspace if it doesn't exist
            max_retries: Max retries for transient errors (default: 1)
            skip_lakehouse: Skip lakehouse parameter replacement (for feature branches sharing dev lakehouse)

    IMPORTANT: Initialization has side effects!
    - Reads fabdep.yml from disk
    - Makes network calls to Fabric API (for workspace ID resolution)
    - Scans workspace files to detect source IDs (stored, not written to disk)
    - Auto-detects lakehouse metadata

    Parameter file (parameter.yml) is written during deploy(), not during init.
    """

    repo_dir: Path | str
    logger_name: str | None = None
    branch_override: str | None = None  # Override git branch for workspace resolution
    new_workspace: bool = False  # Create workspace if it doesn't exist
    max_retries: int = 1  # Max retries for transient errors
    skip_lakehouse: bool = False  # Skip its parameter replacement (feature branch mode)

    # Configuration fields - auto-loaded in __post_init__
    workspace_id: str = field(init=False, repr=False)
    workspace_name: str = field(
        init=False, repr=False
    )  # Resolved workspace name (base_name [branch])
    lakehouse_name: str = field(init=False, repr=False)
    repo_item_types: set[str] = field(
        init=False, repr=False
    )  # Item types present in repo

    # Private fields
    _repo_dir_resolved: Path = field(init=False, repr=False)
    _auto_detect_summary: list[str] | None = field(default=None, init=False, repr=False)
    _logged_manual_warnings: set[str] = field(
        default_factory=set, init=False, repr=False
    )
    _created_param_file: bool = field(default=False, init=False, repr=False)
    _detector: ParameterDetector | None = field(default=None, init=False, repr=False)
    _skipped_lakehouse_count: int = field(default=0, init=False, repr=False)
    _manual_count: int = field(default=0, init=False, repr=False)
    logger: logging.Logger = field(init=False, repr=False)

    def __post_init__(self):
        """
        Initialize derived fields and auto-load configuration.

        WARNING: This method has side effects and performs I/O operations:
        1. Resolves repository directory path
        2. Initializes logger
        3. Reads fabdep.yml from disk
        4. Makes network call to Fabric REST API to resolve workspace ID
        5. Scans workspace files to detect source IDs (stored in memory, not written to disk)
        6. Scans for .Lakehouse directories and reads .platform metadata

        Parameter file writing is deferred to deploy() when the item scope is known.
        """
        self._repo_dir_resolved = Path(self.repo_dir).resolve()
        self.logger = get_logger(self.logger_name or __name__)

        # Always load workspace ID from fabdep.yml
        self._load_config()

        # Detect parameters (file written later in deploy() when item scope is known)
        self._detect_parameters()

        # Detect item types and lakehouse
        self.repo_item_types = self._get_repo_item_types()
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
        config: dict = {}  # pyright: ignore[reportMissingTypeArgument]

        # Check for fabdep.yml - if missing, try directory name as workspace_base_name
        if not config_file.exists():
            # Try using directory name as workspace_base_name (silent fallback)
            dir_name = self._repo_dir_resolved.name
            branch = self.branch_override or self._get_git_branch()
            test_workspace_name = f"{dir_name} [{branch}]"

            try:
                # Attempt to resolve workspace - if it works, use directory name
                test_workspace_id = get_workspace_id_by_name(test_workspace_name)
                # Success - use directory name as workspace_base_name
                self.workspace_name = test_workspace_name
                self.workspace_id = test_workspace_id
                self.logger.debug(
                    f"Using directory name '{dir_name}' as workspace_base_name"
                )
                return  # Config loaded successfully via fallback
            except ValueError:
                # Workspace not found with directory name - show helpful error
                raise FileNotFoundError(
                    f"fabdep.yml not found in {self._repo_dir_resolved}.\n\n"
                    "Create a fabdep.yml file with:\n"
                    '  workspace_base_name: "your_workspace"\n'
                    '  capacity_id: "your_capacity_id"  # Optional\n\n'
                    "The workspace will be resolved from: {base_name} [{branch}]\n\n"
                    "Note: This is separate from parameter.yml which is used by fabric-cicd."
                ) from None

        # Load workspace_base_name from fabdep.yml
        with open(config_file) as f:
            config = yaml.safe_load(f) or {}

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
        if self.new_workspace:
            capacity_id = config.get("capacity_id")
            if not capacity_id:
                # Use default capacity (first active capacity)
                print()
                self.logger.info("No capacity_id specified, using default capacity")
                capacity_id = get_default_capacity_id()
                self.logger.info(f"Using capacity: {capacity_id}")

            self._create_workspace_if_needed(capacity_id)

        # Resolve workspace ID
        self.workspace_id = get_workspace_id_by_name(self.workspace_name)

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
                    "Skipping manual parameter with missing find_value or replace_value"
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
                # Parameter wasn't auto-detected, log warning (only once)
                if find_value not in self._logged_manual_warnings:
                    self.logger.warning(
                        f"Manual parameter '{find_value}' was not auto-detected. "
                        "Make sure the find_value matches exactly what's in the source files."
                    )
                    self._logged_manual_warnings.add(find_value)

        return count

    def _create_workspace_if_needed(self, capacity_id: str) -> None:
        """
        Create workspace if it doesn't exist.

        Args:
            capacity_id: Fabric capacity ID for workspace creation

        Raises:
            ValueError: If workspace already exists (prevents accidental overwrites)
        """
        # Try to create workspace (will raise ValueError if already exists)
        self.logger.info(f"Creating workspace: {self.workspace_name}")
        try:
            workspace_id = create_workspace(
                workspace_name=self.workspace_name,
                capacity_id=capacity_id,
            )
            self.logger.info(f"✓ Workspace created: {workspace_id}")
        except ValueError as e:
            # Workspace already exists - fail fast to prevent accidental reuse
            if "already exists" in str(e):
                raise ValueError(
                    f"Workspace '{self.workspace_name}' already exists.\n\n"
                    "The -N/--new-workspace flag is only for creating new workspaces.\n"
                    "To deploy to an existing workspace, remove the -N flag."
                ) from None
            else:
                raise
        except Exception as e:
            self.logger.error(f"Failed to create workspace: {e}")
            raise

    def _detect_parameters(self) -> None:
        """
        Detect parameters from workspace files and store for later use.

        Runs during __post_init__. Detection and storage only — no file I/O.
        The parameter file is written later in _write_parameter_file() during deploy().
        """
        detector = ParameterDetector(
            repo_dir=self._repo_dir_resolved, logger_name=self.logger.name
        )
        detector.detect_all()

        # Filter out lakehouse parameters if skip_lakehouse is enabled (feature branch mode)
        self._skipped_lakehouse_count = 0
        if self.skip_lakehouse:
            removed = detector.filter_lakehouse_params()
            self._skipped_lakehouse_count = len(removed)

        # Load manual parameters from fabdep.yml if specified
        self._manual_count = self._load_manual_parameters(detector)

        self._detector = detector

    def _write_parameter_file(self, item_types: list[str] | None = None) -> None:
        """
        Filter detected parameters and write parameter.yml for deployment.

        Called once from deploy() when the item scope is known.

        Args:
            item_types: Filter parameters to only those used by specified item types
        """
        detector = self._detector
        if detector is None:
            return

        # Filter parameters based on item_types if specified
        # This prevents warnings about unused parameters when deploying only specific item types
        if item_types:
            scope = set(item_types)
            filtered_params = {}
            for find_value, param in detector.parameters.items():
                # Always include Workspace ID - it applies to all item types
                if param.description == "Workspace ID":
                    filtered_params[find_value] = param
                # Include parameter if its item_types overlap with filtered types
                elif param.item_types & scope:
                    # Narrow item_types to only those in scope so fabric-cicd
                    # doesn't warn about out-of-scope types in the parameter file
                    param.item_types = param.item_types & scope
                    filtered_params[find_value] = param
            detector.parameters = filtered_params

        # Check if source and target workspaces are the same
        if detector.should_skip_replacement(self.workspace_id):
            self._auto_detect_summary = [
                "Source and target workspace are the same -- deploying directly without replacements",
            ]
            return

        # Count what was detected (after filtering)
        dyns = sum(1 for p in detector.parameters.values() if p.dynamic_variable)
        total = len(detector.parameters)

        # Check if we have any parameters that will actually be written to the file
        # When skip_empty=True, only parameters with dynamic variables are written
        if dyns == 0:
            if self._skipped_lakehouse_count > 0:
                self._auto_detect_summary = [
                    f"Skipped {self._skipped_lakehouse_count} lakehouse parameter{'s' if self._skipped_lakehouse_count != 1 else ''} (--skip-lakehouse)",
                    "Deploying without parameter.yml file",
                ]
            else:
                self._auto_detect_summary = [
                    "No parameters detected for replacement",
                    "Deploying without parameter.yml file",
                ]
            return

        # Backup existing parameter.yml if present
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
        self._created_param_file = True

        # Post-process parameter file to add domain substitution for .database connections
        self._add_domain_substitutions(param_file, detector)

        # Store summary to display after header
        external_count = max(total - dyns, 0)
        external_part = (
            f" + {external_count} external connection{'s' if external_count != 1 else ''}"
            if external_count
            else ""
        )
        manual_part = (
            f" + {self._manual_count} manual parameter{'s' if self._manual_count != 1 else ''}"
            if self._manual_count
            else ""
        )
        summary.append(
            f"Found {dyns} dynamic parameter{'s' if dyns != 1 else ''}{external_part}{manual_part}"
        )
        if self._skipped_lakehouse_count > 0:
            summary.append(
                f"Skipped {self._skipped_lakehouse_count} lakehouse parameter{'s' if self._skipped_lakehouse_count != 1 else ''} (--skip-lakehouse)"
            )

        self._auto_detect_summary = summary

    def _add_domain_substitutions(
        self, param_file: Path, detector: ParameterDetector
    ) -> None:
        """
        Post-process parameter file to add domain substitution for non-default connections.

        Handles two cases:
        1. .database.fabric.microsoft.com: Changes .datawarehouse back to .database
        2. .pbidedicated.windows.net: Changes .fabric.microsoft.com back to .pbidedicated.windows.net

        All substitutions are targeted using database identifiers to avoid affecting
        other connections.

        Args:
            param_file: Path to parameter.yml file
            detector: ParameterDetector with detected parameters
        """
        # Find parameters that need domain substitution
        connections_needing_substitution = []

        for param in detector.parameters.values():
            domain_suffix = param.metadata.get("domain_suffix", "").lower()
            domain_type = param.metadata.get("domain_type", "").lower()
            db_name = param.metadata.get("db_name")

            # Skip parameters without database name (can't create targeted replacement)
            if not db_name:
                continue

            # .database connections that need domain type change
            if domain_type == "database":
                connections_needing_substitution.append(("database_type", param))
            # .pbidedicated.windows.net connections that need domain suffix change
            elif domain_suffix == "pbidedicated.windows.net":
                connections_needing_substitution.append(("pbidedicated_suffix", param))

        if not connections_needing_substitution:
            return  # No connections need domain substitution

        # Read the existing parameter.yml
        with open(param_file) as f:
            param_data = yaml.safe_load(f)

        if not param_data or "find_replace" not in param_data:
            return

        # Add targeted domain substitution entries
        for substitution_type, param in connections_needing_substitution:
            db_name = param.metadata.get("db_name")
            if not db_name:
                continue  # Can't create targeted replacement without database name

            if substitution_type == "database_type":
                # For .database connections, change .datawarehouse back to .database
                targeted_find = f'.datawarehouse.fabric.microsoft.com","{db_name}'
                targeted_replace = f'.database.fabric.microsoft.com","{db_name}'

            elif substitution_type == "pbidedicated_suffix":
                # For .pbidedicated.windows.net connections, change .fabric.microsoft.com back to .pbidedicated.windows.net
                domain_type = param.metadata.get("domain_type", "datawarehouse")
                targeted_find = f'.{domain_type}.fabric.microsoft.com","{db_name}'
                targeted_replace = (
                    f'.{domain_type}.pbidedicated.windows.net","{db_name}'
                )

            else:
                continue

            domain_substitution = {
                "find_value": targeted_find,
                "replace_value": {"fabdep": targeted_replace},
                "item_type": ["SemanticModel"],
            }

            param_data["find_replace"].append(domain_substitution)

        # Write back the modified parameter file
        with open(param_file, "w") as f:
            yaml.dump(param_data, f, default_flow_style=False, sort_keys=False)

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

        Scans recursively to find all .platform files and extracts item types
        from their parent directory names.

        Returns:
            Set of item type names found in the repository
        """
        item_types = set()

        # Recursively scan for .platform files
        for platform_file in self._repo_dir_resolved.rglob(".platform"):
            item_dir = platform_file.parent

            # Check if directory follows ItemName.ItemType pattern
            if "." in item_dir.name:
                parts = item_dir.name.rsplit(".", 1)
                if len(parts) == 2:
                    item_type = parts[1]
                    if item_type in ACCEPTED_ITEM_TYPES:
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

        from . import __version__

        log_header(self.logger, f"Fabric CI/CD v{__version__}")

        # Enable experimental feature flags if name-based filtering is requested
        if include_items:
            append_feature_flag("enable_experimental_features")
            append_feature_flag("enable_items_to_include")
            self.logger.warning(
                "Name-based item filtering is experimental. "
                "Selective deployment may cause dependency issues. Use at your own risk."
            )
            self.logger.warning(
                "Orphan cleanup disabled when using --include-items for safety."
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

        # Auto-derive type filter from include_items (Name.Type format)
        if include_items and not item_type_in_scope:
            derived_types = set()
            for item in include_items:
                if "." in item:
                    item_type = item.rsplit(".", 1)[1]
                    if item_type in ACCEPTED_ITEM_TYPES:
                        derived_types.add(item_type)
            if derived_types:
                item_type_in_scope = sorted(derived_types)

        # Write parameter file (with item-type filtering if applicable)
        self._write_parameter_file(item_types=item_type_in_scope)

        # Print header with repo info
        self.logger.info(f"Workspace Name: '{self.workspace_name}'")
        self.logger.info(f"Workspace ID: {self.workspace_id}")
        # Print auto-detect summary if available (on same line as workspace info)
        if self._auto_detect_summary:
            for line in self._auto_detect_summary:
                self.logger.info(line)

        if item_type_in_scope:
            # Show item types in scope, filtered by what exists in repo
            types_to_deploy = sorted(set(item_type_in_scope) & self.repo_item_types)
            if types_to_deploy:
                self.logger.info(f"Item types in scope: {', '.join(types_to_deploy)}")
            else:
                self.logger.warning("No matching item types found in repository")
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

        # Replace fabric_cicd's log formatter to omit timestamps on header lines
        patch_fabric_cicd_logger()

        try:
            self._deploy_items(
                no_cleanup=no_cleanup,
                item_types=item_type_in_scope,
                include_items=include_items,
            )
            self.logger.info("Deployment complete!")

        except Exception:  # Re-raise exception to be handled by caller
            raise  # CLI will handle error message formatting via handle_command_error()
        finally:  # Cleanup auto-generated parameter file
            if self._created_param_file:
                self._cleanup_dynamic_params()
            print()

    def _deploy_items(
        self,
        no_cleanup: bool = False,
        item_types: list[str] | None = None,
        include_items: list[str] | None = None,
        _retry_attempt: int = 0,
    ) -> None:
        """
        Deploy items to workspace using fabric-cicd with retry logic.

        Args:
            no_cleanup: Skip unpublishing orphaned items
            item_types: Specific item types to deploy
            include_items: Include only specific items (format: "ItemName.ItemType")
            _retry_attempt: Internal retry counter (do not set manually)
        """
        target_workspace = FabricWorkspace(
            workspace_id=self.workspace_id,
            repository_directory=str(self._repo_dir_resolved),
            environment="fabdep",
            item_type_in_scope=item_types,
        )

        try:
            _ = publish_all_items(target_workspace, items_to_include=include_items)
        except Exception as e:
            error_msg = str(e)
            # Retry on transient errors (up to max_retries)
            if _retry_attempt < self.max_retries and is_transient_error(error_msg):
                self.logger.warning(f"Transient error detected: {error_msg}")
                self.logger.info(
                    f"Retrying deployment (attempt {_retry_attempt + 2}/{self.max_retries + 1})..."
                )
                return self._deploy_items(
                    no_cleanup=no_cleanup,
                    item_types=item_types,
                    include_items=include_items,
                    _retry_attempt=_retry_attempt + 1,
                )
            raise  # Re-raise if not retryable or max retries exceeded

        # Skip unpublish when using include_items - it's too dangerous
        # If item names don't match, nothing gets published, and unpublish would delete everything
        if not no_cleanup and not include_items:
            unpublish_all_orphan_items(
                target_workspace, item_name_exclude_regex=self.lakehouse_name
            )
