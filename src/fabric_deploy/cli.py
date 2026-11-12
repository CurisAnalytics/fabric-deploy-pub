"""CLI interface for fabric-deploy package."""

import argparse
import sys
from pathlib import Path
from textwrap import dedent

from .auth import (
    bind_tenant,
    list_accounts,
    login_tenant,
    rename_subscription,
    switch_account,
)
from .detect import ParameterDetector
from .log import (
    SEP,
    error_hint,
    format_account_list,
    format_auth_error_message,
    is_auth_error,
    is_connection_error,
    get_logger,
    is_expired_credential_error,
    print_error,
)
from .rename import rename_items_by_display_name
from .workspace import WorkspaceDeployer, format_deploy_options, log_banner


def handle_command_error(error: Exception, context: str) -> None:
    """Handle command errors with specific troubleshooting for different error types.

    Args:
        error: The exception that was raised
        context: Context string describing what failed (e.g., "Workspace operation", "Deployment")
    """
    error_msg = str(error)
    print_error(f"\n{context} failed: {error_msg}\n")

    # Translate known-cryptic Fabric errors (RequiredOptionsMissing, parse
    # failures, ...) into a plain-language hint. Independent of the auth /
    # connection branches below — these are content errors that share no
    # keywords with them.
    hint = error_hint(error_msg)
    if hint:
        print_error(f"{SEP}\nLikely cause\n{SEP}\n\n{hint}\n\n{SEP}\n")

    # Connection errors first — their messages often contain auth keywords too.
    if is_connection_error(error_msg):
        connection_help = dedent(
            f"""
            {SEP}
            Connection Reference Error Detected
            {SEP}

            This error occurs when a DataPipeline (or other item) references a connection
            that is unavailable in the target tenant:
            \t• Connection GUIDs are Tenant-wide (not Workspace-specific)
            \t• The Workspace repo contains connection GUIDs from a different tenant
            \t• Alternatively, you lack permissions to one or more needed connections

            Solution:
            \t• Identify the correct connections between the repo and the Fabric UI
            \t• If possible/advisable, fix the connections in the Fabric UI

            Notes:
            \t• Connections must exist in the target tenant for a DataPipeline to be deployed
            \t• Use 'fabdep detect' to find connection GUIDs in your current repo

            {SEP}
            """
        )
        print_error(connection_help)
    elif is_expired_credential_error(error_msg):
        # The credential itself is expired — switching accounts can't fix it,
        # so the account list is noise. The AADSTS text above says what to do.
        print_error(
            "The active credential has expired. Rotate the SPN secret/cert "
            "(or `az login` again for a user account) — switching accounts "
            "won't help.\n"
        )
    elif is_auth_error(error_msg):
        all_accounts = list_accounts()
        auth_help = format_auth_error_message(all_accounts)
        print_error(auth_help)

    sys.exit(1)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    from . import __version__

    parser = argparse.ArgumentParser(
        prog="fabdep",
        description="Deploy Fabric workspace items - auto-detects configuration from parameter.yml",
        epilog="Run from your Fabric workspace directory.",
    )
    parser.add_argument(
        "--version", "-V", action="version", version=f"%(prog)s {__version__}"
    )

    subparsers = parser.add_subparsers(
        dest="command", help="Available commands (defaults to 'deploy')"
    )

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
        "account", help="Subscription ID or account name (alias supported)"
    )

    rename_parser = auth_subparsers.add_parser(
        "rename", help="Label a subscription in `fabdep auth list` (not a tenant alias)"
    )
    _ = rename_parser.add_argument("subscription_id", help="Subscription ID to rename")
    _ = rename_parser.add_argument("name", help="New subscription name")

    login_parser = auth_subparsers.add_parser(
        "login",
        help="Log a tenant in to an isolated config dir and register its alias",
    )
    _ = login_parser.add_argument("alias", help="Tenant alias (e.g. contoso) used in fabdep.yml")
    _ = login_parser.add_argument("--app", help="SPN client id (first login / cert swap)")
    _ = login_parser.add_argument("--tenant", help="Entra tenant GUID (first login)")
    _ = login_parser.add_argument("--cert", help="Path to the SPN cert .pem (first login)")
    _ = login_parser.add_argument(
        "--config-dir", help="AZURE_CONFIG_DIR to use (default ~/.azure-<alias>)"
    )
    _ = login_parser.add_argument(
        "--user", action="store_true", help="Interactive user login instead of an SPN"
    )

    bind_parser = auth_subparsers.add_parser(
        "bind",
        help="Register a tenant alias against an existing az login (no new login)",
    )
    _ = bind_parser.add_argument("alias", help="Tenant alias (e.g. contoso) used in fabdep.yml")
    _ = bind_parser.add_argument(
        "target",
        help="Tenant GUID (any account in it), or a subscription id/name from "
        "`fabdep auth list` to pin that account",
    )

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
    _ = detect_parser.add_argument(
        "--source-workspace-id",
        dest="source_workspace_ids",
        action="append",
        metavar="GUID",
        help="A workspace the content may have been exported from, as an anchor "
        "for cross-workspace detection (repeatable; deploy lists the repo's "
        "sibling workspaces itself)",
    )

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
        help="Item types to deploy (e.g., Notebook DataPipeline)",
    )
    _ = deploy_parser.add_argument(
        "--exclude-types",
        "-x",
        nargs="+",
        help="Item types to exclude (e.g., Report SemanticModel)",
    )
    _ = deploy_parser.add_argument(
        "--include-items",
        "-i",
        nargs="+",
        help='Specific items only (format: "display_name.ItemType")',
    )
    _ = deploy_parser.add_argument(
        "--branch",
        "-b",
        help="Override git branch for workspace resolution",
    )
    _ = deploy_parser.add_argument(
        "--pr",
        nargs="?",
        const="HEAD",
        metavar="COMMIT",
        help="Deploy only the items a pull request changed: those of the newest "
        "pull-request commit in HEAD's first-parent history (as Azure DevOps, GitHub, "
        "GitLab or Bitbucket word it; a plain merge commit doesn't count; fails if HEAD "
        "has commits on top of it), or of COMMIT. Mutually exclusive with --include-items.",
    )
    _ = deploy_parser.add_argument(
        "--new-workspace",
        "-N",
        action="store_true",
        help="Create workspace if it doesn't exist",
    )
    _ = deploy_parser.add_argument(
        "--max-retries",
        "-r",
        type=int,
        default=1,
        help="Max retries for transient errors (default: 1)",
    )
    _ = deploy_parser.add_argument(
        "--skip-lakehouse",
        "-L",
        action="store_true",
        help="Skip lakehouse parameter replacement (for feature branches sharing dev lakehouse)",
    )
    _ = deploy_parser.add_argument(
        "--fail-on-stale",
        "-F",
        action="store_true",
        help="Exit non-zero if any notebook has multiple lakehouses attached "
        "(for CI / autonomous agent loops where the warning would get buried)",
    )
    _ = deploy_parser.add_argument(
        "--frame",
        action="store_true",
        help="After deploy, frame the Direct Lake model(s): sync SQL endpoint "
        "-> refresh -> poll -> surface the first framing error",
    )
    _ = deploy_parser.add_argument(
        "--allow-rls-change",
        action="store_true",
        help="Permit a deploy to change a model's live row-level security. "
        "By default, if the repo carries RLS and the target's live RLS differs, "
        "the deploy is blocked (someone may have edited RLS directly in the "
        "target, or a role's annotation PBI_Id changed, which makes the "
        "service recreate the role without its members).",
    )
    _ = deploy_parser.add_argument(
        "--allow-unbound-udf",
        action="store_true",
        help="Deploy report buttons whose User Data Function reference could "
        "not be rebound to the target workspace (missing or ambiguous "
        ".UserDataFunction item). By default such a button blocks the deploy, "
        "because it would ship still calling the SOURCE workspace's function.",
    )

    refresh_parser = subparsers.add_parser(
        "refresh",
        help="Frame a Direct Lake semantic model (sync SQL endpoint -> refresh -> poll)",
        description="Sync the SQL analytics endpoint metadata, then refresh and "
        "poll the semantic model(s), surfacing the first framing error.",
    )
    _ = refresh_parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        type=Path,
        help="Workspace directory (defaults to current directory)",
    )
    _ = refresh_parser.add_argument(
        "--include-items",
        "-i",
        nargs="+",
        help="Semantic model display name(s) to frame (default: all in repo); "
        "a trailing '.SemanticModel' is tolerated to match deploy -i",
    )
    _ = refresh_parser.add_argument(
        "--branch",
        "-b",
        help="Override git branch for workspace resolution",
    )
    _ = refresh_parser.add_argument(
        "--no-sync",
        action="store_true",
        help="Skip the SQL analytics endpoint metadata sync",
    )
    _ = refresh_parser.add_argument(
        "--timeout",
        type=int,
        default=900,
        help="Per-model refresh poll timeout in seconds (default: 900)",
    )

    rls_parser = subparsers.add_parser(
        "rls",
        help="Inspect/reconcile row-level security (role definitions) vs the target",
        description="Read a semantic model's live RLS role definitions and compare "
        "them to the repo. Definitions only — role memberships are Service state "
        "and are never read; a role whose annotation PBI_Id changes loses them.",
    )
    rls_subparsers = rls_parser.add_subparsers(
        dest="rls_command", required=True, help="RLS actions"
    )
    rls_pull_parser = rls_subparsers.add_parser(
        "pull",
        help="Diff the target's RLS roles against the repo; --write to adopt them",
    )
    _ = rls_pull_parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        type=Path,
        help="Workspace directory (defaults to current directory)",
    )
    _ = rls_pull_parser.add_argument(
        "--include-items",
        "-i",
        nargs="+",
        help="Semantic model display name(s) to check (default: all in repo); "
        "a trailing '.SemanticModel' is tolerated to match deploy -i",
    )
    _ = rls_pull_parser.add_argument(
        "--branch",
        "-b",
        help="Override git branch for workspace resolution",
    )
    _ = rls_pull_parser.add_argument(
        "--write",
        action="store_true",
        help="Overwrite the working tree's roles to match the target "
        "(for review + commit); default is diff-only (exit non-zero on drift)",
    )

    # argparse can't natively pick a default subparser or treat unknown
    # leading args as a subcommand's options — so resolve it ourselves
    # and feed argparse a normalized argv.
    known_commands = {"auth", "deploy", "rename", "detect", "refresh", "rls"}
    help_flags = {"-h", "--help", "-V", "--version"}

    raw = sys.argv[1:]
    head = raw[0] if raw else None

    if head is None:
        argv = ["deploy"]
    elif head in known_commands or head in help_flags:
        argv = raw
    else:
        # `fabdep -t Notebook`, `fabdep .`, etc. — treat as deploy options.
        argv = ["deploy", *raw]

    return parser.parse_args(argv)


