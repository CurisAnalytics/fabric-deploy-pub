"""CLI interface for fabric-deploy package."""

import argparse
import logging
import sys
from pathlib import Path
from textwrap import dedent

from fabric_cicd._common._logging import log_header

from .config import AUTH_MODES, load_tenant_pin
from .detect import ParameterDetector
from .identity import (
    AuthError,
    apply_tenant_pin,
    auth_hint,
    credential_in_use,
    describe_identity,
    set_auth_mode,
)
from .log import (
    SEP,
    error_hint,
    get_logger,
    is_auth_error,
    is_connection_error,
    is_expired_credential_error,
    print_error,
)
from .refresh import frame_models
from .workspace import PR_HEAD, WorkspaceDeployer

_TOP_LEVEL_FLAGS = ("-h", "--help", "-V", "--version")
# Named in an error instead of being read as deploy arguments (a directory "rename").
_REMOVED_COMMANDS = ("rename", "auth list", "auth switch", "auth rename", "auth login", "auth bind")


def format_deploy_options(
    branch_override: str | None = None,
    new_workspace: bool = False,
    skip_lakehouse: bool = False,
    no_cleanup: bool = False,
    item_types: list[str] | None = None,
    exclude_types: list[str] | None = None,
    include_items: list[str] | None = None,
    fail_on_stale: bool = False,
    allow_rls_change: bool = False,
    allow_unbound_udf: bool = False,
    pr: str | None = None,
) -> str:
    """One-line record of every non-default deploy option, in CLI-flag form.

    Printed under the version banner so a run's scrollback says which flags
    were in effect without reconstructing the command line.
    """
    opts: list[str] = []
    if branch_override:
        opts.append(f"--branch {branch_override}")
    if new_workspace:
        opts.append("--new-workspace")
    if skip_lakehouse:
        opts.append("--skip-lakehouse")
    if no_cleanup:
        opts.append("--no-cleanup")
    if item_types:
        opts.append(f"--item-types {' '.join(item_types)}")
    if exclude_types:
        opts.append(f"--exclude-types {' '.join(exclude_types)}")
    if include_items:
        opts.append(f"--include-items {' '.join(repr(i) for i in include_items)}")
    if pr:
        opts.append("--pr" if pr == PR_HEAD else f"--pr {pr}")
    if fail_on_stale:
        opts.append("--fail-on-stale")
    if allow_rls_change:
        opts.append("--allow-rls-change")
    if allow_unbound_udf:
        opts.append("--allow-unbound-udf")
    return f"Options: {' '.join(opts) if opts else '(defaults)'}"


def log_banner(logger: logging.Logger, options_line: str) -> None:
    """The `fabdep vX` header plus the options line.

    Printed before the deployer is constructed: construction already resolves
    workspaces and runs detection, whose warnings belong under the header.
    """
    from . import __version__

    log_header(logger, f"fabdep v{__version__}")
    logger.info(options_line)


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
        print_error(
            "The credential has expired: issue a new client secret or certificate for the "
            "service principal (or sign in again as a user) and retry.\n"
        )
    elif is_auth_error(error) and not isinstance(error, AuthError):
        # AuthError already says how to sign in; this covers the API refusing the identity.
        print_error(
            f"{SEP}\nAuthentication or permissions\n{SEP}\n\n"
            f"Credential: {credential_in_use()}\n"
            "\t• Run `fabdep auth status` to see which identity and tenant that is.\n"
            f"\t• {auth_hint()}\n"
            "\t• The identity needs a Contributor (or higher) role on the workspace; a service\n"
            "\t  principal also needs the Fabric admin setting that lets service principals use\n"
            "\t  Fabric APIs.\n\n"
            f"{SEP}\n"
        )

    sys.exit(1)


