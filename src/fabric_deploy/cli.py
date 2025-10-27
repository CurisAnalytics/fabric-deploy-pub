"""CLI interface for fabric-deploy package."""

import argparse
import sys
from pathlib import Path

from .auth import (
    get_current_account,
    list_accounts,
    rename_subscription,
    switch_account,
)
from .logging import format_account_list, print_error
from .rename import rename_items_by_display_name
from .workspace import WorkspaceDeployer


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="fabdep",
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

    # Rename subcommand
    rename_items_parser = subparsers.add_parser(
        "rename",
        help="Rename workspace item directories to match display names",
        description="Rename workspace item directories based on their .platform displayName metadata",
    )
    _ = rename_items_parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        type=Path,
        help="Workspace directory (defaults to current directory)",
    )
    _ = rename_items_parser.add_argument(
        "--execute",
        "-e",
        action="store_true",
        help="Actually perform renames (default is dry-run)",
    )
    _ = rename_items_parser.add_argument(
        "--clean",
        "-c",
        action="store_true",
        help="Apply .lower().replace(' ', '_') to directory names",
    )

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
    if argv and not any(cmd in argv for cmd in ["auth", "deploy", "rename"]):
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


def handle_auth_command(args: argparse.Namespace) -> None:
    """Handle auth subcommand execution."""
    if args.auth_command == "list":
        # List current and all available accounts
        try:
            current_account = get_current_account()
            all_accounts = list_accounts()

            output = format_account_list(
                all_accounts, current_account["id"] if current_account else None
            )
            print(output)
        except Exception as e:
            print_error(f"Failed to list accounts: {e}")
            sys.exit(1)

    elif args.auth_command == "switch":
        # Switch to different subscription
        try:
            switch_account(args.subscription_id)
            print(f"\n✓ Switched to subscription: {args.subscription_id}\n")
        except Exception as e:
            print_error(f"\nFailed to switch account: {e}\n")
            sys.exit(1)

    elif args.auth_command == "rename":
        # Rename subscription (auto-switches if needed)
        try:
            switched = rename_subscription(args.subscription_id, args.name)
            if switched:
                print(
                    f"\n✓ Switched to and renamed subscription to: {args.name} ({args.subscription_id})\n"
                )
            else:
                print(
                    f"\n✓ Renamed subscription to: {args.name} ({args.subscription_id})\n"
                )
        except Exception as e:
            print_error(f"\nFailed to rename subscription: {e}\n")
            sys.exit(1)


def handle_rename_command(args: argparse.Namespace) -> None:
    """Handle rename subcommand execution."""
    try:
        rename_items_by_display_name(
            repo_dir=args.directory,
            dry_run=not args.execute,
            clean=args.clean,
        )
    except FileNotFoundError as e:
        print_error(f"\n{e}\n")
        print("Usage: fabdep rename [DIRECTORY] [OPTIONS]")
        print("Run 'fabdep rename -h' for more information.\n")
        sys.exit(1)
    except Exception as e:
        print_error(f"\nRename failed: {e}\n")
        sys.exit(1)


def handle_deploy_command(args: argparse.Namespace) -> None:
    """Handle deploy subcommand execution."""
    try:
        deployer = WorkspaceDeployer(
            repo_dir=args.directory,
            logger_name="fabric_deploy.cli",
        )
    except FileNotFoundError as e:
        print_error(f"\n{e}\n")
        print("Usage: fabdep [OPTIONS]")
        print("Run 'fabdep -h' for more information.\n")
        sys.exit(1)

    try:
        deployer.deploy(
            no_cleanup=args.no_cleanup,
            item_types=args.item_types,
            exclude_types=args.exclude_types,
            include_items=args.include_items,
        )
    except Exception as e:
        print_error(f"Deployment failed: {e}\n")
        sys.exit(1)


def main() -> None:
    """CLI entry point - routes to appropriate command handler."""
    args = parse_args()

    # Route to appropriate handler
    if args.command == "auth":
        handle_auth_command(args)
    elif args.command == "rename":
        handle_rename_command(args)
    elif args.command == "deploy":
        handle_deploy_command(args)
    else:
        print_error(f"Unknown command: {args.command}")
        sys.exit(1)