def handle_auth_command(args: argparse.Namespace) -> None:
    """Handle auth subcommand execution."""
    if args.auth_command == "list":
        try:
            all_accounts = list_accounts()
            print(format_account_list(all_accounts))
        except Exception as e:
            print_error(f"Failed to list accounts: {e}")
            sys.exit(1)

    elif args.auth_command == "switch":
        try:
            switch_account(args.account)
        except Exception as e:
            print_error(f"\nFailed to switch account: {e}\n")
            sys.exit(1)

    elif args.auth_command == "rename":
        try:
            rename_subscription(args.subscription_id, args.name)
        except Exception as e:
            print_error(f"\nFailed to rename subscription: {e}\n")
            sys.exit(1)

    elif args.auth_command == "login":
        try:
            entry = login_tenant(
                args.alias,
                app_id=args.app,
                tenant=args.tenant,
                cert=args.cert,
                config_dir=args.config_dir,
                user=args.user,
            )
            print(
                f"registered tenant '{args.alias}' -> {entry['id']} "
                f"(AZURE_CONFIG_DIR={entry.get('config_dir')})"
            )
        except Exception as e:
            print_error(f"\nFailed to log in tenant '{args.alias}': {e}\n")
            sys.exit(1)

    elif args.auth_command == "bind":
        try:
            entry = bind_tenant(args.alias, args.target)
            sub = entry.get("subscription")
            via = f"account {sub}" if sub else "any enabled account in the tenant"
            print(f"registered tenant '{args.alias}' -> {entry['id']} ({via})")
        except Exception as e:
            print_error(f"\nFailed to bind tenant '{args.alias}': {e}\n")
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
        detector = ParameterDetector(
            repo_dir=args.directory,
            source_workspace_ids=args.source_workspace_ids,
        )
        detector.detect_all()
        detector.print_detection_summary()

        if args.save:
            output_file = Path(args.directory) / "parameter.yml.detected"
            detector.save_parameter_yml(output_path=output_file, environment="prod")

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

    # Header first: constructing the deployer already resolves workspaces and
    # runs detection, and their warnings belong under the banner.
    log_banner(
        get_logger("fabric_deploy.cli"),
        format_deploy_options(
            branch_override=args.branch,
            new_workspace=bool(args.new_workspace),
            skip_lakehouse=bool(args.skip_lakehouse),
            max_retries=int(args.max_retries),
            no_cleanup=args.no_cleanup,
            item_types=args.item_types,
            exclude_types=args.exclude_types,
            include_items=args.include_items,
            fail_on_stale=args.fail_on_stale,
            allow_rls_change=bool(args.allow_rls_change),
            allow_unbound_udf=bool(args.allow_unbound_udf),
            pr=args.pr,
        ),
    )

    try:
        deployer = WorkspaceDeployer(
            repo_dir=args.directory,
            logger_name="fabric_deploy.cli",
            branch_override=args.branch,
            new_workspace=bool(args.new_workspace),
            max_retries=int(args.max_retries),
            skip_lakehouse=bool(args.skip_lakehouse),
        )
    except FileNotFoundError as e:
        print_error(f"\n{e}\n")
        print("Usage: fabdep [OPTIONS]")
        print("Run 'fabdep -h' for more information.\n")
        sys.exit(1)
    except Exception as e:
        handle_command_error(e, "Workspace initialization")  # exits

    try:
        deployer.deploy(  # pyright: ignore[reportPossiblyUnboundVariable]
            no_cleanup=args.no_cleanup,
            item_types=args.item_types,
            exclude_types=args.exclude_types,
            include_items=args.include_items,
            fail_on_stale=args.fail_on_stale,
            allow_rls_change=bool(args.allow_rls_change),
            allow_unbound_udf=bool(args.allow_unbound_udf),
            pr=args.pr,
            banner=False,
        )
    except Exception as e:
        handle_command_error(e, "Deployment")

    if getattr(args, "frame", False):
        # Frame the model(s) just deployed. If the run was scoped (-i, or --pr
        # resolving to an item list), frame those SemanticModels; otherwise
        # frame every model in the repo.
        models = None
        scoped = args.include_items or deployer.deployed_items  # pyright: ignore[reportPossiblyUnboundVariable]
        if scoped:
            sm = [
                it.rsplit(".", 1)[0]
                for it in scoped
                if it.endswith(".SemanticModel")
            ]
            models = sm or None
        try:
            from .refresh import frame_models

            frame_models(
                repo_dir=args.directory,
                branch_override=args.branch,
                models=models,
            )
        except Exception as e:
            handle_command_error(e, "Framing")


