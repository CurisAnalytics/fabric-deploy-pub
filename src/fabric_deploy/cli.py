"""The `fabdep` command line."""

import argparse
import difflib
import io
import json
import logging
import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from textwrap import dedent, indent
from typing import NoReturn, cast

from .config import AUTH_MODES, load_auth_settings
from .detect import ParameterDetector
from .identity import (
    AuthError,
    apply_auth_settings,
    auth_hint,
    credential_in_use,
    describe_identity,
    set_auth_mode,
)
from .log import (
    GREEN,
    error_hint,
    get_logger,
    is_auth_error,
    is_connection_error,
    is_expired_credential_error,
    print_error,
    use_color,
)
from .plan import BLOCKED_EXIT_CODE, JSON_SHAPE, format_plan, plan_deploy, plan_json
from .refresh import frame_models
from .rls import pull_rls
from .scaffold import schema_text, write_starter
from .scope import repo_items
from .workspace import PR_HEAD, WorkspaceDeployer

_SEP = "=" * 100
# Deploy flags echoed under the banner, in this order.
_ECHOED_FLAGS = (
    "auth",
    "branch",
    "new_workspace",
    "skip_lakehouse",
    "no_cleanup",
    "item_types",
    "exclude_types",
    "include_items",
    "pr",
    "fail_on_stale",
    "frame",
    "allow_rls_change",
    "allow_unbound_udf",
)
_TOP_LEVEL_FLAGS = ("-h", "--help", "-V", "--version")
_OUTPUTS = ("text", "json")
# Named in an error instead of being read as deploy arguments (a directory "rename").
_REMOVED_COMMANDS = ("rename", "auth list", "auth switch", "auth rename", "auth login", "auth bind")
_REMOVED_OPTIONS = ("--max-retries", "-r")


def format_deploy_options(args: argparse.Namespace) -> str:
    """The deploy's non-default flags as one `Options:` line, in command-line form.

    Printed under the version banner so a run's scrollback says which flags
    were in effect without reconstructing the command line.
    """
    opts: list[str] = []
    for dest in _ECHOED_FLAGS:
        value = getattr(args, dest)
        flag = "--" + dest.replace("_", "-")
        if value is True or (dest == "pr" and value == PR_HEAD):
            opts.append(flag)
        elif isinstance(value, list):
            opts.append(shlex.join([flag, *cast(list[str], value)]))
        elif value:
            opts.append(shlex.join([flag, value]))
    return f"Options: {' '.join(opts) or '(defaults)'}"


def log_banner(logger: logging.Logger, options_line: str) -> None:
    """The `fabdep vX` header plus the options line.

    Printed before the deployer is constructed: construction already resolves
    workspaces and runs detection, whose warnings belong under the header.
    """
    from . import __version__

    rule = "#" * 100
    title = f"########## fabdep v{__version__} "
    logger.info("")
    for line in (rule, title + rule[len(title) :], rule):
        logger.info(line, extra={"color": GREEN})
    logger.info("")
    logger.info(options_line)


