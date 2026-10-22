"""Workspace deployment abstraction for Microsoft Fabric workspaces."""

# pyright: reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from fabric_cicd import (
    FabricWorkspace,
    append_feature_flag,
    publish_all_items,
    unpublish_all_orphan_items,
)

from .client import (
    create_workspace,
    find_workspace_id_by_name,
    get_default_capacity_id,
    get_workspace_id_by_name,
    list_workspace_items,
    list_workspaces,
)
from .config import FabdepConfig
from .detect import ParameterDetector
from .git import changed_paths_in, latest_pr_commit
from .identity import apply_auth_settings, fabric_credential
from .log import get_logger, patch_fabric_cicd_logger
from .params import (
    ParameterFile,
    Rule,
    apply_manual_overrides,
    resolve_sqldatabase_hostname_params,
    resolve_warehouse_endpoint_params,
)
from .rls import RlsCheck, check_deploy_rls, rls_verdict
from .scope import (
    RepoItem,
    find_in_item_files,
    items_for_changed_paths,
    repo_item_types,
    repo_items,
    resolve_item_scope,
    validate_include_items,
)

# Sentinel for a bare `--pr`: search back from HEAD for the PR commit, rather
# than treating a specific commit-ish as the PR.
PR_HEAD = "HEAD"


@dataclass(frozen=True)
class DeployScope:
    """What a deploy publishes: fabric-cicd's filters and the repo items they select."""

    item_types: list[str] | None  # fabric-cicd's item_type_in_scope; None: every type
    include_items: list[str] | None  # its items_to_include, all in the repo; None: no filter
    items: list[RepoItem]  # the repo items the deploy publishes
    cleanup: bool  # whether orphan cleanup runs afterwards


@dataclass(frozen=True)
class Verdict:
    """A deploy guard's verdict: "pass", "warn" (the deploy goes ahead and logs
    `message`) or "block" (the deploy stops with `message`)."""

    status: str
    message: str = ""
    findings: tuple[str, ...] = ()  # what the guard found, one entry each
    models: tuple[RlsCheck, ...] = ()  # the RLS guard's check of each in-scope model


