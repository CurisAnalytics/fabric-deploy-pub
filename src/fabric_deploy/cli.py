import argparse
import sys
import yaml
from json import loads
from pathlib import Path
from typing import final

from fabric_cicd import (
    FabricWorkspace,
    publish_all_items,
    unpublish_all_orphan_items,
    append_feature_flag,
)
from fabric_cicd.constants import ACCEPTED_ITEM_TYPES  # CopyJob, Noteook, Report, &c

from .logging import get_logger, print_header, print_error
from .auth import list_accounts, get_current_account, switch_account, is_auth_error


@final
class FabricDeployer:
    """
    Main deployer class for Fabric workspace items.

    Encapsulates configuration extraction, validation, and deployment logic
    for programmatic usage with proper logging namespace.
    """

    def __init__(
        self,
        repo_dir: Path | str,
        logger_name: str | None = None,
        workspace_id: str | None = None,
        lakehouse_name: str | None = None,
    ):
        self.repo_dir = Path(repo_dir).resolve()
        self.workspace_id = workspace_id
        self.lakehouse_name = lakehouse_name
        self.logger = get_logger(logger_name or __name__)
        self.workspace_id = workspace_id if workspace_id else self._load_config()
        self.lakehouse_name = (
            lakehouse_name if lakehouse_name else self._detect_lakehouse()
        )

    def _load_config(self) -> str:
        """Load and validate workspace configuration from parameter.yml."""
        if not self.repo_dir.exists():
            raise FileNotFoundError(f"Directory does not exist: {self.repo_dir}")
        param_file = self.repo_dir / "parameter.yml"
        if not param_file.exists():
            raise FileNotFoundError(
                f"parameter.yml not found in {self.repo_dir}. "
                "Make sure you're running this from a Fabric workspace directory"
            )
        workspace_id, _ = self._extract_config_from_parameters(param_file)
        return workspace_id

    def _extract_config_from_parameters(self, param_file: Path) -> tuple[str, str]:
        """
        Extract workspace ID and environment from parameter.yml.
        Reads the first entry's replace_value to get destination workspace ID.
        Environment is determined by the keys present (prod, dev, env).
        Args: param_file: Path to parameter.yml
        Returns: Tuple of (workspace_id, environment)
        """
        with open(param_file) as f:
            params = yaml.safe_load(f)
        first_entry = params.get("find_replace", [])[0]
        replace_values = first_entry.get("replace_value", {})
        # Get available environments from the replace_value keys
        available_envs = list(replace_values.keys())
        if not available_envs:
            raise ValueError("No environments found in parameter.yml")
        # Assert that all available environments are the same
        if len(set(available_envs)) > 1:
            raise ValueError(
                f"Multiple different environments found in parameter.yml: {available_envs}"
            )
        environment = available_envs[0]
        # Get the destination workspace ID for this environment
        workspace_id = replace_values[environment]
        if not workspace_id:
            raise ValueError(
                f"No workspace ID specified for environment '{environment}'"
            )
        return workspace_id, environment

    def _detect_lakehouse(self) -> str:
        """
        Detect lakehouse name from .Lakehouse directory.

        Returns:
            Lakehouse display name from metadata
        """
        lakehouses = list(self.repo_dir.glob("*.Lakehouse"))
        if len(lakehouses) == 0:
            raise ValueError("No lakehouse found in repository directory")
        elif len(lakehouses) > 1:
            self.logger.warning(
                f"Multiple lakehouses found: {[d.name for d in lakehouses]}"
            )
        try:
            platform_file = lakehouses[0] / ".platform"
            metadata = loads(platform_file.read_text())
            lakehouse_name: str = metadata["metadata"]["displayName"]
        except Exception as e:
            raise RuntimeError(f"Failed to read lakehouse metadata: {e}") from e
        return lakehouse_name

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
            # All supported types minus excluded ones
            item_type_in_scope = [
                t for t in ACCEPTED_ITEM_TYPES if t not in exclude_types
            ]

        print_header("Contoso  𐧾  Fabric CI/CD")  # \udb85\uddfc
        self.logger.info(f"Detected lakehouse name: {self.lakehouse_name}")
        self.logger.info(f"Workspace ID: {self.workspace_id}")
        if item_type_in_scope:
            self.logger.info(f"Item types in scope: {', '.join(item_type_in_scope)}")
        else:
            self.logger.info("Item types in scope: All supported types")
        if include_items:
            self.logger.info(f"Including items: {', '.join(include_items)}")

        target_workspace = FabricWorkspace(
            workspace_id=self.workspace_id,
            repository_directory=str(self.repo_dir),
            item_type_in_scope=item_type_in_scope,
        )

        try:
            _ = publish_all_items(target_workspace, items_to_include=include_items)

            if not no_cleanup:
                assert self.lakehouse_name is not None
                unpublish_all_orphan_items(
                    target_workspace, item_name_exclude_regex=self.lakehouse_name
                )

            print()
            self.logger.info("Deployment complete!")
        except Exception as e:
            error_msg = str(e)
            if is_auth_error(error_msg):
                self._handle_auth_error()
            raise

    def _handle_auth_error(self) -> None:
        """Print helpful auth troubleshooting info."""
        print_error("\n" + "=" * 80)
        self.logger.error("Authentication/Authorization Error Detected")
        print_error("=" * 80)

        try:
            current = get_current_account()
            if current:
                print()
                self.logger.info("Current account:")
                self.logger.info(f"\tUser: {current['user']['name']}")
                self.logger.info(f"\tTenant: {current['tenantId']}")
                self.logger.info(f"\tSubscription: {current['name']}")

            accounts = list_accounts()
            if len(accounts) > 1:
                print()
                self.logger.info("Available accounts:")
                for acct in accounts:
                    marker = " [CURRENT]" if acct.get("isDefault") else ""
                    self.logger.info(f"  • {acct['name']}{marker}")
                    self.logger.info(f"    User: {acct['user']['name']}")
                    self.logger.info(f"    Subscription: {acct['id']}")

                print()
                self.logger.info("To switch accounts:")
                self.logger.info("  fabric-cicd auth switch <subscription-id>")
        except Exception:
            pass

        print()
        self.logger.info("Common causes:")
        self.logger.info(
            "  • Wrong tenant/subscription (workspace may be in different org)"
        )
        self.logger.info("  • Expired token (run: az login)")
        print_error(f"\n{'=' * 80}\n")


