"""Workspace deployment abstraction for Microsoft Fabric workspaces."""

# pyright: reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false

import logging
from dataclasses import dataclass, field
from datetime import datetime
from json import loads
from pathlib import Path

import yaml
from azure.identity import AzureCliCredential
from fabric_cicd import (
    FabricWorkspace,
    append_feature_flag,
    publish_all_items,
    unpublish_all_orphan_items,
)
from fabric_cicd._common._logging import log_header
from fabric_cicd.constants import ACCEPTED_ITEM_TYPES

from .api import (
    create_workspace,
    get_default_capacity_id,
    get_workspace_id_by_name,
)
from .config import FabdepConfig
from .detect import ParameterDetector
from .git import current_branch
from .log import get_logger, is_transient_error, patch_fabric_cicd_logger


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
    _auto_detect_summary: list[str] | None = field(default=None, init=False, repr=False)
    _logged_manual_warnings: set[str] = field(
        default_factory=set, init=False, repr=False
    )
    _created_param_file: bool = field(default=False, init=False, repr=False)
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

        self.config = FabdepConfig.from_path(
            self._repo_dir_resolved, branch_override=self.branch_override
        )
        self.workspace_name = self.config.workspace_name
        self._resolve_workspace_id()
        self._detect_parameters()
        self.repo_item_types = self._get_repo_item_types()
        self._detect_lakehouse()

    def _get_git_branch(self) -> str:
        """Thin wrapper around `git.current_branch` for testing/mockability."""
        return current_branch(self._repo_dir_resolved)

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

    def _load_manual_parameters(self, detector: ParameterDetector) -> int:
        """
        Load manual parameter overrides from fabdep.yml and update auto-detected parameters.

        This allows users to override the replace_value for parameters that were auto-detected
        but reference external resources (e.g., SQL endpoint IDs from other workspaces).

        The find_value is still auto-detected from source files. Users can specify:
        - replace_value: Custom replacement value or keep external reference
        - item_type (optional): Scope replacement to specific item types (e.g., ["SemanticModel"])

        Args:
            detector: ParameterDetector instance with auto-detected parameters

        Returns:
            Number of manual parameter overrides applied
        """
        manual_params = self.config.manual_parameters
        if not manual_params:
            return 0

        count = 0
        for param in manual_params:
            find_value = param.get("find_value")
            replace_value = param.get("replace_value")
            description = param.get("description")
            item_type = param.get("item_type")

            if not find_value or not replace_value:
                self.logger.warning(
                    "Skipping manual parameter with missing find_value or replace_value"
                )
                continue

            if find_value in detector.parameters:
                param_obj = detector.parameters[find_value]

                # replace_value may be a string or an env-keyed dict ({env: "value"}).
                if isinstance(replace_value, dict):
                    replace_value = next(iter(replace_value.values()))
                param_obj.dynamic_variable = replace_value

                if item_type:
                    param_obj.item_types = (
                        set(item_type) if isinstance(item_type, list) else {item_type}
                    )
                    self.logger.debug(
                        f"Manual item_type override for {find_value}: {item_type}"
                    )

                if description:
                    param_obj.description = (
                        f"{param_obj.description} (manual override: {description})"
                    )
                param_obj.source_files.add("fabdep.yml (manual override)")
                count += 1
                self.logger.debug(f"Manual override for {find_value}: {replace_value}")

            elif find_value not in self._logged_manual_warnings:
                self.logger.warning(
                    f"Manual parameter '{find_value}' was not auto-detected. "
                    "Make sure the find_value matches exactly what's in the source files."
                )
                self._logged_manual_warnings.add(find_value)

        return count

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

        self._manual_count = self._load_manual_parameters(detector)
        self._detector = detector

    def _write_parameter_file(self, item_types: list[str] | None = None) -> None:
        """
        Write parameter.yml for deployment.

        Called once from deploy(). All detected parameters are emitted; only
        manual_parameters overrides carry per-type scoping (via the item_type
        field on the entry). This keeps the find_replace surface simple — if a
        parameter isn't used by any in-scope file, fabric-cicd will warn but
        the deployment still succeeds.

        Args:
            item_types: Currently unused; kept for signature stability with
                callers that may pass it for future filtering decisions.
        """
        del item_types  # No auto-detected scoping — see docstring.
        detector = self._detector
        if detector is None:
            return

        if detector.should_skip_replacement(self.workspace_id):
            self._auto_detect_summary = [
                "Source and target workspace are the same -- deploying directly without replacements",
            ]
            return

        dyns = sum(1 for p in detector.parameters.values() if p.dynamic_variable)
        total = len(detector.parameters)

        if dyns == 0:
            if self._skipped_lakehouse_count > 0:
                self._auto_detect_summary = [
                    f"Skipped {self._skipped_lakehouse_count} lakehouse parameter{'s' if self._skipped_lakehouse_count != 1 else ''} (--skip-lakehouse)",
                    "Deploying without parameter.yml file",
                ]
            else:
                self._auto_detect_summary = [
                    "No parameters detected for replacement",
                    "Deploying without parameter.yml file",
                ]
            return

        param_file = self._repo_dir_resolved / "parameter.yml"
        backup_file = self._repo_dir_resolved / "parameter.yml.backup"
        summary = []

        if param_file.exists():
            _ = param_file.rename(backup_file)
            summary.append(f"Backed up existing parameter.yml to {backup_file.name}")

        detector.save_parameter_yml(
            output_path=param_file,
            use_dynamic=True,
            environment="fabdep",
            skip_empty=True,
        )
        self._created_param_file = True
        self._add_domain_substitutions(param_file, detector)

        external_count = max(total - dyns, 0)
        external_part = (
            f" + {external_count} external connection{'s' if external_count != 1 else ''}"
            if external_count
            else ""
        )
        manual_part = (
            f" + {self._manual_count} manual parameter{'s' if self._manual_count != 1 else ''}"
            if self._manual_count
            else ""
        )
        summary.append(
            f"Found {dyns} dynamic parameter{'s' if dyns != 1 else ''}{external_part}{manual_part}"
        )
        if self._skipped_lakehouse_count > 0:
            summary.append(
                f"Skipped {self._skipped_lakehouse_count} lakehouse parameter{'s' if self._skipped_lakehouse_count != 1 else ''} (--skip-lakehouse)"
            )

        unresolved_sem_model = [
            p for p in detector.parameters.values()
            if "SemanticModel" in p.item_types and not p.dynamic_variable and p.description != "External connection ID"
        ]
        if unresolved_sem_model:
            self.logger.warning(
                f"{len(unresolved_sem_model)} SemanticModel parameter(s) could not be auto-resolved and will be skipped:"
            )
            for p in unresolved_sem_model:
                self.logger.warning(f"  {p.description}: {p.find_value[:60]}...")
            self.logger.warning(
                "Add a manual_parameters entry in fabdep.yml to supply the replacement value."
            )

        self._auto_detect_summary = summary

    def _add_domain_substitutions(
        self, param_file: Path, detector: ParameterDetector
    ) -> None:
        """Append a second-pass domain rewrite for non-default SemanticModel connections.

        fabric-cicd 1.0's `$items.Lakehouse.<name>.$sqlendpoint` always returns
        `.datawarehouse.fabric.microsoft.com` form, but SemanticModels can
        reference `.database.fabric.microsoft.com` (SQL Database endpoints) or
        `.pbidedicated.windows.net` (older workspaces). fabric-cicd has no
        callback, no multi-pass templating, and no equivalent dynamic for
        those forms — so we let fabric-cicd do the first pass and append a
        second find_replace that rewrites the resulting FQDN. Targeting
        includes the database name and is scoped to SemanticModel.

        TODO: revisit if fabric-cicd grows native multi-domain support.
        """
        connections_needing_substitution = []
        for param in detector.parameters.values():
            domain_suffix = param.metadata.get("domain_suffix", "").lower()
            domain_type = param.metadata.get("domain_type", "").lower()
            if not param.metadata.get("db_name"):
                continue
            if domain_type == "database":
                connections_needing_substitution.append(("database_type", param))
            elif domain_suffix == "pbidedicated.windows.net":
                connections_needing_substitution.append(("pbidedicated_suffix", param))

        if not connections_needing_substitution:
            return

        with open(param_file) as f:
            param_data = yaml.safe_load(f)

        if not param_data or "find_replace" not in param_data:
            return

        for substitution_type, param in connections_needing_substitution:
            db_name = param.metadata.get("db_name")
            if not db_name:
                continue

            if substitution_type == "database_type":
                targeted_find = f'.datawarehouse.fabric.microsoft.com","{db_name}'
                targeted_replace = f'.database.fabric.microsoft.com","{db_name}'
            elif substitution_type == "pbidedicated_suffix":
                domain_type = param.metadata.get("domain_type", "datawarehouse")
                targeted_find = f'.{domain_type}.fabric.microsoft.com","{db_name}'
                targeted_replace = f'.{domain_type}.pbidedicated.windows.net","{db_name}'
            else:
                continue

            param_data["find_replace"].append({
                "find_value": targeted_find,
                "replace_value": {"fabdep": targeted_replace},
                "item_type": ["SemanticModel"],
            })

        with open(param_file, "w") as f:
            yaml.dump(param_data, f, default_flow_style=False, sort_keys=False)

    def _cleanup_dynamic_params(self) -> None:
        """Remove the auto-generated parameter.yml and restore the user's backup."""
        param_file = self._repo_dir_resolved / "parameter.yml"
        backup_file = self._repo_dir_resolved / "parameter.yml.backup"

        if param_file.exists():
            param_file.unlink()
        if backup_file.exists():
            _ = backup_file.rename(param_file)
            self.logger.info("Restored original parameter.yml")

    def _get_repo_item_types(self) -> set[str]:
        """Return the set of accepted Fabric item types found in the repo."""
        item_types = set()
        for platform_file in self._repo_dir_resolved.rglob(".platform"):
            # Items are stored as `<DisplayName>.<ItemType>/`.
            if "." in platform_file.parent.name:
                parts = platform_file.parent.name.rsplit(".", 1)
                if len(parts) == 2 and parts[1] in ACCEPTED_ITEM_TYPES:
                    item_types.add(parts[1])
        return item_types

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

    def _resolve_item_scope(
        self,
        item_types: list[str] | None,
        exclude_types: list[str] | None,
        include_items: list[str] | None,
    ) -> list[str] | None:
        """Normalize, validate, and combine item-type filters into a single scope.

        Returns the list of item types that fabric-cicd should deploy, or None
        if no scope filtering is requested. Logs warnings for unrecognized types.

        Combines (in priority order):
        1. Explicit `item_types` (after canonical-case normalization).
        2. `exclude_types` → all ACCEPTED_ITEM_TYPES minus excludes.
        3. `include_items` (e.g. "Foo.Notebook") → derive scope from suffixes
           if no other scope was provided.
        """
        canonical = {t.lower(): t for t in ACCEPTED_ITEM_TYPES}

        def _normalize(types: list[str] | None) -> list[str] | None:
            if not types:
                return types
            return [canonical.get(t.lower(), t) for t in types]

        def _warn_unknown(types: list[str] | None, label: str) -> None:
            if not types:
                return
            invalid = [t for t in types if t not in ACCEPTED_ITEM_TYPES]
            if invalid:
                self.logger.warning(
                    f"Unrecognized item types in {label}: {', '.join(invalid)}"
                )
                self.logger.warning(
                    f"Supported types: {', '.join(ACCEPTED_ITEM_TYPES)}"
                )

        item_types = _normalize(item_types)
        exclude_types = _normalize(exclude_types)
        _warn_unknown(item_types, "filter")
        _warn_unknown(exclude_types, "exclusion filter")

        if item_types:
            return item_types
        if exclude_types:
            return [t for t in ACCEPTED_ITEM_TYPES if t not in exclude_types]

        # Derive scope from include_items (Name.Type format)
        if include_items:
            derived = {
                item.rsplit(".", 1)[1]
                for item in include_items
                if "." in item and item.rsplit(".", 1)[1] in ACCEPTED_ITEM_TYPES
            }
            if derived:
                return sorted(derived)

        return None

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

        item_type_in_scope = self._resolve_item_scope(
            item_types=item_types,
            exclude_types=exclude_types,
            include_items=include_items,
        )
        self._write_parameter_file(item_types=item_type_in_scope)

        self.logger.info(f"Workspace Name: '{self.workspace_name}'")
        self.logger.info(f"Workspace ID: {self.workspace_id}")
        if self._auto_detect_summary:
            for line in self._auto_detect_summary:
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
        bak_file = param_file.with_suffix(".yml.bak")
        if bak_file.exists() and not param_file.exists():
            mtime = datetime.fromtimestamp(bak_file.stat().st_mtime)
            self.logger.info(
                f"Parameter file (.bak) modified: {mtime.strftime('%d/%m/%Y %H:%M:%S')}"
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
            self.logger.info("Deployment complete!")
        finally:
            if self._created_param_file:
                self._cleanup_dynamic_params()
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
        # fabric-cicd 1.0 requires an explicit token_credential (no more
        # implicit DefaultAzureCredential). AzureCliCredential keeps fabdep's
        # auth surface a single command — the active `az` session is what
        # gets used.
        target_workspace = FabricWorkspace(
            workspace_id=self.workspace_id,
            repository_directory=str(self._repo_dir_resolved),
            environment="fabdep",
            item_type_in_scope=item_types,
            token_credential=AzureCliCredential(),
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
