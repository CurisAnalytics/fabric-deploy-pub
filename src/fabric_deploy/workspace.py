"""Workspace deployment abstraction for Microsoft Fabric workspaces."""

# pyright: reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false

import logging
from dataclasses import dataclass, field
from datetime import datetime
from json import loads
from pathlib import Path

from fabric_cicd import (
    FabricWorkspace,
    append_feature_flag,
    publish_all_items,
    unpublish_all_orphan_items,
)
from fabric_cicd._common._logging import log_header

from .client import (
    create_workspace,
    get_default_capacity_id,
    get_workspace_id_by_name,
)
from .config import FabdepConfig
from .detect import ParameterDetector
from .git import current_branch
from .identity import apply_tenant_pin, fabric_credential
from .log import get_logger, is_transient_error, patch_fabric_cicd_logger
from .params import (
    ParameterFile,
    apply_manual_overrides,
    resolve_warehouse_endpoint_params,
)
from .scope import repo_item_types, resolve_item_scope, validate_include_items


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
            max_retries: Max retries for transient errors (default: 1)
            skip_lakehouse: Skip lakehouse parameter replacement (for feature branches sharing dev lakehouse)

    IMPORTANT: Initialization has side effects!
    - Reads fabdep.yml from disk
    - Makes network calls to Fabric API (for workspace ID resolution)
    - Scans workspace files to detect source IDs (stored, not written to disk)
    - Auto-detects lakehouse metadata

    Parameter file (parameter.yml) is written during deploy(), not during init.
    """

    repo_dir: Path | str
    logger_name: str | None = None
    branch_override: str | None = None  # Override git branch for workspace resolution
    new_workspace: bool = False  # Create workspace if it doesn't exist
    max_retries: int = 1  # Max retries for transient errors
    skip_lakehouse: bool = False  # Skip its parameter replacement (feature branch mode)

    # Configuration fields - auto-loaded in __post_init__
    workspace_id: str = field(init=False, repr=False)
    workspace_name: str = field(
        init=False, repr=False
    )  # Resolved workspace name (base_name [branch])
    lakehouse_name: str = field(init=False, repr=False)
    repo_item_types: set[str] = field(
        init=False, repr=False
    )  # Item types present in repo

    _repo_dir_resolved: Path = field(init=False, repr=False)
    config: FabdepConfig = field(init=False, repr=False)
    _paramfile: ParameterFile = field(init=False, repr=False)
    _detector: ParameterDetector | None = field(default=None, init=False, repr=False)
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
        4. Makes network call to Fabric REST API to resolve workspace ID
        5. Scans workspace files to detect source IDs (stored in memory, not written to disk)
        6. Scans for .Lakehouse directories and reads .platform metadata

        Parameter file writing is deferred to deploy() when the item scope is known.
        """
        self._repo_dir_resolved = Path(self.repo_dir).resolve()
        self.logger = get_logger(self.logger_name or __name__)
        self._paramfile = ParameterFile(self._repo_dir_resolved, self.logger)

        self.config = FabdepConfig.from_path(
            self._repo_dir_resolved, branch_override=self.branch_override
        )
        self.workspace_name = self.config.workspace_name
        self._apply_tenant_pin()
        self._resolve_workspace_id()
        self._detect_parameters()
        self.repo_item_types = repo_item_types(self._repo_dir_resolved)
        self._detect_lakehouse()

    def _get_git_branch(self) -> str:
        """Thin wrapper around `git.current_branch` for testing/mockability."""
        return current_branch(self._repo_dir_resolved)

    def _apply_tenant_pin(self) -> None:
        """Honor fabdep.yml's `tenant:` block before any Fabric API call."""
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
                print()
                self.logger.info("No capacity_id specified, using default capacity")
                capacity_id = get_default_capacity_id()
                self.logger.info(f"Using capacity: {capacity_id}")
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

    def _detect_parameters(self) -> None:
        """
        Detect parameters from workspace files and store for later use.

        Runs during __post_init__. Detection and storage only — no file I/O.
        The parameter file is written later in _write_parameter_file() during deploy().
        """
        detector = ParameterDetector(
            repo_dir=self._repo_dir_resolved, logger_name=self.logger.name
        )
        detector.detect_all()

        self._skipped_lakehouse_count = 0
        if self.skip_lakehouse:
            self._skipped_lakehouse_count = len(detector.filter_lakehouse_params())

        self._manual_count = apply_manual_overrides(
            detector, self.config.manual_parameters, self.logger
        )

        # MountedWarehouse bindings need a literal GUID from the target
        # workspace (no fabric-cicd dynamic exists). Manual overrides above
        # win; same-workspace deploys skip replacement entirely.
        if not detector.should_skip_replacement(self.workspace_id):
            _ = resolve_warehouse_endpoint_params(
                detector, self.workspace_id, self.logger
            )

        self._detector = detector

    def _detect_lakehouse(self) -> None:
        """Set self.lakehouse_name from the first *.Lakehouse/.platform file."""
        lakehouses = list(self._repo_dir_resolved.glob("*.Lakehouse"))
        if len(lakehouses) == 0:
            raise ValueError("No lakehouse found in repository directory")
        elif len(lakehouses) > 1:
            self.logger.warning(
                f"Multiple lakehouses found: {[d.name for d in lakehouses]}"
            )
        platform_file = lakehouses[0] / ".platform"
        try:
            metadata = loads(platform_file.read_text())
        except Exception as e:
            raise RuntimeError(f"Failed to read lakehouse metadata: {e}") from e
        self.lakehouse_name = metadata["metadata"]["displayName"]

    def deploy(
        self,
        no_cleanup: bool = False,
        item_types: list[str] | None = None,
        exclude_types: list[str] | None = None,
        include_items: list[str] | None = None,
        fail_on_stale: bool = False,
    ) -> None:
        """
        Deploy Fabric workspace items.

        Args:
            no_cleanup: Skip unpublishing orphaned items
            item_types: Specific item types to deploy
            exclude_types: Item types to exclude from deployment
            include_items: Include only specific items (format: "ItemName.ItemType")
            fail_on_stale: Raise instead of warning when a notebook has multiple
                lakehouses attached, or a warehouse attachment that can't be
                remapped to the target workspace. Intended for CI / autonomous
                agent loops where the inline warning would get buried in logs.
        """

        from . import __version__

        log_header(self.logger, f"Fabric CI/CD v{__version__}")

        if include_items:
            append_feature_flag("enable_experimental_features")
            append_feature_flag("enable_items_to_include")
            self.logger.warning(
                "Name-based item filtering is experimental. "
                "Selective deployment may cause dependency issues. Use at your own risk."
            )
            self.logger.warning(
                "Orphan cleanup disabled when using --include-items for safety."
            )
            # Fail fast on a name mismatch instead of silently deploying nothing.
            include_items = validate_include_items(
                include_items, self._repo_dir_resolved, self.logger
            )

        item_type_in_scope = resolve_item_scope(
            item_types, exclude_types, include_items, self.logger
        )

        if fail_on_stale and self._detector and self._detector.stale_attachments:
            files = ", ".join(f for f, _ in self._detector.stale_attachments)
            raise RuntimeError(
                f"--fail-on-stale: {len(self._detector.stale_attachments)} "
                f"notebook(s) have multiple lakehouses attached: {files}. "
                f"See warnings above for stale IDs."
            )

        if (
            fail_on_stale
            and self._detector
            and self._detector.unresolved_warehouse_bindings
        ):
            files = ", ".join(
                sorted({f for f, _, _ in self._detector.unresolved_warehouse_bindings})
            )
            raise RuntimeError(
                f"--fail-on-stale: "
                f"{len(self._detector.unresolved_warehouse_bindings)} warehouse "
                f"attachment(s) would deploy with source-workspace GUIDs: {files}. "
                f"See warnings above."
            )

        if self._detector is not None:
            self._paramfile.write(
                self._detector,
                self.workspace_id,
                self._manual_count,
                self._skipped_lakehouse_count,
            )

        self.logger.info(f"Workspace Name: '{self.workspace_name}'")
        self.logger.info(f"Workspace ID: {self.workspace_id}")
        for line in self._paramfile.summary:
            self.logger.info(line)

        if item_type_in_scope:
            types_to_deploy = sorted(set(item_type_in_scope) & self.repo_item_types)
            if types_to_deploy:
                self.logger.info(f"Item types in scope: {', '.join(types_to_deploy)}")
            else:
                self.logger.warning("No matching item types found in repository")
        if include_items:
            self.logger.info(f"Including items: {', '.join(include_items)}")

        param_file = self._repo_dir_resolved / "parameter.yml"
        bak_file = self._repo_dir_resolved / "parameter.yml.backup"
        if bak_file.exists() and not param_file.exists():
            mtime = datetime.fromtimestamp(bak_file.stat().st_mtime)
            self.logger.info(
                f"Parameter file (.backup) modified: {mtime.strftime('%d/%m/%Y %H:%M:%S')}"
            )
        elif param_file.exists():
            mtime = datetime.fromtimestamp(param_file.stat().st_mtime)
            self.logger.info(
                f"Parameter file modified: {mtime.strftime('%d/%m/%Y %H:%M:%S')}"
            )

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
            if include_items:
                self.logger.info(
                    f"Deployed {len(include_items)} item(s): {', '.join(include_items)}"
                )
            self.logger.info("Deployment complete!")
        finally:
            if self._paramfile.created:
                self._paramfile.cleanup()
            print()

    def _deploy_items(
        self,
        no_cleanup: bool = False,
        item_types: list[str] | None = None,
        include_items: list[str] | None = None,
        _retry_attempt: int = 0,
    ) -> None:
        """
        Deploy items to workspace using fabric-cicd with retry logic.

        Args:
            no_cleanup: Skip unpublishing orphaned items
            item_types: Specific item types to deploy
            include_items: Include only specific items (format: "ItemName.ItemType")
            _retry_attempt: Internal retry counter (do not set manually)
        """
        # fabric-cicd 1.0 requires an explicit token_credential. fabric_credential()
        # returns AzureCliCredential by default, or a StaticTokenCredential wrapping
        # FABRIC_BEARER_TOKEN for token-relay mode (used when `az login` is CA-blocked,
        # e.g. Fabrikam tenant from a locked-down host).
        target_workspace = FabricWorkspace(
            workspace_id=self.workspace_id,
            repository_directory=str(self._repo_dir_resolved),
            environment="fabdep",
            item_type_in_scope=item_types,
            token_credential=fabric_credential(),
        )

        try:
            _ = publish_all_items(target_workspace, items_to_include=include_items)
        except Exception as e:
            error_msg = str(e)
            if _retry_attempt < self.max_retries and is_transient_error(error_msg):
                self.logger.warning(f"Transient error detected: {error_msg}")
                self.logger.info(
                    f"Retrying deployment (attempt {_retry_attempt + 2}/{self.max_retries + 1})..."
                )
                return self._deploy_items(
                    no_cleanup=no_cleanup,
                    item_types=item_types,
                    include_items=include_items,
                    _retry_attempt=_retry_attempt + 1,
                )
            raise

        # include_items + unpublish is a footgun: if names don't match, nothing
        # publishes and orphan cleanup would wipe the workspace.
        if not no_cleanup and not include_items:
            unpublish_all_orphan_items(
                target_workspace, item_name_exclude_regex=self.lakehouse_name
            )
