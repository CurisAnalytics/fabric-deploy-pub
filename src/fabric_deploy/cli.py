"""CLI interface for fabric-deploy package."""

import argparse
import sys
from pathlib import Path

from .auth import handle_auth_command
from .logging import print_error
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
    # Handle subcommands
    if args.command == "auth":
        handle_auth_command(args)
        return
    elif args.command == "deploy":
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
        deployer.deploy(
            no_cleanup=args.no_cleanup,
            item_types=args.item_types,
            exclude_types=args.exclude_types,
            include_items=args.include_items,
        )
    else:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        sys.exit(1)
