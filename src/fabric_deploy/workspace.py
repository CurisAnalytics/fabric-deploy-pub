"""Workspace deployment abstraction for Microsoft Fabric workspaces."""

import logging
import yaml
from dataclasses import dataclass, field
from datetime import datetime
from json import loads
from pathlib import Path

from fabric_cicd import (
    FabricWorkspace,
    publish_all_items,
    unpublish_all_orphan_items,
    append_feature_flag,
)
from fabric_cicd.constants import ACCEPTED_ITEM_TYPES

from .auth import list_accounts, get_current_account
from .logging import get_logger, print_header, print_error, format_auth_error_message


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

    # Private fields
    _repo_dir_resolved: Path = field(init=False, repr=False)
    logger: logging.Logger = field(init=False, repr=False)

    def __post_init__(self):
        """Initialize derived fields and auto-load config if not provided."""
        self._repo_dir_resolved = Path(self.repo_dir).resolve()
        self.logger = get_logger(self.logger_name or __name__)

        # Auto-load configuration if not provided by user
        if self.workspace_id is None or self.environment is None:
            self._load_config()
        if self.lakehouse_name is None:
            self._detect_lakehouse()

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

        # Validate parameter file and handle invalid cases (only if it's not already .bak)
        if param_file.suffix != ".bak":
            self._validate_and_handle_parameter_file(param_file)
            # If file was renamed to .bak, update param_file reference
            if bak_file.exists() and not param_file.exists():
                param_file = bak_file

        workspace_id, environment = self._extract_config_from_parameters(param_file)
        self.workspace_id = workspace_id
        self.environment = environment

    def _validate_and_handle_parameter_file(self, param_file: Path) -> None:
        """
        Validate parameter file and rename to .bak if invalid.

        Detects invalid scenarios:
        - File already has .bak extension
        - Duplicate find_value entries
        - Blank/empty destination values in replace_value

        Args:
            param_file: Path to parameter.yml
        """
        # Check if already a .bak file
        if param_file.suffix == ".bak":
            self.logger.warning(
                f"Parameter file already has .bak extension: {param_file.name}"
            )
            return

        try:
            with open(param_file) as f:
                params = yaml.safe_load(f)
        except Exception as e:
            self.logger.error(f"Failed to parse parameter file: {e}")
            return

        find_replace = params.get("find_replace", [])
        if not find_replace:
            return

        is_invalid = False
        reasons: list[str] = []

        # Check for duplicate find_value entries
        find_values: list[str] = []
        for entry in find_replace:
            find_val = entry.get("find_value", "")
            if find_val in find_values:
                is_invalid = True
                reasons.append(f"Duplicate find_value: '{find_val}'")
            find_values.append(find_val)

        # Check for blank destination values
        for i, entry in enumerate(find_replace):
            replace_values = entry.get("replace_value", {})
            for env, value in replace_values.items():
                if value == "" or value is None:
                    is_invalid = True
                    reasons.append(
                        f"Entry {i + 1}: Blank destination value for environment '{env}'"
                    )

        # If invalid, rename to .bak
        if is_invalid:
            bak_file = param_file.with_suffix(".yml.bak")
            print()
            self.logger.warning("Invalid parameter file detected:")
            for reason in reasons:
                self.logger.warning(f"  - {reason}")

            _ = param_file.rename(bak_file)
            self.logger.warning(
                f"Renamed parameter file: {param_file.name} -> {bak_file.name}"
            )
            print()
            self.logger.info(
                "Deployment will proceed using workspace ID from first entry only"
            )

    def _extract_config_from_parameters(self, param_file: Path) -> tuple[str, str]:
        """
        Extract workspace ID and environment from parameter.yml.

        Reads the first entry's replace_value to get destination workspace ID.
        Validates that all entries use consistent environment keys.

        Args:
            param_file: Path to parameter.yml

        Returns:
            Tuple of (workspace_id, environment)
        """
        with open(param_file) as f:
            params = yaml.safe_load(f)

        find_replace = params.get("find_replace", [])
        if not find_replace:
            raise ValueError("No find_replace entries found in parameter.yml")

        first_entry = find_replace[0]
        replace_values = first_entry.get("replace_value", {})

        available_envs = list(replace_values.keys())
        if not available_envs:
            raise ValueError("No environments found in parameter.yml")

        # Validate all entries use the same environment
        for entry in find_replace:
            entry_envs = list(entry.get("replace_value", {}).keys())
            if set(entry_envs) != set(available_envs):
                raise ValueError(
                    f"Inconsistent environments across parameter.yml entries. "
                    f"Expected {available_envs}, found {entry_envs}"
                )

        environment = available_envs[0]
        workspace_id = replace_values[environment]

        if not workspace_id:
            raise ValueError(
                f"No workspace ID specified for environment '{environment}'"
            )

        return workspace_id, environment

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

        except Exception as e:
            indicators = ("not found", "unauthorized", "forbidden", "401", "403", "404")
            if any(i in str(e).lower() for i in indicators):
                try:
                    current = get_current_account()
                    accounts = list_accounts()
                    error_msg = format_auth_error_message(current, accounts)
                except Exception:
                    # Fallback if account fetching fails
                    error_msg = format_auth_error_message()
                print_error(error_msg)
            raise