def handle_command_error(error: Exception, context: str) -> NoReturn:
    """Print why a command failed, with troubleshooting for known errors, and exit 1.

    Args:
        error: The exception that was raised.
        context: What failed, e.g. "Deployment".
    """
    error_msg = str(error)
    print_error(f"\n{context} failed: {error_msg}\n")

    # Translate known-cryptic Fabric errors (RequiredOptionsMissing, parse
    # failures, ...) into a plain-language hint. Independent of the auth /
    # connection branches below — these are content errors that share no
    # keywords with them.
    hint = error_hint(error_msg)
    if hint:
        print_error(f"{_SEP}\nLikely cause\n{_SEP}\n\n{hint}\n\n{_SEP}\n")

    # Connection errors first — their messages often contain auth keywords too.
    if is_connection_error(error_msg):
        connection_help = dedent(
            f"""
            {_SEP}
            Connection Reference Error Detected
            {_SEP}

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

            {_SEP}
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
            f"{_SEP}\nAuthentication or permissions\n{_SEP}\n\n"
            f"Credential: {credential_in_use()}\n"
            "\t• Run `fabdep auth status` to see which identity and tenant that is.\n"
            f"\t• {auth_hint()}\n"
            "\t• The identity needs a Contributor (or higher) role on the workspace; a service\n"
            "\t  principal also needs the Fabric admin setting that lets service principals use\n"
            "\t  Fabric APIs.\n\n"
            f"{_SEP}\n"
        )

    sys.exit(1)


def _directory(value: str) -> Path:
    """argparse type for the workspace directory: it has to exist."""
    if not Path(value).is_dir():
        raise argparse.ArgumentTypeError(f"no such directory: {value}")
    return Path(value)


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
        type=_directory,
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

    init = commands.add_parser(
        "init",
        parents=[repo],
        help="Write a starter fabdep.yml",
        description="Write a starter fabdep.yml into the workspace directory: "
        "workspace_base_name, and every other setting as a commented-out example.",
    )
    _ = init.add_argument(
        "--base-name",
        metavar="NAME",
        help="workspace_base_name (default: the directory's name)",
    )
    _ = init.add_argument("--force", action="store_true", help="Overwrite an existing fabdep.yml")

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
        "files and the replacement detection gives each. Offline: fabdep.yml is not read, "
        "so manual_parameters and sibling workspaces don't show; `fabdep plan` shows the "
        "exact rules a deploy uses.",
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

    # The deploy's options, which plan takes too.
    deploy_options = argparse.ArgumentParser(add_help=False, parents=[target])
    _ = deploy_options.add_argument(
        "-t",
        "--item-types",
        nargs="+",
        metavar="TYPE",
        help="Only these item types (e.g. Notebook DataPipeline)",
    )
    _ = deploy_options.add_argument(
        "-x",
        "--exclude-types",
        nargs="+",
        metavar="TYPE",
        help="All item types but these (e.g. Report SemanticModel)",
    )
    _ = deploy_options.add_argument(
        "--pr",
        nargs="?",
        const=PR_HEAD,
        metavar="COMMIT",
        help="Deploy only the items a pull request changed: those of the newest "
        "pull-request commit reachable from HEAD (as Azure DevOps, GitHub, GitLab or "
        "Bitbucket word it; a plain merge commit doesn't count), or of COMMIT. Not with -i.",
    )
    _ = deploy_options.add_argument(
        "-n",
        "--no-cleanup",
        action="store_true",
        help="Don't unpublish workspace items that the repo doesn't contain",
    )
    _ = deploy_options.add_argument(
        "-N",
        "--new-workspace",
        action="store_true",
        help="Create the target workspace; fails if it already exists (to create it only "
        "when missing, set 'create: true' for the branch under 'environments:' in fabdep.yml)",
    )
    _ = deploy_options.add_argument(
        "-L",
        "--skip-lakehouse",
        action="store_true",
        help="Leave lakehouse references unchanged (for a feature-branch workspace that "
        "reads the dev workspace's lakehouse)",
    )
    _ = deploy_options.add_argument(
        "-F",
        "--fail-on-stale",
        action="store_true",
        help="Fail instead of warning when a notebook's warehouse attachment can't be "
        "remapped to the target workspace (for CI, where a warning goes unnoticed)",
    )
    _ = deploy_options.add_argument(
        "--allow-rls-change",
        action="store_true",
        help="Deploy even if it changes a semantic model's live row-level security. By "
        "default every in-scope model's roles are compared with the target's, and any "
        "difference blocks the deploy: a changed, added or removed role (also when the "
        "repo has no roles for the model), or a changed role PBI_Id, which makes the service "
        "recreate the role without its members.",
    )
    _ = deploy_options.add_argument(
        "--allow-unbound-udf",
        action="store_true",
        help="Deploy report buttons whose User Data Function reference could "
        "not be rebound to the target workspace (missing or ambiguous "
        ".UserDataFunction item). By default such a button blocks the deploy, "
        "because it would ship still calling the SOURCE workspace's function.",
    )

    deploy = commands.add_parser(
        "deploy",
        parents=[deploy_options],
        help="Deploy the workspace (the default command)",
        description="Publish the directory's items to the workspace fabdep.yml names for the "
        "branch, rewriting the source IDs detection finds, then unpublish the workspace's "
        "items that the repo doesn't contain (see --no-cleanup).",
    )
    _ = deploy.add_argument(
        "--frame",
        action="store_true",
        help="After deploying, refresh the semantic models the deploy published, first "
        "syncing the SQL analytics endpoints their Direct Lake tables read",
    )
    _ = deploy.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what the deploy would do instead of deploying (fabdep plan)",
    )
    _ = deploy.add_argument(
        "-o", "--output", choices=_OUTPUTS, help="With --dry-run: the plan's format"
    )

    plan = commands.add_parser(
        "plan",
        parents=[deploy_options],
        help="Show what a deploy would do, without changing anything",
        description="Work out what `fabdep deploy` with the same options would do, making\n"
        "read calls only: the target workspace, the items it would create, update and\n"
        "unpublish, the find/replace rules it would apply, and each guard's verdict.",
        epilog=f"JSON output (--output json) is one document on stdout:\n\n"
        f"{indent(JSON_SHAPE, '  ')}\n\n"
        "Exit status: 0 the deploy would proceed, 3 a guard would block it, 1 error, 2 usage.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _ = plan.add_argument(
        "-o", "--output", choices=_OUTPUTS, default="text", help="Format (default: text)"
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

    schema = commands.add_parser(
        "schema",
        help="Print the JSON Schema of fabdep.yml, for editor validation",
        description="Print the JSON Schema of fabdep.yml. Save it next to fabdep.yml "
        "(fabdep schema -o fabdep.schema.json) and point the YAML language server at it "
        "with the comment '# yaml-language-server: $schema=./fabdep.schema.json'.",
    )
    _ = schema.add_argument(
        "-o", "--output", type=Path, metavar="FILE", help="Write it to FILE (UTF-8)"
    )

    parser.epilog = "With no command, fabdep runs deploy:\n\n" + deploy.format_help()

    # argparse has no default subcommand: anything that isn't a command or a
    # top-level flag is the deploy command's arguments.
    argv = sys.argv[1:]
    for removed in _REMOVED_COMMANDS:
        if argv[: removed.count(" ") + 1] == removed.split():
            parser.exit(2, f"fabdep: '{removed}' was removed; see CHANGELOG.md.\n")
    for arg in argv:
        if (option := arg.split("=", 1)[0]) in _REMOVED_OPTIONS:
            parser.exit(2, f"fabdep: '{option}' was removed; see CHANGELOG.md.\n")
    if not argv or (argv[0] not in commands.choices and argv[0] not in _TOP_LEVEL_FLAGS):
        # A word that is no directory but close to a command is a mistyped command,
        # not the workspace directory deploy would otherwise report missing.
        first = argv[0] if argv else ""
        if first and not first.startswith("-") and not Path(first).is_dir():
            close = difflib.get_close_matches(first, list(commands.choices), n=1)
            if close:
                parser.exit(2, f"fabdep: unknown command '{first}' (did you mean '{close[0]}'?)\n")
        argv = ["deploy", *argv]
    args = parser.parse_args(argv)
    if args.command == "deploy" and args.output and not args.dry_run:
        deploy.error("--output applies to --dry-run only")
    return args


def handle_auth_command(args: argparse.Namespace) -> None:
    """`fabdep auth status`: the identity, tenant and credential fabdep signs in with."""
    try:
        apply_auth_settings(load_auth_settings(args.directory), get_logger("fabric_deploy.cli"))
        identity = describe_identity()
    except Exception as e:
        handle_command_error(e, "auth status")
    for label, value in identity.items():
        print(f"{label + ':':<12} {value}")


def handle_detect_command(args: argparse.Namespace) -> None:
    """`fabdep detect`: list the detected source IDs, optionally saving them."""
    output_file = args.directory / "parameter.yml.detected"
    try:
        detector = ParameterDetector(
            repo_dir=args.directory, source_workspace_ids=args.source_workspace_ids
        )
        detector.detect_all()
        detector.print_detection_summary()
        if args.save:
            detector.save_parameter_yml(output_path=output_file)
    except Exception as e:
        handle_command_error(e, "Detection")
    if not args.save:
        return

    with_dynamic = sum(1 for p in detector.parameters.values() if p.dynamic_variable)
    skipped = len(detector.parameters) - with_dynamic
    print(f"✓ Saved {with_dynamic} parameter(s) to: {output_file}\n")
    if skipped:
        print(
            f"Note: {skipped} parameter(s) without dynamic variables were skipped\n"
            "(external connections need manual setup and rarely change between deployments)\n"
        )
    print("Using dynamic variables: $workspace.$id, $items.Lakehouse.<name>.$id, etc.\n")


def handle_init_command(args: argparse.Namespace) -> None:
    """`fabdep init`: write a starter fabdep.yml and say what to run next."""
    log = get_logger("fabric_deploy.cli")
    try:
        path = write_starter(args.directory, base_name=args.base_name, force=args.force)
    except (OSError, ValueError) as e:
        handle_command_error(e, "init")
    if not repo_items(args.directory):
        log.warning(
            f"No Fabric items (folders with a .platform file) under {args.directory}: "
            "fabdep.yml belongs in the folder that holds the workspace's item folders."
        )
    where = "" if args.directory.resolve() == Path.cwd().resolve() else f", in {args.directory}"
    print(
        f"✓ Wrote {path}\n\n"
        f"Next steps{where}:\n"
        "  fabdep detect   list the IDs a deploy will rewrite for the target workspace\n"
        "  fabdep plan     show what a deploy would change, without changing anything\n"
        "  fabdep          deploy the checked-out branch"
    )


def handle_deploy_command(args: argparse.Namespace) -> None:
    """`fabdep deploy`, optionally followed by framing the published models."""
    if args.dry_run:
        handle_plan_command(args)
        return
    log = get_logger("fabric_deploy.cli")
    # Header first: constructing the deployer already resolves workspaces and
    # runs detection, and their warnings belong under the banner.
    log_banner(log, format_deploy_options(args))
    try:
        deployer = WorkspaceDeployer(
            repo_dir=args.directory,
            logger_name="fabric_deploy.cli",
            branch_override=args.branch,
            new_workspace=args.new_workspace,
            skip_lakehouse=args.skip_lakehouse,
        )
    except Exception as e:
        handle_command_error(e, "Workspace initialization")
    try:
        deployer.deploy(
            no_cleanup=args.no_cleanup,
            item_types=args.item_types,
            exclude_types=args.exclude_types,
            include_items=args.include_items,
            fail_on_stale=args.fail_on_stale,
            allow_rls_change=args.allow_rls_change,
            allow_unbound_udf=args.allow_unbound_udf,
            pr=args.pr,
        )
    except Exception as e:
        handle_command_error(e, "Deployment")
    if not args.frame:
        return

    suffix = ".SemanticModel"
    models = sorted(i.removesuffix(suffix) for i in deployer.deployed_items if i.endswith(suffix))
    if not models:
        log.info("--frame: the deploy published no semantic model, so there is nothing to frame.")
        return
    try:
        _ = frame_models(repo_dir=args.directory, branch_override=args.branch, models=models)
    except Exception as e:
        handle_command_error(e, "Framing")


def handle_plan_command(args: argparse.Namespace) -> None:
    """`fabdep plan` and `deploy --dry-run`: print the plan; exit 3 if a guard would block it."""
    try:
        plan = plan_deploy(
            args.directory,
            branch_override=args.branch,
            new_workspace=args.new_workspace,
            skip_lakehouse=args.skip_lakehouse,
            no_cleanup=args.no_cleanup,
            item_types=args.item_types,
            exclude_types=args.exclude_types,
            include_items=args.include_items,
            pr=args.pr,
            fail_on_stale=args.fail_on_stale,
            allow_rls_change=args.allow_rls_change,
            allow_unbound_udf=args.allow_unbound_udf,
        )
    except Exception as e:
        handle_command_error(e, "Plan")
    if args.output == "json":
        print(json.dumps(plan_json(plan), indent=2))
    else:
        print(format_plan(plan, color=use_color(sys.stdout)))
    if plan.blocked:
        sys.exit(BLOCKED_EXIT_CODE)


def handle_refresh_command(args: argparse.Namespace) -> None:
    """`fabdep refresh`: frame the semantic models."""
    try:
        _ = frame_models(
            repo_dir=args.directory,
            branch_override=args.branch,
            models=args.include_items,
            sync=not args.no_sync,
            timeout_s=args.timeout,
        )
    except Exception as e:
        handle_command_error(e, "Framing")


def handle_rls_command(args: argparse.Namespace) -> None:
    """`fabdep rls pull`. A diff-only run that finds drift exits 1, so CI can gate on it."""
    try:
        changed = pull_rls(
            repo_dir=args.directory,
            branch_override=args.branch,
            models=args.include_items,
            write=args.write,
        )
    except Exception as e:
        handle_command_error(e, "RLS pull")
    if changed and not args.write:
        sys.exit(1)


def handle_schema_command(args: argparse.Namespace) -> None:
    """`fabdep schema`: print fabdep.yml's JSON Schema, or write it to a file."""
    if args.output:
        # Not a shell redirect: Windows PowerShell 5.1 would write UTF-16.
        try:
            _ = args.output.write_text(schema_text(), encoding="utf-8")
        except OSError as e:
            handle_command_error(e, "schema")
    else:
        print(schema_text(), end="")


_HANDLERS: dict[str, Callable[[argparse.Namespace], None]] = {
    "auth": handle_auth_command,
    "deploy": handle_deploy_command,
    "detect": handle_detect_command,
    "init": handle_init_command,
    "plan": handle_plan_command,
    "refresh": handle_refresh_command,
    "rls": handle_rls_command,
    "schema": handle_schema_command,
}


def main() -> None:
    """CLI entry point: run the command the arguments name (deploy by default)."""
    # A console that can't encode a character (✓ on a Windows cp1252 console)
    # prints '?' for it instead of failing the command.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")
    args = parse_args()
    set_auth_mode(getattr(args, "auth", None))
    _HANDLERS[args.command](args)