def parse_args():
    """Parse command-line arguments."""
    # Check if first arg is a known subcommand; if not, inject 'deploy'
    if len(sys.argv) > 1 and sys.argv[1] not in ("auth", "deploy", "-h", "--help"):
        sys.argv.insert(1, "deploy")

    parser = argparse.ArgumentParser(
        prog="fabric-cicd",
        description="Deploy Fabric workspace items - auto-detects configuration from parameter.yml",
        epilog="Run from your Fabric workspace directory.",
    )

    # Create subparsers for commands
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Auth subcommand
    auth_parser = subparsers.add_parser("auth", help="Manage Azure authentication")
    auth_subparsers = auth_parser.add_subparsers(
        dest="auth_command", required=True, help="Auth actions"
    )
    _ = auth_subparsers.add_parser("list", help="List available Azure accounts")
    _ = auth_subparsers.add_parser("current", help="Show current account")
    switch_parser = auth_subparsers.add_parser(
        "switch", help="Switch to different account"
    )
    _ = switch_parser.add_argument(
        "subscription_id", help="Subscription ID to switch to"
    )

    # Deploy subcommand (default when no subcommand given)
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
        help="Specific item types to deploy (e.g., Notebook DataPipeline Environment)",
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
        help='Include only specific items (format: "ItemName.ItemType", e.g., "MyNotebook.Notebook")',
    )

    # Parse args - default command injection already handled above
    args = parser.parse_args()

    # If still no command (empty argv), default to deploy with current directory
    if args.command is None:
        sys.argv.insert(1, "deploy")
        args = parser.parse_args()

    return args


def handle_auth_command(args):
    """Handle auth subcommands."""
    if args.auth_command == "list":
        accounts = list_accounts()
        print(f"\nAvailable accounts ({len(accounts)}):\n")
        for acct in accounts:
            marker = " [CURRENT]" if acct.get("isDefault") else ""
            print(f"{acct['name']}{marker}")
            print(f"  User: {acct['user']['name']}")
            print(f"  Tenant: {acct['tenantId']}")
            print(f"  Subscription: {acct['id']}\n")

    elif args.auth_command == "current":
        current = get_current_account()
        if current:
            print("\nCurrent account:")
            print(f"  Name: {current['name']}")
            print(f"  User: {current['user']['name']}")
            print(f"  Tenant: {current['tenantId']}")
            print(f"  Subscription: {current['id']}\n")
        else:
            print("\nNo default account set.\n")

    elif args.auth_command == "switch":
        switch_account(args.subscription_id)
        print(f"\nSwitched to subscription: {args.subscription_id}\n")


def main():
    """CLI entry point."""
    args = parse_args()

    # Handle auth subcommands
    if args.command == "auth":
        handle_auth_command(args)
        return

    # Handle deployment
    if args.command == "deploy":
        deployer = FabricDeployer(
            repo_dir=args.directory, logger_name="fabric_deploy.cli"
        )

        deployer.deploy(
            no_cleanup=args.no_cleanup,
            item_types=args.item_types,
            exclude_types=args.exclude_types,
            include_items=args.include_items,
        )
    else:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