def handle_refresh_command(args: argparse.Namespace) -> None:
    """Handle refresh subcommand execution (Direct Lake framing)."""
    from .refresh import frame_models

    try:
        frame_models(
            repo_dir=args.directory,
            branch_override=args.branch,
            models=args.include_items,
            sync=not args.no_sync,
            timeout_s=int(args.timeout),
        )
    except FileNotFoundError as e:
        print_error(f"\n{e}\n")
        sys.exit(1)
    except Exception as e:
        handle_command_error(e, "Framing")


def handle_rls_command(args: argparse.Namespace) -> None:
    """Handle rls subcommand execution (RLS definition round-trip)."""
    from .rls import pull_rls

    if args.rls_command == "pull":
        try:
            changed = pull_rls(
                repo_dir=args.directory,
                branch_override=args.branch,
                models=args.include_items,
                write=bool(args.write),
            )
        except FileNotFoundError as e:
            print_error(f"\n{e}\n")
            sys.exit(1)
        except Exception as e:
            handle_command_error(e, "RLS pull")
        # Diff-only run that found drift exits non-zero so CI can gate on it.
        if changed and not args.write:  # pyright: ignore[reportPossiblyUnboundVariable]
            sys.exit(1)
    else:
        print_error(f"Unknown rls command: {args.rls_command}")
        sys.exit(1)


def main() -> None:
    """CLI entry point - routes to appropriate command handler."""
    args = parse_args()

    if args.command == "auth":
        handle_auth_command(args)
    elif args.command == "rename":
        handle_rename_command(args)
    elif args.command == "detect":
        handle_detect_command(args)
    elif args.command == "refresh":
        handle_refresh_command(args)
    elif args.command == "rls":
        handle_rls_command(args)
    elif args.command == "deploy":
        handle_deploy_command(args)
    else:
        print_error(f"Unknown command: {args.command}")
        sys.exit(1)