def parse_args() -> argparse.Namespace:
    """Parse the command line; anything that isn't a command runs `deploy`."""
    from . import __version__

    # Shared arguments, layered: the directory (every command), the credential
    # (commands that call Fabric), and the target (commands that act on items).
    repo = argparse.ArgumentParser(add_help=False)
    _ = repo.add_argument(
        "directory",
        nargs="?",
        default=".",
        type=Path,
        help="Workspace directory (default: the current directory)",
    )
    signed_in = argparse.ArgumentParser(add_help=False, parents=[repo])
    _ = signed_in.add_argument(
        "--auth",
        choices=AUTH_MODES,
        help="Credential to sign in with (default: $FABDEP_AUTH, then fabdep.yml 'auth:', then cli)",
    )
    target = argparse.ArgumentParser(add_help=False, parents=[signed_in])
    _ = target.add_argument(
        "-b",
        "--branch",
        help="Branch whose workspace to target (default: the checked-out branch)",
    )
    _ = target.add_argument(
        "-i",
        "--include-items",
        nargs="+",
        metavar="ITEM",
        help="Only these items, as '<displayName>.<ItemType>' (refresh and rls take "
        "semantic models, with or without the '.SemanticModel' suffix)",
    )

    parser = argparse.ArgumentParser(
        prog="fabdep",
        description="Deploy Microsoft Fabric workspace items from git with fabric-cicd.\n"
        "The target workspace comes from fabdep.yml in the workspace directory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _ = parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", title="commands", metavar="COMMAND")

    auth = commands.add_parser("auth", help="Show the identity fabdep signs in with")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True, metavar="ACTION")
    _ = auth_commands.add_parser(
        "status",
        parents=[signed_in],
        help="Sign in and show the identity, tenant, credential and token type fabdep would use",
    )

    detect = commands.add_parser(
        "detect",
        parents=[repo],
        help="Show the source IDs a deploy would rewrite",
        description="Find the workspace, lakehouse and connection IDs in the repo's item "
        "files and the replacement each one gets on deploy.",
    )
    _ = detect.add_argument(
        "-s",
        "--save",
        action="store_true",
        help="Also write the replacements to parameter.yml.detected, as a fabric-cicd "
        "parameter file",
    )
    _ = detect.add_argument(
        "--source-workspace-id",
        dest="source_workspace_ids",
        action="append",
        metavar="GUID",
        help="A workspace the content may have been exported from, as an anchor "
        "for cross-workspace detection (repeatable; deploy lists the repo's "
        "sibling workspaces itself)",
    )

    deploy = commands.add_parser(
        "deploy",
        parents=[target],
        help="Deploy the workspace (the default command)",
        description="Publish the directory's items to the workspace fabdep.yml names for the "
        "branch, rewriting the source IDs detection finds, then unpublish the workspace's "
        "items that the repo doesn't contain (see --no-cleanup).",
    )
    _ = deploy.add_argument(
        "-t",
        "--item-types",
        nargs="+",
        metavar="TYPE",
        help="Only these item types (e.g. Notebook DataPipeline)",
    )
    _ = deploy.add_argument(
        "-x",
        "--exclude-types",
        nargs="+",
        metavar="TYPE",
        help="All item types but these (e.g. Report SemanticModel)",
    )
    _ = deploy.add_argument(
        "--pr",
        nargs="?",
        const=PR_HEAD,
        metavar="COMMIT",
        help="Deploy only the items a pull request changed: those of the newest "
        "pull-request commit reachable from HEAD (as Azure DevOps, GitHub, GitLab or "
        "Bitbucket word it; a plain merge commit doesn't count), or of COMMIT. Not with -i.",
    )
    _ = deploy.add_argument(
        "-n",
        "--no-cleanup",
        action="store_true",
        help="Don't unpublish workspace items that the repo doesn't contain",
    )
    _ = deploy.add_argument(
        "-N",
        "--new-workspace",
        action="store_true",
        help="Create the target workspace; fails if it already exists (to create it only "
        "when missing, set 'create: true' for the branch under 'environments:' in fabdep.yml)",
    )
    _ = deploy.add_argument(
        "-L",
        "--skip-lakehouse",
        action="store_true",
        help="Leave lakehouse references unchanged (for a feature-branch workspace that "
        "reads the dev workspace's lakehouse)",
    )
    _ = deploy.add_argument(
        "-F",
        "--fail-on-stale",
        action="store_true",
        help="Fail instead of warning when a notebook's warehouse attachment can't be "
        "remapped to the target workspace (for CI, where a warning goes unnoticed)",
    )
    _ = deploy.add_argument(
        "--frame",
        action="store_true",
        help="After deploying, refresh the semantic models the deploy published, first "
        "syncing the SQL analytics endpoints their Direct Lake tables read",
    )
    _ = deploy.add_argument(
        "--allow-rls-change",
        action="store_true",
        help="Deploy even if it changes a semantic model's live row-level security. By "
        "default every in-scope model's roles are compared with the target's, and any "
        "difference blocks the deploy: a changed or removed role (also when the repo has "
        "no roles for the model), or a changed role PBI_Id, which makes the service "
        "recreate the role without its members.",
    )
    _ = deploy.add_argument(
        "--allow-unbound-udf",
        action="store_true",
        help="Deploy report buttons whose User Data Function reference could "
        "not be rebound to the target workspace (missing or ambiguous "
        ".UserDataFunction item). By default such a button blocks the deploy, "
        "because it would ship still calling the SOURCE workspace's function.",
    )

    refresh = commands.add_parser(
        "refresh",
        parents=[target],
        help="Refresh semantic models in the target workspace (Direct Lake framing)",
        description="Sync the SQL analytics endpoints the models' Direct Lake tables read, "
        "then refresh each semantic model and wait for it, failing on the first refresh "
        "error. Without -i, every semantic model in the repo.",
    )
    _ = refresh.add_argument(
        "--no-sync",
        action="store_true",
        help="Skip syncing the SQL analytics endpoints the Direct Lake models read",
    )
    _ = refresh.add_argument(
        "--timeout",
        type=int,
        default=900,
        help="Per-model refresh poll timeout in seconds (default: 900)",
    )

    rls = commands.add_parser(
        "rls",
        help="Compare row-level security roles with the target",
        description="Compare the repo's row-level security role definitions with the "
        "target's. Role memberships are not part of a model definition: they can't be "
        "compared or restored from the repo, and a deploy that removes a role or changes "
        "its PBI_Id drops its members.",
    )
    rls_commands = rls.add_subparsers(dest="rls_command", required=True, metavar="ACTION")
    rls_pull = rls_commands.add_parser(
        "pull",
        parents=[target],
        help="Diff the target's RLS roles against the repo; --write to adopt them",
    )
    _ = rls_pull.add_argument(
        "--write",
        action="store_true",
        help="Overwrite the working tree's roles to match the target "
        "(for review + commit); default is diff-only (exit non-zero on drift)",
    )

    parser.epilog = "With no command, fabdep runs deploy:\n\n" + deploy.format_help()

    # argparse has no default subcommand: anything that isn't a command or a
    # top-level flag is the deploy command's arguments.
    argv = sys.argv[1:]
    for removed in _REMOVED_COMMANDS:
        if argv[: removed.count(" ") + 1] == removed.split():
            parser.exit(2, f"fabdep: '{removed}' was removed; see CHANGELOG.md.\n")
    if not argv or (argv[0] not in commands.choices and argv[0] not in _TOP_LEVEL_FLAGS):
        argv = ["deploy", *argv]
    return parser.parse_args(argv)


