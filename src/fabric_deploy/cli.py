import argparse
import logging
import sys
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

from .logging import get_logger, print_header, print_error
from .auth import is_auth_error, format_auth_error_message, handle_auth_command


@dataclass
class FabricDeployer:
    """
    Main deployer class for Fabric workspace items.

    Encapsulates configuration extraction, validation, and deployment logic.
    Configuration is loaded lazily on first deploy() call.
    """

    repo_dir: Path | str
    logger_name: str | None = None

    # Configuration fields populated by _load_config()
    workspace_id: str | None = field(default=None, init=False)
    environment: str | None = field(default=None, init=False)
    lakehouse_name: str | None = field(default=None, init=False)

    # Private fields
    _repo_dir_resolved: Path = field(init=False, repr=False)
    logger: logging.Logger = field(init=False, repr=False)

    def __post_init__(self):
        """Initialize derived fields after dataclass initialization."""
        self._repo_dir_resolved = Path(self.repo_dir).resolve()
        self.logger = get_logger(self.logger_name or __name__)

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
                print()
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

        if len(lakehouses) > 1:
            self.logger.warning(
                f"Multiple lakehouses found: {[d.name for d in lakehouses]}"
            )

        try:
            platform_file = lakehouses[0] / ".platform"
            metadata = loads(platform_file.read_text())
            self.lakehouse_name = metadata["metadata"]["displayName"]
        except Exception as e:
            raise RuntimeError(f"Failed to read lakehouse metadata: {e}") from e

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
        # Load config if not already loaded
        if not self.workspace_id:
            self._load_config()

        if not self.lakehouse_name:
            self._detect_lakehouse()

        # Enable experimental feature flags if name-based filtering is requested
        if include_items:
            append_feature_flag("enable_experimental_features")
            append_feature_flag("enable_items_to_include")
            self.logger.warning(
                "Name-based item filtering is experimental. "
                "Selective deployment may cause dependency issues. Use at your own risk."
            )

        # Build item_type_in_scope based on include/exclude flags
        item_type_in_scope = None
        if item_types:
            item_type_in_scope = item_types
        elif exclude_types:
            item_type_in_scope = [
                t for t in ACCEPTED_ITEM_TYPES if t not in exclude_types
            ]

        print_header("Contoso  𐧾  Fabric CI/CD")
        self.logger.info(f"Lakehouse name: {self.lakehouse_name}")
        self.logger.info(f"Workspace ID: {self.workspace_id}")

        if item_type_in_scope:
            self.logger.info(f"Item types in scope: {', '.join(item_type_in_scope)}")

        if include_items:
            self.logger.info(f"Including items: {', '.join(include_items)}")

        self.logger.info(f"Input directory: {self._repo_dir_resolved}")

        # Display parameter.yml last modified time
        param_file = self._repo_dir_resolved / "parameter.yml"
        bak_file = param_file.with_suffix(".yml.bak")

        if bak_file.exists() and not param_file.exists():
            # Using .bak file
            mtime = datetime.fromtimestamp(bak_file.stat().st_mtime)
            self.logger.info(
                f"Parameter file (backup): {bak_file.name} - modified: {mtime.strftime('%d/%m/%Y %H:%M:%S')}"
            )
        elif param_file.exists():
            mtime = datetime.fromtimestamp(param_file.stat().st_mtime)
            self.logger.info(
                f"Parameter file modified: {mtime.strftime('%d/%m/%Y %H:%M:%S')}"
            )

        target_workspace = FabricWorkspace(
            workspace_id=self.workspace_id,
            repository_directory=str(self._repo_dir_resolved),
            item_type_in_scope=item_type_in_scope,
        )

        try:
            _ = publish_all_items(target_workspace, items_to_include=include_items)

            if not no_cleanup:
                if not self.lakehouse_name:
                    raise RuntimeError("Lakehouse name not detected for cleanup")
                unpublish_all_orphan_items(
                    target_workspace, item_name_exclude_regex=self.lakehouse_name
                )

            print()
            self.logger.info("Deployment complete!")
            print()

        except Exception as e:
            error_msg = str(e)
            if is_auth_error(error_msg):
                self._handle_auth_error()
            raise

    def _handle_auth_error(self) -> None:
        """Print helpful auth troubleshooting info."""
        error_msg = format_auth_error_message()
        print_error(error_msg)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="fabric-cicd",
        description="Deploy Fabric workspace items - auto-detects configuration from parameter.yml",
        epilog="Run from your Fabric workspace directory.",
    )

    # Create subparsers for commands
    subparsers = parser.add_subparsers(
        dest="command", help="Available commands (defaults to 'deploy')"
    )

    # Auth subcommand
    auth_parser = subparsers.add_parser("auth", help="Manage Azure authentication")
    auth_subparsers = auth_parser.add_subparsers(
        dest="auth_command", required=True, help="Auth actions"
    )

    _ = auth_subparsers.add_parser(
        "list", help="List current and available Azure accounts"
    )

    switch_parser = auth_subparsers.add_parser(
        "switch", help="Switch to different account"
    )
    _ = switch_parser.add_argument(
        "subscription_id", help="Subscription ID to switch to"
    )

    rename_parser = auth_subparsers.add_parser("rename", help="Rename a subscription")
    _ = rename_parser.add_argument("subscription_id", help="Subscription ID to rename")
    _ = rename_parser.add_argument("name", help="New subscription name")

    # Deploy subcommand (default)
    deploy_parser = subparsers.add_parser(
        "deploy",
        help="Deploy Fabric workspace items (default command)",
        description="Deploy Fabric workspace items - auto-detects configuration from parameter.yml",
    )
    _ = deploy_parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        type=Path,
        help="Workspace directory (defaults to current directory)",
    )
    _ = deploy_parser.add_argument(
        "--no-cleanup",
        "-n",
        action="store_true",
        help="Skip unpublishing orphaned items",
    )
    _ = deploy_parser.add_argument(
        "--item-types",
        "-t",
        nargs="+",
        help="Specific item types to deploy (e.g., Notebook DataPipeline)",
    )
    _ = deploy_parser.add_argument(
        "--exclude-types",
        "-x",
        nargs="+",
        help="Item types to exclude from deployment (e.g., Report SemanticModel)",
    )
    _ = deploy_parser.add_argument(
        "--include-items",
        "-i",
        nargs="+",
        help='Include only specific items (format: "ItemName.ItemType")',
    )

    # Check if we should inject 'deploy' as default subcommand
    # This happens when no subcommand is specified but deploy-specific flags are used
    argv = sys.argv[1:]
    if argv and not any(cmd in argv for cmd in ["auth", "deploy"]):
        # Don't inject deploy if only help flag is present (show main help instead)
        if argv == ["-h"] or argv == ["--help"]:
            pass
        else:
            # Check if any deploy-specific flags are present
            deploy_flags = {
                "-n",
                "--no-cleanup",
                "-t",
                "--item-types",
                "-x",
                "--exclude-types",
                "-i",
                "--include-items",
            }
            if any(arg in deploy_flags or not arg.startswith("-") for arg in argv):
                # Inject 'deploy' as the first argument
                sys.argv.insert(1, "deploy")

    args = parser.parse_args()

    # Default to deploy command if no command specified
    if args.command is None:
        args.command = "deploy"
        args.directory = Path(".")
        args.no_cleanup = False
        args.item_types = None
        args.exclude_types = None
        args.include_items = None

    return args


def main() -> None:
    """CLI entry point."""
    args = parse_args()

    # Handle auth subcommands
    if args.command == "auth":
        handle_auth_command(args)
        return

    # Handle deployment
    if args.command == "deploy":
        try:
            deployer = FabricDeployer(
                repo_dir=args.directory,
                logger_name="fabric_deploy.cli",
            )

            deployer.deploy(
                no_cleanup=args.no_cleanup,
                item_types=args.item_types,
                exclude_types=args.exclude_types,
                include_items=args.include_items,
            )
        except FileNotFoundError as e:
            print_error(f"\n{e}\n")
            print("Usage: fabric-cicd [deploy] [DIRECTORY] [OPTIONS]")
            print("Run 'fabric-cicd -h' for more information.\n")
            sys.exit(1)
    else:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
