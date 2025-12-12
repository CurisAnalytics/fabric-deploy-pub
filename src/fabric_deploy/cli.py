"""CLI interface for fabric-deploy package."""

import argparse
import sys
from pathlib import Path
from textwrap import dedent

from .auth import (
    get_current_account,
    list_accounts,
    rename_subscription,
    switch_account,
)
from .detect import ParameterDetector
from .log import (
    SEP,
    format_account_list,
    format_auth_error_message,
    is_auth_error,
    is_connection_error,
    print_error,
)
from .rename import rename_items_by_display_name
from .workspace import WorkspaceDeployer


def handle_command_error(error: Exception, context: str) -> None:
    """Handle command errors with specific troubleshooting for different error types.

    Args:
        error: The exception that was raised
        context: Context string describing what failed (e.g., "Workspace operation", "Deployment")
    """
    error_msg = str(error)
    print_error(f"\n{context} failed: {error_msg}\n")

    # Check for connection errors first (before auth errors, as they can contain auth keywords)
    if is_connection_error(error_msg):
        connection_help = dedent(
            f"""{SEP}
            Connection Reference Error Detected
            {SEP}

            This error occurs when a DataPipeline (or other item) references a connection
            \tthat doesn't exist in the target tenant.

            Root Cause:
            \t• Connection GUIDs are TENANT-WIDE (not workspace-specific)
            \t• Your workspace repo contains connection GUIDs from a different tenant
            \t• This indicates the workspace repo is misconfigured for the target tenant

            Solution:
            \t• Identify the correct connections between the repo and the Fabric UI
            \t• If possible/advisable, fix the connections in the Fabric UI and push changes
            \t• Otherwise edit the pipeline files in your workspace repository directly
            \t\t◦ Update connection GUIDs to match the target tenant's connections
            \t\t◦ Commit the updated files to your repository
            \t\t◦ Re-run deployment

            Notes:
            \t• DO NOT use parameter.yml find/replace for connection GUIDs
            \t• Connections must exist in the target tenant for a DataPipeline to be deployed
            \t• Use 'fabdep detect' to find connection GUIDs in your current repo

            Common locations for connection references:
            \t• DataPipeline: pipeline-content.json files
            \t• Dataflow: dataflow configuration files

            {SEP}
            """
        )
        print_error(connection_help)
    # Show friendly auth error message if this is an auth issue
    elif is_auth_error(error_msg):
        current = get_current_account()
        all_accounts = list_accounts()
        auth_help = format_auth_error_message(current, all_accounts)
        print_error(auth_help)

    sys.exit(1)


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

    # Detect subcommand
    detect_parser = subparsers.add_parser(
        "detect",
        help="Detect source IDs from workspace files",
        description="Auto-detect workspace, lakehouse, and connection IDs from repository files",
    )
    _ = detect_parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        type=Path,
        help="Workspace directory (defaults to current directory)",
    )
    _ = detect_parser.add_argument(
        "--save",
        "-s",
        action="store_true",
        help="Save detected parameters to parameter.yml.detected (uses smart replace format)",
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
    _ = deploy_parser.add_argument(
        "--no-auto-detect",
        dest="auto_detect",
        action="store_false",
        default=True,
        help="Disable auto-detect of source IDs (uses existing parameter.yml)",
    )
    _ = deploy_parser.add_argument(
        "--save-params",
        action="store_true",
        help="Save auto-detected parameters to parameter.yml.detected",
    )
    _ = deploy_parser.add_argument(
        "--branch",
        "-b",
        help="Override git branch name for workspace resolution (defaults to current git branch)",
    )
    _ = deploy_parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="Enable two-phase deployment (foundational items first, then all items)",
    )
    _ = deploy_parser.add_argument(
        "--create-workspace",
        action="store_true",
        help="Create workspace if it doesn't exist (requires capacity_id in fabdep.yml)",
    )

    # Check if we should inject 'deploy' as default subcommand
    # This happens when no subcommand is specified
    argv = sys.argv[1:]
    known_commands = ["auth", "deploy", "rename", "detect"]

    # Inject 'deploy' if no subcommand and not just asking for help
    if argv and not any(cmd in argv for cmd in known_commands):
        if argv not in (["-h"], ["--help"]):
            sys.argv.insert(1, "deploy")

    args = parser.parse_args()

    # Default to deploy command if no command specified (e.g., running `fabdep` with no args)
    if args.command is None:
        args.command = "deploy"
        # Set deploy defaults (deploy_parser won't have set these if no subcommand provided)
        if not hasattr(args, "directory"):
            args.directory = Path(".")
            args.no_cleanup = False
            args.item_types = None
            args.exclude_types = None
            args.include_items = None
            args.auto_detect = True
            args.save_params = False
            args.branch = None
            args.bootstrap = False
            args.create_workspace = False

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


def handle_detect_command(args: argparse.Namespace) -> None:
    """Handle detect subcommand execution."""
    try:
        detector = ParameterDetector(repo_dir=args.directory)
        detector.detect_all()

        # Print summary
        detector.print_detection_summary()

        # Save if requested
        if args.save:
            output_file = Path(args.directory) / "parameter.yml.detected"
            detector.save_parameter_yml(
                output_path=output_file,
                use_dynamic=True,
                environment="prod",
                skip_empty=True,
            )

            # Count what was saved
            with_dynamic = sum(
                1 for p in detector.parameters.values() if p.dynamic_variable
            )
            total = len(detector.parameters)

            print(f"✓ Saved {with_dynamic} parameter(s) to: {output_file}\n")
            if with_dynamic < total:
                print(
                    f"Note: {total - with_dynamic} parameter(s) without dynamic variables were skipped\n"
                    "(External connections require manual setup and typically don't change between deployments)\n"
                )
            print(
                "Using dynamic variables: $workspace.$id, $items.Lakehouse.<name>.$id, etc.\n"
            )

    except FileNotFoundError as e:
        print_error(f"\n{e}\n")
        print("Usage: fabdep detect [DIRECTORY] [OPTIONS]")
        print("Run 'fabdep detect -h' for more information.\n")
        sys.exit(1)
    except Exception as e:
        print_error(f"\nDetection failed: {e}\n")
        sys.exit(1)


def handle_deploy_command(args: argparse.Namespace) -> None:
    """Handle deploy subcommand execution."""

    try:
        deployer = WorkspaceDeployer(
            repo_dir=args.directory,
            logger_name="fabric_deploy.cli",
            auto_detect=bool(args.auto_detect),
            save_detected_params=bool(args.save_params),
            branch_override=args.branch,
            bootstrap=bool(args.bootstrap),
            create_workspace=bool(args.create_workspace),
        )
    except FileNotFoundError as e:
        print_error(f"\n{e}\n")
        print("Usage: fabdep [OPTIONS]")
        print("Run 'fabdep -h' for more information.\n")
        sys.exit(1)
    except Exception as e:
        handle_command_error(e, "Workspace initialization")

    try:
        deployer.deploy(
            no_cleanup=args.no_cleanup,
            item_types=args.item_types,
            exclude_types=args.exclude_types,
            include_items=args.include_items,
        )
    except Exception as e:
        handle_command_error(e, "Deployment")


def main() -> None:
    """CLI entry point - routes to appropriate command handler."""
    args = parse_args()

    # Route to appropriate handler
    if args.command == "auth":
        handle_auth_command(args)
    elif args.command == "rename":
        handle_rename_command(args)
    elif args.command == "detect":
        handle_detect_command(args)
    elif args.command == "deploy":
        handle_deploy_command(args)
    else:
        print_error(f"Unknown command: {args.command}")
        sys.exit(1)