def handle_auth_command(args: argparse.Namespace) -> None:
    """Handle auth subcommand execution."""
    try:
        apply_tenant_pin(load_tenant_pin(args.directory), get_logger("fabric_deploy.cli"))
        identity = describe_identity()
    except Exception as e:
        handle_command_error(e, "auth status")
    for label, value in identity.items():  # pyright: ignore[reportPossiblyUnboundVariable]
        print(f"{label + ':':<12} {value}")


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
            detector.save_parameter_yml(output_path=output_file)

            with_dynamic = sum(1 for p in detector.parameters.values() if p.dynamic_variable)
            total = len(detector.parameters)

            print(f"✓ Saved {with_dynamic} parameter(s) to: {output_file}\n")
            if with_dynamic < total:
                print(
                    f"Note: {total - with_dynamic} parameter(s) without dynamic variables were skipped\n"
                    "(External connections require manual setup and typically don't change between deployments)\n"
                )
            print("Using dynamic variables: $workspace.$id, $items.Lakehouse.<name>.$id, etc.\n")

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
        )
    except Exception as e:
        handle_command_error(e, "Deployment")
    print()

    if args.frame:
        models = _published_models(args, deployer.deployed_items)  # pyright: ignore[reportPossiblyUnboundVariable]
        if models == []:
            get_logger("fabric_deploy.cli").info(
                "--frame: the deploy published no semantic model, so there is nothing to frame."
            )
            return
        try:
            _ = frame_models(repo_dir=args.directory, branch_override=args.branch, models=models)
        except Exception as e:
            handle_command_error(e, "Framing")


def _published_models(args: argparse.Namespace, deployed_items: list[str]) -> list[str] | None:
    """Display names of the semantic models a deploy published; None means all the repo's.

    Mirrors the deploy's scope: the -t/-x type filter (case-insensitive, as the
    deploy applies it), then the item list that -i or --pr resolved to.
    """
    if args.item_types:  # -t wins over -x, as in the deploy
        in_scope = "semanticmodel" in {t.lower() for t in args.item_types}
    else:
        in_scope = "semanticmodel" not in {t.lower() for t in args.exclude_types or ()}
    if not in_scope:
        return []
    if not deployed_items:
        return None
    suffix = ".SemanticModel"
    return [item.removesuffix(suffix) for item in deployed_items if item.endswith(suffix)]


def handle_refresh_command(args: argparse.Namespace) -> None:
    """Handle refresh subcommand execution (Direct Lake framing)."""
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
    set_auth_mode(getattr(args, "auth", None))

    if args.command == "auth":
        handle_auth_command(args)
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