@dataclass
class WorkspaceDeployer:
    """
    Main deployer class for Fabric workspace items.
    Encapsulates configuration extraction, validation, and deployment logic.

    Attributes:
            repo_dir: Path to the repository directory containing Fabric workspace items
            logger_name: Optional custom logger name (defaults to module name)
            branch_override: Override git branch for workspace name resolution (defaults to current branch)
            new_workspace: Create the target workspace; fails if it already exists
            skip_lakehouse: Skip lakehouse parameter replacement (for feature branches sharing dev lakehouse)
            environment: fabric-cicd environment name (defaults to fabdep.yml's, else the branch)
            dry_run: Make read calls only, for `fabdep plan`: work out whether the
                deploy would create the workspace instead of creating it

    IMPORTANT: Initialization has side effects!
    - Reads fabdep.yml from disk
    - Makes network calls to Fabric API (workspace ID resolution or creation,
      the repo's sibling workspaces, and the lakehouses of those the content
      references)
    - Scans workspace files to detect source IDs (stored, not written to disk)

    The parameter file is written to a temporary directory during deploy(),
    never into the repo.
    """

    repo_dir: Path | str
    logger_name: str | None = None
    branch_override: str | None = None  # Override git branch for workspace resolution
    new_workspace: bool = False  # Create the workspace; refuse an existing one
    skip_lakehouse: bool = False  # Skip its parameter replacement (feature branch mode)
    # fabric-cicd environment: selects environment-keyed replace_values and the
    # Variable Library value set. Empty means fabdep.yml's choice for the branch.
    environment: str = ""
    dry_run: bool = False  # Read calls only (fabdep plan); such a deployer can't deploy

    # Configuration fields - auto-loaded in __post_init__
    # Empty in a dry run whose deploy would create the workspace.
    workspace_id: str = field(init=False, repr=False)
    workspace_name: str = field(init=False, repr=False)  # the target's display name
    creates_workspace: bool = field(init=False, default=False, repr=False)
    capacity_id: str = field(init=False, default="", repr=False)  # a created workspace's
    # This repo's workspaces (target + siblings) and the lakehouses of the
    # siblings the content references, by item id — the detector's anchors
    # (see _resolve_source_anchors). `source_workspaces` is the referenced
    # siblings by id → display name, for the run summary.
    source_workspace_ids: set[str] = field(init=False, default_factory=set, repr=False)
    source_workspaces: dict[str, str] = field(init=False, default_factory=dict, repr=False)
    source_lakehouse_ids: dict[str, str] = field(init=False, default_factory=dict, repr=False)

    _repo_dir_resolved: Path = field(init=False, repr=False)
    config: FabdepConfig = field(init=False, repr=False)
    _paramfile: ParameterFile = field(init=False, repr=False)
    _detector: ParameterDetector | None = field(default=None, init=False, repr=False)
    # The items the last deploy() published, as '<displayName>.<type>'.
    deployed_items: list[str] = field(default_factory=list, init=False, repr=False)
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
        4. Makes network calls to the Fabric REST API: the target workspace ID
           (creating the workspace when asked to), the repo's sibling
           workspaces, and lakehouses of referenced siblings
        5. Scans workspace files to detect source IDs (stored in memory, not written to disk)

        Parameter file writing is deferred to deploy() when the item scope is known.
        """
        self._repo_dir_resolved = Path(self.repo_dir).resolve()
        self.logger = get_logger(self.logger_name or __name__)
        self._paramfile = ParameterFile(self._repo_dir_resolved)

        self.config = FabdepConfig.from_path(
            self._repo_dir_resolved, branch_override=self.branch_override
        )
        self.workspace_name = self.config.workspace_name
        self.environment = self.environment or self.config.environment
        # fabdep.yml's identity keys apply before any Fabric API call.
        apply_auth_settings(self.config.auth, self.logger)
        self._resolve_workspace_id()
        self._resolve_source_anchors()
        self._detect_parameters()

    def _resolve_workspace_id(self) -> None:
        """Look up the target workspace, creating it first with `new_workspace`
        or, when fabdep.yml sets `create: true` for the branch, if it is missing.

        A dry run only works out whether the deploy would create it, and leaves
        `workspace_id` empty if so.

        Raises:
            ValueError: With `new_workspace`, if it already exists.
        """
        self.creates_workspace = self.new_workspace or (
            self.config.create_workspace and find_workspace_id_by_name(self.workspace_name) is None
        )
        if not self.creates_workspace:
            self.workspace_id = get_workspace_id_by_name(self.workspace_name)
            return
        self.capacity_id = self.config.capacity_id or get_default_capacity_id()
        if not self.dry_run:
            self.workspace_id = self._create_workspace()
        elif self.new_workspace and find_workspace_id_by_name(self.workspace_name) is not None:
            raise ValueError(self._exists_message())
        else:
            self.workspace_id = ""

    def _create_workspace(self) -> str:
        """Create the target workspace on fabdep.yml's capacity, else the only active one.

        Raises:
            ValueError: If it already exists: `new_workspace` never reuses one.
        """
        self.logger.info(f"Creating workspace: {self.workspace_name}")
        try:
            workspace_id = create_workspace(
                workspace_name=self.workspace_name, capacity_id=self.capacity_id
            )
        except ValueError as e:
            if "already exists" in str(e):
                raise ValueError(self._exists_message()) from None
            raise
        self.logger.info(f"✓ Workspace created: {workspace_id}")
        return workspace_id

    def _exists_message(self) -> str:
        return (
            f"Workspace '{self.workspace_name}' already exists.\n\n"
            "The -N/--new-workspace flag is only for creating new workspaces.\n"
            "To deploy to an existing workspace, remove the -N flag."
        )

    def _resolve_source_anchors(self) -> None:
        """Anchor the ids of this repo's other workspaces as environment ids.

        Every workspace this fabdep.yml deploys to (see
        `FabdepConfig.is_repo_workspace`) is one of the repo's environments, and
        content exported from any of them carries that environment's ids. A
        deploy rewrites those to the target's; any other workspace id deploys
        unchanged. Fabric's lakehouse auto-binding writes the null UUID where
        notebook metadata once named the source workspace, so the set comes from
        the workspace list rather than the content, whatever branch is checked
        out (a `main` checkout after a dev-to-main merge still carries the dev
        workspace's ids). The lakehouses of each sibling the content references
        are listed too, so their ids map where notebook code still carries them
        (abfss paths). A failed lookup is a warning, not an error.
        """
        self.source_workspace_ids = {self.workspace_id} if self.workspace_id else set()
        self.source_workspaces = {}
        self.source_lakehouse_ids = {}
        try:
            workspaces = list_workspaces()
        except Exception as e:
            self.logger.warning(
                f"Could not list workspaces ({e}), so ids from this repo's other "
                f"workspaces are replaced only where item metadata names their "
                f"workspace; other workspace ids deploy unchanged."
            )
            return

        siblings = {
            ws["id"]: ws["displayName"]
            for ws in workspaces
            if ws.get("id")
            and ws["id"] != self.workspace_id
            and self.config.is_repo_workspace(ws.get("displayName") or "")
        }
        if not siblings:
            return
        self.source_workspace_ids |= set(siblings)

        referenced = {
            ws_id
            for ws_id, files in find_in_item_files(self._repo_dir_resolved, siblings).items()
            if files
        }
        for ws_id in sorted(referenced):
            name = siblings[ws_id]
            self.source_workspaces[ws_id] = name
            try:
                lakehouses = list_workspace_items(ws_id, "Lakehouse")
            except Exception as e:
                self.logger.warning(f"Could not list lakehouses in '{name}': {e}")
                continue
            for item in lakehouses:
                if item.get("id") and item.get("displayName"):
                    self.source_lakehouse_ids[item["id"]] = item["displayName"]

    def _detect_parameters(self) -> None:
        """
        Detect parameters from workspace files and store for later use.

        Runs during __post_init__. Detection and storage only — no file I/O.
        The parameter file is written later, during deploy().
        """
        detector = ParameterDetector(
            repo_dir=self._repo_dir_resolved,
            logger_name=self.logger.name,
            source_workspace_ids=self.source_workspace_ids,
            source_lakehouse_ids=self.source_lakehouse_ids,
        )
        detector.detect_all()

        self._skipped_lakehouse_count = 0
        if self.skip_lakehouse:
            self._skipped_lakehouse_count = len(detector.filter_lakehouse_params())
            if detector.auto_bound_notebooks:
                # fabric-cicd swaps a lakehouse logicalId for the target's
                # lakehouse before any parameter applies, so dropping the
                # Lakehouse ID rules changes nothing for these notebooks.
                names = ", ".join(detector.auto_bound_notebooks)
                self.logger.warning(
                    f"--skip-lakehouse cannot keep {len(detector.auto_bound_notebooks)} "
                    f"auto-bound notebook(s) on the source lakehouse: their "
                    f"metadata names the lakehouse by logicalId, which fabric-cicd "
                    f"resolves to this workspace's lakehouse regardless: {names}"
                )

        self._manual_count = apply_manual_overrides(
            detector, self.config.manual_parameters, self.logger, self.environment
        )

        # MountedWarehouse bindings need a literal GUID from the target
        # workspace (no fabric-cicd dynamic exists). Manual overrides above
        # win; same-workspace deploys skip replacement entirely.
        if not detector.should_skip_replacement(self.workspace_id):
            target = self.workspace_id or None  # None: a dry run's workspace-to-be
            _ = resolve_warehouse_endpoint_params(detector, target, self.logger)
            # SQL database hostnames also resolve to literals: fabric-cicd's
            # $sqlendpoint dynamic returns serverFqdn with a ",1433" port
            # suffix that must not land in bare-hostname find_values.
            _ = resolve_sqldatabase_hostname_params(detector, target, self.logger)

        self._detector = detector

    def guard_verdicts(
        self,
        scope: DeployScope,
        fail_on_stale: bool = False,
        allow_rls_change: bool = False,
        allow_unbound_udf: bool = False,
    ) -> Iterator[tuple[str, Verdict]]:
        """Each guard's verdict on deploying `scope`, by name, in the order a deploy checks them.

        Lazy: a deploy stops at the first block, before the RLS guard reads the
        target. The options are deploy()'s.
        """
        yield "fail_on_stale", self._stale_verdict(fail_on_stale)
        yield "unbound_udf", self._udf_verdict(scope, allow_unbound_udf)
        yield "rls", self._rls_verdict(scope, allow_rls_change)

    def _stale_verdict(self, fail_on_stale: bool) -> Verdict:
        """Notebook warehouse attachments that can't be remapped to the target workspace."""
        bindings = self._detector.unresolved_warehouse_bindings if self._detector else []
        if not bindings:
            return Verdict("pass")
        files = tuple(sorted({f for f, _, _ in bindings}))
        found = (
            f"{len(bindings)} warehouse attachment(s) would deploy with source-workspace "
            f"GUIDs: {', '.join(files)}."
        )
        if fail_on_stale:
            return Verdict("block", f"--fail-on-stale: {found} See warnings above.", files)
        return Verdict("warn", f"{found} (--fail-on-stale makes this an error)", files)

    def _udf_verdict(self, scope: DeployScope, allow_unbound_udf: bool) -> Verdict:
        """Report buttons that would ship pointing at the source workspace's function.

        A bound button whose function name resolves to no repo
        `.UserDataFunction` item deploys still calling the SOURCE workspace's
        function — a silent cross-workspace leak. It blocks when its report is
        published, unless --allow-unbound-udf; a report the deploy doesn't
        publish can't leak.
        """
        reports = {item.name for item in scope.items if item.type == "Report"}
        skipped = self._detector.udf_skipped if self._detector else []
        in_scope = [s for s in skipped if s[0] in reports]
        if not in_scope:
            return Verdict("pass")
        findings = tuple(
            sorted({f"'{fn or '(unnamed)'}' in '{report}'" for report, fn, _, _ in in_scope})
        )
        if allow_unbound_udf:
            return Verdict(
                "warn",
                f"--allow-unbound-udf: {len(in_scope)} User Data Function "
                f"button(s) deploy still pointing at the source workspace's "
                f"function. See warnings above.",
                findings,
            )
        return Verdict(
            "block",
            f"{len(in_scope)} User Data Function button(s) cannot be rebound "
            f"to the target workspace and would deploy still calling the "
            f"source workspace's function: {', '.join(findings)}. Fix the button's "
            f"function binding (or the missing/ambiguous .UserDataFunction "
            f"item), or pass --allow-unbound-udf to deploy as-is. See "
            f"warnings above.",
            findings,
        )

    def _rls_verdict(self, scope: DeployScope, allow_rls_change: bool) -> Verdict:
        """Live row-level security the published semantic models would change (see `rls`)."""
        checks = check_deploy_rls(
            self.workspace_id or None,
            self._repo_dir_resolved,
            [item.name for item in scope.items if item.type == "SemanticModel"],
            allow_rls_change,
            # A dry run reports the checks instead of logging them.
            logger=None if self.dry_run else self.logger,
        )
        status, message = rls_verdict(checks)
        return Verdict(status, message, models=tuple(checks))

    def parameter_rules(self) -> list[Rule]:
        """The find_replace rules a deploy hands fabric-cicd, in the order it applies them."""
        if self._detector is None:
            return []
        _ = self._paramfile.prepare(
            self._detector,
            self.workspace_id,
            self.environment,
            self._manual_count,
            self._skipped_lakehouse_count,
        )
        return self._paramfile.rules

    def _items_from_pr(self, pr: str) -> list[str]:
        """Resolve `--pr` to the item list the pull request actually touched.

        Mirrors reading the PR diff by hand before a deploy: find the merge (or
        squash) commit, take what it changed, and keep the items that own those
        files. Anything ambiguous is reported rather than silently widened —
        deploying more than the PR changed is the failure mode this flag exists
        to avoid.

        Raises:
            RuntimeError: If no PR commit is found, or it touched no item.
        """
        if pr == PR_HEAD:
            found = latest_pr_commit(self._repo_dir_resolved)
            if not found:
                raise RuntimeError(
                    "--pr found no pull-request commit in the last 50 commits (one "
                    "worded the way Azure DevOps, GitHub, GitLab or Bitbucket word a "
                    "merged pull request). Pass the commit explicitly with --pr "
                    "<commit-ish>."
                )
            sha, subject = found
        else:
            sha, subject = pr, ""

        changed = changed_paths_in(self._repo_dir_resolved, sha)
        matched, unmatched = items_for_changed_paths(changed, self._repo_dir_resolved)

        label = f"{sha[:8]}" + (f" ({subject})" if subject else "")
        self.logger.info(
            f"--pr: {label} changed {len(changed)} file(s) across {len(matched)} item(s)."
        )
        if unmatched:
            # Deleted items land here too — their directory is gone, so there
            # is nothing to publish and `--pr` never unpublishes.
            self.logger.info(
                f"--pr: {len(unmatched)} changed path(s) belong to no item "
                f"(repo config, docs, or an item the PR deleted) and are "
                f"ignored: {', '.join(sorted(unmatched)[:5])}"
                + (" ..." if len(unmatched) > 5 else "")
            )
        if not matched:
            raise RuntimeError(
                f"--pr: {label} changed no deployable item, so there is "
                "nothing to publish. Deploy without --pr to push the whole "
                "workspace."
            )
        return matched

    def resolve_scope(
        self,
        no_cleanup: bool = False,
        item_types: list[str] | None = None,
        exclude_types: list[str] | None = None,
        include_items: list[str] | None = None,
        pr: str | None = None,
    ) -> DeployScope:
        """Work out what a deploy with these options publishes (see `deploy`).

        Raises:
            RuntimeError: If `pr` is combined with `include_items`, no PR commit
                is found, the PR touched no deployable item, or no
                `include_items` entry names a repo item.
        """
        if pr:
            if include_items:
                raise RuntimeError(
                    "--pr and --include-items both set the item filter. Use "
                    "--pr to deploy what a PR touched, or --include-items to "
                    "name items yourself — not both."
                )
            include_items = self._items_from_pr(pr)

        if include_items:
            self.logger.warning(
                "Name-based item filtering is experimental. "
                "Selective deployment may cause dependency issues. Use at your own risk."
            )
            self.logger.warning("Orphan cleanup disabled when using --include-items for safety.")
            # Fail fast on a name mismatch instead of silently deploying nothing.
            include_items = validate_include_items(
                include_items, self._repo_dir_resolved, self.logger
            )

        types = resolve_item_scope(item_types, exclude_types, include_items)
        return DeployScope(
            item_types=types,
            include_items=include_items or None,
            items=[
                item
                for item in repo_items(self._repo_dir_resolved)
                if (types is None or item.type in types)
                and (not include_items or item.key in include_items)
            ],
            # include_items + unpublish is a footgun: if names don't match,
            # nothing publishes and orphan cleanup would wipe the workspace.
            cleanup=not no_cleanup and not include_items,
        )

    def deploy(
        self,
        no_cleanup: bool = False,
        item_types: list[str] | None = None,
        exclude_types: list[str] | None = None,
        include_items: list[str] | None = None,
        fail_on_stale: bool = False,
        allow_rls_change: bool = False,
        allow_unbound_udf: bool = False,
        pr: str | None = None,
    ) -> None:
        """
        Deploy Fabric workspace items.

        Args:
            no_cleanup: Skip unpublishing orphaned items
            item_types: Specific item types to deploy
            exclude_types: Item types to exclude from deployment
            include_items: Include only specific items (format: "ItemName.ItemType")
            fail_on_stale: Raise instead of warning when a notebook has a
                warehouse attachment that can't be remapped to the target
                workspace. Intended for CI, where the inline warning would get
                buried in logs.
            allow_rls_change: Permit a deploy to change a model's live row-level
                security. By default, if the repo carries RLS and the target's
                live RLS differs, the deploy is blocked (someone may have edited
                RLS directly in the target).
            allow_unbound_udf: Deploy report buttons whose User Data Function
                reference could not be rebound to the target workspace. By
                default an in-scope report carrying such a button blocks the
                deploy, because the button would ship still calling the SOURCE
                workspace's function.
            pr: Restrict the deploy to the items a pull request touched.
                PR_HEAD ("HEAD") searches back from HEAD for the PR commit; any
                other value is used as the commit-ish directly. Resolves into
                `include_items`, so it cannot be combined with it.

        Raises:
            RuntimeError: If the scope can't be resolved (see `resolve_scope`)
                or a guard blocks the deploy.
        """
        if self.dry_run:
            raise RuntimeError("This deployer is for a dry run: it can't deploy.")

        for ws_id, name in sorted(self.source_workspaces.items(), key=lambda kv: kv[1]):
            self.logger.info(f"Source Workspace: {name} ({ws_id})")
        self.logger.info(f"Target Workspace: '{self.workspace_name}' ({self.workspace_id})")

        scope = self.resolve_scope(
            no_cleanup=no_cleanup,
            item_types=item_types,
            exclude_types=exclude_types,
            include_items=include_items,
            pr=pr,
        )
        item_type_in_scope, include_items = scope.item_types, scope.include_items

        # Every guard runs before anything is written.
        for _, verdict in self.guard_verdicts(
            scope, fail_on_stale, allow_rls_change, allow_unbound_udf
        ):
            if verdict.status == "block":
                raise RuntimeError(verdict.message)
            if verdict.status == "warn":
                self.logger.warning(verdict.message)

        if self._detector is not None:
            self._paramfile.write(
                self._detector,
                self.workspace_id,
                self.environment,
                self._manual_count,
                self._skipped_lakehouse_count,
            )

        for line in self._paramfile.summary:
            self.logger.info(line)

        if item_type_in_scope:
            types_to_deploy = sorted(
                set(item_type_in_scope) & repo_item_types(self._repo_dir_resolved)
            )
            if types_to_deploy:
                self.logger.info(f"Item types in scope: {', '.join(types_to_deploy)}")
            else:
                self.logger.warning("No matching item types found in repository")
        if include_items:
            self.logger.info(f"Including items: {', '.join(include_items)}")

        # fabric-cicd configures its own logger at import time; swap in our
        # formatter that omits timestamps on header lines.
        patch_fabric_cicd_logger()

        try:
            self._deploy_items(scope)
            self.deployed_items = [item.key for item in scope.items]
            # One-line confirmation of what actually deployed — fabric-cicd's
            # "Skipping ... due to items_to_include" noise otherwise buries it.
            if include_items:
                self.logger.info(
                    f"Deployed {len(self.deployed_items)} item(s): {', '.join(self.deployed_items)}"
                )
            self.logger.info("Deployment complete!")
        finally:
            self._paramfile.cleanup()

    def _deploy_items(self, scope: DeployScope) -> None:
        """Publish the scope's items with fabric-cicd, then clean up orphans if it says to."""
        if scope.include_items:
            append_feature_flag("enable_experimental_features")
            append_feature_flag("enable_items_to_include")
        # The REST calls' credential, so fabric-cicd deploys as the same checked identity.
        target_workspace = FabricWorkspace(
            workspace_id=self.workspace_id,
            repository_directory=str(self._repo_dir_resolved),
            environment=self.environment,
            item_type_in_scope=scope.item_types,
            token_credential=fabric_credential(),
            parameter_file_path=self._paramfile.path,
        )

        _ = publish_all_items(target_workspace, items_to_include=scope.include_items)
        if scope.cleanup:
            _ = unpublish_all_orphan_items(target_workspace)
