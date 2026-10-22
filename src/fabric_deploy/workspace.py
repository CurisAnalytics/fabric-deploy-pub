"""Workspace deployment abstraction for Microsoft Fabric workspaces."""

# pyright: reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false

import logging
import re
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
    get_default_capacity_id,
    get_workspace_id_by_name,
    list_workspace_items,
    list_workspaces,
)
from .config import FabdepConfig
from .detect import ParameterDetector, find_in_item_files
from .git import changed_paths_in, latest_pr_commit
from .identity import apply_tenant_pin, fabric_credential
from .log import get_logger, patch_fabric_cicd_logger
from .params import (
    ParameterFile,
    apply_manual_overrides,
    resolve_sqldatabase_hostname_params,
    resolve_warehouse_endpoint_params,
)
from .scope import (
    items_for_changed_paths,
    repo_item_types,
    resolve_item_scope,
    validate_include_items,
)

# Sentinel for a bare `--pr`: search back from HEAD for the PR commit, rather
# than treating a specific commit-ish as the PR.
PR_HEAD = "HEAD"


@dataclass
class WorkspaceDeployer:
    """
    Main deployer class for Fabric workspace items.
    Encapsulates configuration extraction, validation, and deployment logic.

    Attributes:
            repo_dir: Path to the repository directory containing Fabric workspace items
            logger_name: Optional custom logger name (defaults to module name)
            branch_override: Override git branch for workspace name resolution (defaults to current branch)
            new_workspace: Create workspace if it doesn't exist
            skip_lakehouse: Skip lakehouse parameter replacement (for feature branches sharing dev lakehouse)

    IMPORTANT: Initialization has side effects!
    - Reads fabdep.yml from disk
    - Makes network calls to Fabric API (workspace ID resolution, the repo's
      sibling workspaces, and the lakehouses of those the content references)
    - Scans workspace files to detect source IDs (stored, not written to disk)

    The parameter file is written to a temporary directory during deploy(),
    never into the repo.
    """

    repo_dir: Path | str
    logger_name: str | None = None
    branch_override: str | None = None  # Override git branch for workspace resolution
    new_workspace: bool = False  # Create workspace if it doesn't exist
    skip_lakehouse: bool = False  # Skip its parameter replacement (feature branch mode)
    # fabric-cicd environment: selects environment-keyed replace_values and the
    # Variable Library value set. Empty means the target branch name.
    environment: str = ""

    # Configuration fields - auto-loaded in __post_init__
    workspace_id: str = field(init=False, repr=False)
    workspace_name: str = field(
        init=False, repr=False
    )  # Resolved workspace name (base_name [branch])
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
    # Item scope a deploy() call resolved to (set by --pr / --include-items).
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
        4. Makes network calls to the Fabric REST API: the target workspace ID,
           the repo's sibling workspaces, and lakehouses of referenced siblings
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
        self.environment = self.environment or self.config.branch
        self._apply_tenant_pin()
        self._resolve_workspace_id()
        self._resolve_source_anchors()
        self._detect_parameters()

    def _apply_tenant_pin(self) -> None:
        """Adopt fabdep.yml's identity keys before any Fabric API call."""
        apply_tenant_pin(self.config.tenant, self.logger)

    def _resolve_workspace_id(self) -> None:
        """Look up (or create) the Fabric workspace and store its ID.

        - With `new_workspace=True`, creates the workspace first using the
          configured (or default) capacity.
        - With `from_yaml=False` (no fabdep.yml), the directory-name fallback
          is in play: if the lookup fails we raise FileNotFoundError with
          the same actionable message the prior implementation produced.
        """
        if self.new_workspace:
            capacity_id = self.config.capacity_id
            if not capacity_id:
                capacity_id = get_default_capacity_id()
            self._create_workspace_if_needed(capacity_id)

        try:
            self.workspace_id = get_workspace_id_by_name(self.workspace_name)
        except ValueError:
            # No fabdep.yml AND the dirname-derived workspace doesn't exist —
            # surface the actionable "create a fabdep.yml" message.
            if not self.config.from_yaml:
                raise FileNotFoundError(
                    f"fabdep.yml not found in {self._repo_dir_resolved}.\n\n"
                    "Create a fabdep.yml file with:\n"
                    '  workspace_base_name: "your_workspace"\n'
                    '  capacity_id: "your_capacity_id"  # Optional\n\n'
                    "The workspace will be resolved from: {base_name} [{branch}]\n\n"
                    "Note: This is separate from parameter.yml which is used by fabric-cicd."
                ) from None
            raise

    def _create_workspace_if_needed(self, capacity_id: str) -> None:
        """
        Create workspace if it doesn't exist.

        Args:
            capacity_id: Fabric capacity ID for workspace creation

        Raises:
            ValueError: If workspace already exists (prevents accidental overwrites)
        """
        # Try to create workspace (will raise ValueError if already exists)
        self.logger.info(f"Creating workspace: {self.workspace_name}")
        try:
            workspace_id = create_workspace(
                workspace_name=self.workspace_name,
                capacity_id=capacity_id,
            )
            self.logger.info(f"✓ Workspace created: {workspace_id}")
        except ValueError as e:
            # Refuse `-N` against existing workspaces — protects against silent reuse.
            if "already exists" in str(e):
                raise ValueError(
                    f"Workspace '{self.workspace_name}' already exists.\n\n"
                    "The -N/--new-workspace flag is only for creating new workspaces.\n"
                    "To deploy to an existing workspace, remove the -N flag."
                ) from None
            raise
        except Exception as e:
            self.logger.error(f"Failed to create workspace: {e}")
            raise

    # One of this repo's workspaces: `{base} [{branch}]`.
    @staticmethod
    def _sibling_pattern(base_name: str) -> re.Pattern[str]:
        return re.compile(rf"^{re.escape(base_name)} \[.+\]$", re.IGNORECASE)

    def _resolve_source_anchors(self) -> None:
        """Anchor the ids of this repo's other workspaces as environment ids.

        fabdep's model is `{base} [{branch}]`: every workspace with this repo's
        base name is one of its environments, and content exported from any of
        them carries that environment's ids. A deploy rewrites those to the
        target's; a workspace id outside this family belongs to another repo
        and deploys verbatim. Legacy notebook metadata let detection
        recover the source workspace from content alone; Fabric's lakehouse
        auto-binding in git replaces it with the null UUID, so the family is
        resolved here from the workspace list instead — whatever branch is
        checked out (a `prod` checkout after a dev→prod merge still carries
        DEV's ids). The lakehouses of each sibling the content references are
        listed too, so their physical ids map where notebook code still
        carries them (abfss literals). A lookup that fails is a warning, not
        an error: detection falls back to whatever anchors the content has.
        """
        self.source_workspace_ids = {self.workspace_id}
        self.source_workspaces = {}
        self.source_lakehouse_ids = {}
        try:
            workspaces = list_workspaces()
        except Exception as e:
            self.logger.warning(
                f"Could not list workspaces to anchor this repo's environments "
                f"({e}); workspaceIds that no notebook, model or report anchors "
                f"deploy verbatim."
            )
            return

        base_name = self.config.workspace_base_name or self._repo_dir_resolved.name
        is_sibling = self._sibling_pattern(base_name).match
        siblings = {
            ws["id"]: ws["displayName"]
            for ws in workspaces
            if ws.get("id")
            and ws["id"] != self.workspace_id
            and is_sibling(ws.get("displayName") or "")
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
            _ = resolve_warehouse_endpoint_params(detector, self.workspace_id, self.logger)
            # SQL database hostnames also resolve to literals: fabric-cicd's
            # $sqlendpoint dynamic returns serverFqdn with a ",1433" port
            # suffix that must not land in bare-hostname find_values.
            _ = resolve_sqldatabase_hostname_params(detector, self.workspace_id, self.logger)

        self._detector = detector

    def _guard_rls(
        self,
        item_type_in_scope: list[str] | None,
        include_items: list[str] | None,
        allow_rls_change: bool,
    ) -> None:
        """Block a deploy that would change a target model's live RLS.

        Resolves which SemanticModels this deploy publishes and delegates to
        `guard_deploy_rls`, which compares each one's roles with the target's.
        """
        from .rls import guard_deploy_rls

        if item_type_in_scope is not None and "SemanticModel" not in item_type_in_scope:
            models = []  # SemanticModel not in scope -> skip the guard
        elif include_items:
            models = [it.rsplit(".", 1)[0] for it in include_items if it.endswith(".SemanticModel")]
        else:
            models = None  # every SemanticModel in the repo is in scope

        guard_deploy_rls(
            workspace_id=self.workspace_id,
            repo_dir=self._repo_dir_resolved,
            model_names=models,
            allow_change=allow_rls_change,
            logger=self.logger,
        )

    def _guard_udf_bindings(
        self,
        item_type_in_scope: list[str] | None,
        include_items: list[str] | None,
        allow_unbound_udf: bool,
    ) -> None:
        """Block a deploy that would ship a UDF button pointing off-target.

        A bound button whose function name resolves to no repo
        `.UserDataFunction` item deploys still calling the SOURCE workspace's
        function — a silent cross-workspace leak. Fatal when the report is in
        deploy scope, unless --allow-unbound-udf. Reports outside the scope
        aren't deploying, so their buttons can't leak and don't block.
        """
        if not (self._detector and self._detector.udf_skipped):
            return
        skipped = self._detector.udf_skipped
        if include_items:
            in_scope = [s for s in skipped if f"{s[0]}.Report" in include_items]
        elif item_type_in_scope is None or "Report" in item_type_in_scope:
            in_scope = skipped
        else:
            in_scope = []
        if not in_scope:
            return
        if allow_unbound_udf:
            self.logger.warning(
                f"--allow-unbound-udf: {len(in_scope)} User Data Function "
                f"button(s) deploy still pointing at the source workspace's "
                f"function. See warnings above."
            )
            return
        details = ", ".join(
            sorted({f"'{fn or '(unnamed)'}' in '{report}'" for report, fn, _, _ in in_scope})
        )
        raise RuntimeError(
            f"{len(in_scope)} User Data Function button(s) cannot be rebound "
            f"to the target workspace and would deploy still calling the "
            f"source workspace's function: {details}. Fix the button's "
            f"function binding (or the missing/ambiguous .UserDataFunction "
            f"item), or pass --allow-unbound-udf to deploy as-is. See "
            f"warnings above."
        )

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
                    "--pr found no pull-request commit in the last 50 commits "
                    "(looked for a merge commit, 'Merged PR N:' from Azure "
                    "DevOps, or GitHub's 'Merge pull request #N' / '(#N)'). "
                    "Pass the commit explicitly with --pr <commit-ish>."
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
                workspace. Intended for CI / autonomous
                agent loops where the inline warning would get buried in logs.
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
            RuntimeError: If `pr` is combined with `include_items`, no PR commit
                is found, or the PR touched no deployable item.
        """

        for ws_id, name in sorted(self.source_workspaces.items(), key=lambda kv: kv[1]):
            self.logger.info(f"Source Workspace: {name} ({ws_id})")
        self.logger.info(f"Target Workspace: '{self.workspace_name}' ({self.workspace_id})")

        if pr:
            if include_items:
                raise RuntimeError(
                    "--pr and --include-items both set the item filter. Use "
                    "--pr to deploy what a PR touched, or --include-items to "
                    "name items yourself — not both."
                )
            include_items = self._items_from_pr(pr)
        # Published as the resolved scope so callers (e.g. --frame) can act on
        # what a --pr run actually selected instead of re-deriving it.
        self.deployed_items = list(include_items or [])

        if include_items:
            append_feature_flag("enable_experimental_features")
            append_feature_flag("enable_items_to_include")
            self.logger.warning(
                "Name-based item filtering is experimental. "
                "Selective deployment may cause dependency issues. Use at your own risk."
            )
            self.logger.warning("Orphan cleanup disabled when using --include-items for safety.")
            # Fail fast on a name mismatch instead of silently deploying nothing.
            include_items = validate_include_items(
                include_items, self._repo_dir_resolved, self.logger
            )

        item_type_in_scope = resolve_item_scope(
            item_types, exclude_types, include_items, self.logger
        )

        if fail_on_stale and self._detector and self._detector.unresolved_warehouse_bindings:
            files = ", ".join(
                sorted({f for f, _, _ in self._detector.unresolved_warehouse_bindings})
            )
            raise RuntimeError(
                f"--fail-on-stale: "
                f"{len(self._detector.unresolved_warehouse_bindings)} warehouse "
                f"attachment(s) would deploy with source-workspace GUIDs: {files}. "
                f"See warnings above."
            )

        self._guard_udf_bindings(item_type_in_scope, include_items, allow_unbound_udf)

        # Guard live RLS before any write: any difference between an in-scope
        # model's repo roles and the target's blocks the deploy unless allowed.
        self._guard_rls(item_type_in_scope, include_items, allow_rls_change)

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
            self._deploy_items(
                no_cleanup=no_cleanup,
                item_types=item_type_in_scope,
                include_items=include_items,
            )
            # One-line confirmation of what actually deployed — fabric-cicd's
            # "Skipping ... due to items_to_include" noise otherwise buries it.
            # Filtered by the type scope: an item excluded via -x/--item-types
            # stays in include_items but was never published.
            if include_items:
                deployed = [
                    it
                    for it in include_items
                    if item_type_in_scope is None or it.rsplit(".", 1)[-1] in item_type_in_scope
                ]
                self.logger.info(f"Deployed {len(deployed)} item(s): {', '.join(deployed)}")
            self.logger.info("Deployment complete!")
        finally:
            self._paramfile.cleanup()

    def _deploy_items(
        self,
        no_cleanup: bool = False,
        item_types: list[str] | None = None,
        include_items: list[str] | None = None,
    ) -> None:
        """
        Deploy items to workspace using fabric-cicd.

        Args:
            no_cleanup: Skip unpublishing orphaned items
            item_types: Specific item types to deploy
            include_items: Include only specific items (format: "ItemName.ItemType")
        """
        # The REST calls' credential, so fabric-cicd deploys as the same checked identity.
        target_workspace = FabricWorkspace(
            workspace_id=self.workspace_id,
            repository_directory=str(self._repo_dir_resolved),
            environment=self.environment,
            item_type_in_scope=item_types,
            token_credential=fabric_credential(),
            parameter_file_path=self._paramfile.path,
        )

        _ = publish_all_items(target_workspace, items_to_include=include_items)

        # include_items + unpublish is a footgun: if names don't match, nothing
        # publishes and orphan cleanup would wipe the workspace.
        if not no_cleanup and not include_items:
            unpublish_all_orphan_items(target_workspace)
