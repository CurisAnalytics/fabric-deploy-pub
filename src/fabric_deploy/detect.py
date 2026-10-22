"""Auto-detection of source IDs from workspace repository for parameter generation."""

# pyright: reportMissingTypeArgument=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownParameterType=false
# pyright: reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .log import get_logger

regex = re.Pattern[str]


def _iter_meta_blocks(content: str):
    """Yield each contiguous run of `# META` / `-- META` lines as one string.

    Fabric notebooks repeat `# META { ... }` (Python) or `-- META { ... }`
    (T-SQL / Spark SQL) blocks: once for top-level dependencies, then once
    per cell for language hints. Each contiguous run forms a self-contained
    JSON object once the prefix is stripped.
    """
    current: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# META"):
            current.append(stripped.removeprefix("# META").lstrip())
        elif stripped.startswith("-- META"):
            current.append(stripped.removeprefix("-- META").lstrip())
        elif current:
            yield "\n".join(current)
            current = []
    if current:
        yield "\n".join(current)


def find_in_item_files(repo_dir: Path, values: Iterable[str]) -> dict[str, set[str]]:
    """Repo-relative item files that contain each literal in `values`.

    Item files are those under a directory carrying a `.platform` — the set
    fabric-cicd's find_replace operates on. Repo-root files such as
    fabdep.yml are deliberately excluded so a rule can't match itself. One
    walk covers every value.
    """
    matches: dict[str, set[str]] = {v: set() for v in values}
    if not matches:
        return matches
    item_dirs = {p.parent for p in repo_dir.glob("**/.platform")}
    for item_dir in item_dirs:
        for path in item_dir.rglob("*"):
            if not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            rel = str(path.relative_to(repo_dir))
            for value, found_in in matches.items():
                if value in content:
                    found_in.add(rel)
    return matches


@dataclass
class DetectedParameter:
    """Represents a detected parameter with its context.

    `item_types` is set ONLY by manual_parameters overrides in fabdep.yml.
    Auto-detected parameters leave it empty so fabric-cicd's find_replace
    applies to every file in scope (no per-type filtering).
    """

    find_value: str
    item_types: set[str] = field(default_factory=set)
    source_files: set[str] = field(default_factory=set)
    description: str = ""
    dynamic_variable: str = ""
    metadata: dict = field(default_factory=dict)  # e.g. domain_type, db_name

    def to_dict(self, environment: str = "prod") -> dict:
        """
        Convert to parameter entry format for auto-detect mode.

        Args:
            environment: Environment name (default: "prod")

        Returns:
            Dictionary in parameter entry format with dynamic variables
        """
        entry: dict = {"find_value": self.find_value}
        if self.item_types:
            entry["item_type"] = sorted(self.item_types)

        # fabric-cicd requires replace_value to be an env-keyed dict, even
        # though we only emit one environment ("fabdep") in practice.
        entry["replace_value"] = {environment: self.dynamic_variable}  # pyright: ignore[reportArgumentType]

        return entry


class ParameterDetector:
    """Detects source IDs from workspace repository files."""

    # Stripped during feature-branch deploys (`-L` / skip_lakehouse).
    LAKEHOUSE_DESCRIPTIONS: set[str] = {
        "Lakehouse ID",
        "SQL Analytics Endpoint Connection String",
        "Lakehouse SQL Analytics Endpoint Item ID",
    }

    # Maps `known_warehouses[].type` from notebook metadata to a parameter
    # description (which drives the dynamic-variable mapping). The GUID in
    # all three cases is a SQLEndpoint *item id* — for Lakewarehouse the
    # lakehouse's SQL analytics endpoint, for MountedWarehouse the SQL
    # database's endpoint. Datawarehouse attaches by the Warehouse item id.
    WAREHOUSE_ATTACHMENT_DESCRIPTIONS: dict[str, str] = {
        "lakewarehouse": "Lakehouse SQL Analytics Endpoint Item ID",
        "mountedwarehouse": "SQL Database SQL Endpoint Item ID",
        "datawarehouse": "Warehouse ID",
    }

    UUID_PATTERN: regex = re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
    )

    # Fabric's "unattached / placeholder" sentinel. Appears as workspaceId in
    # TridentNotebook pipeline activities (fabric-cicd's _replace_workspace_ids
    # handles those itself) and as default_lakehouse_workspace_id in notebooks
    # that were never attached. Never emit a find_replace rule for it.
    NULL_UUID = "00000000-0000-0000-0000-000000000000"

    # Sql.Database("HOST-SQLSTRING.{datawarehouse|database}.{fabric.microsoft.com|pbidedicated.windows.net}", "DB-NAME" or "DB-NAME-UUID")
    # Groups: (1) host prefix, (2) sql endpoint string, (3) domain type, (4) domain suffix, (5) db name, (6) optional UUID suffix
    SQL_DATABASE_PATTERN: regex = re.compile(
        r'Sql\.Database\(\s*"([^"]+)-([A-Z0-9]+)\.(datawarehouse|database)\.(fabric\.microsoft\.com|pbidedicated\.windows\.net)"\s*,\s*"([^"]+?)(?:-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}))?"\s*\)',
        re.IGNORECASE,
    )

    # Sql.Databases("HOST-SQLSTRING.{datawarehouse|database}.{...}") — the
    # server-level navigation form (Power Query picks the database by name in
    # a later step). One argument, so there is no db-name/item-id to capture;
    # only the hostname needs remapping.
    SQL_DATABASES_PATTERN: regex = re.compile(
        r'Sql\.Databases\(\s*"([^"]+)-([A-Z0-9]+)\.(datawarehouse|database)\.(fabric\.microsoft\.com|pbidedicated\.windows\.net)"',
        re.IGNORECASE,
    )

    WORKSPACE_ID_PATTERN: regex = re.compile(
        r'"default_lakehouse_workspace_id":\s*"([0-9a-f-]+)"', re.IGNORECASE
    )
    LAKEHOUSE_ID_PATTERN: regex = re.compile(
        r'"default_lakehouse":\s*"([0-9a-f-]+)"', re.IGNORECASE
    )

    # A PBIR action button invokes a User Data Function through a `visualLink`
    # container object whose properties look like:
    #   type.expr.Literal.Value            == "'DataFunction'"
    #   dataFunction.byReference.itemId    -> "'<udf item id>'"
    #   dataFunction.byReference.workspaceId -> "'<workspace id>'"
    #   dataFunction.metadata.dataFunction.name -> the function inside the item
    # Both GUIDs are the SOURCE workspace's deployed ids (not logicalIds), so a
    # dev->prod deploy ships buttons that still call the dev function unless
    # they are parameterized. Unbound buttons carry "''" and are left alone.
    PBIR_DATA_FUNCTION_TYPE = "'DataFunction'"

    # DirectLake semantic models reference the lakehouse via OneLake DFS URL:
    #   https://onelake.dfs.fabric.microsoft.com/<workspace_id>/<lakehouse_id>
    ONELAKE_DFS_PATTERN: regex = re.compile(
        r"onelake\.dfs\.fabric\.microsoft\.com/"
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/"
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        re.IGNORECASE,
    )

    def __init__(
        self,
        repo_dir: Path | str,
        logger_name: str | None = None,
        source_workspace_ids: Iterable[str] | None = None,
        source_lakehouse_ids: dict[str, str] | None = None,
    ):
        """
        Initialize parameter detector.

        Args:
            repo_dir: Path to workspace repository directory
            logger_name: Optional logger name (defaults to module name)
            source_workspace_ids: Workspaces this repo's content may have been
                exported from — the deployer passes the target plus every
                sibling workspace (`{base} [branch]`). They seed the anchor
                set pipeline/copyjob workspaceIds are classified against.
                Needed once notebooks use Fabric's lakehouse auto-binding in
                git: their metadata then carries the null UUID instead of a
                workspace id, so content alone no longer says which workspace
                the repo came from.
            source_lakehouse_ids: Item id → display name of those workspaces'
                lakehouses. Each becomes a `$items.Lakehouse` rule where an
                item file still carries the physical id (an abfss literal in
                notebook code, say) — the coverage legacy notebook metadata
                gave for free.
        """
        self.repo_dir = Path(repo_dir).resolve()  # pyright: ignore[reportUnannotatedClassAttribute]
        self.parameters: dict[str, DetectedParameter] = {}
        self._seed_workspace_ids: set[str] = set(source_workspace_ids or ())
        self._seed_lakehouse_ids: dict[str, str] = dict(source_lakehouse_ids or {})
        # logicalId → displayName for the repo's Lakehouse items. Auto-bound
        # notebook metadata names its lakehouse by logicalId, not item id.
        self._lakehouse_logical_ids: dict[str, str] = {}
        # Repo-relative notebook dirs whose notebook-settings.json turns
        # Fabric's lakehouse auto-binding on.
        self.auto_bound_notebooks: list[str] = []
        self._lakehouse_names: list[str] = []
        self._sqldatabase_names: list[str] = []
        self._warehouse_names: list[str] = []
        self._mirroreddatabase_names: list[str] = []
        # (source_file, [stale_ids]) for each notebook with >1 known_lakehouse.
        # Populated alongside the inline warning so the caller (e.g. deploy
        # with --fail-on-stale) can promote it to a fatal error.
        self.stale_attachments: list[tuple[str, list[str]]] = []
        # (source_file, guid, type) for each notebook warehouse attachment
        # that has no replacement mapping — the source-workspace GUID would
        # deploy verbatim. Same promote-to-fatal contract as stale_attachments.
        self.unresolved_warehouse_bindings: list[tuple[str, str, str]] = []
        # Workspace ids this repo's own items were exported from (legacy
        # notebook attachments, semantic-model OneLake URLs, bound UDF buttons,
        # or the repo's sibling workspaces the deployer listed). Pipeline/
        # CopyJob workspaceIds are only remapped to the target when they match
        # one of these — anything else is a deliberate cross-workspace
        # reference that must deploy verbatim (one workspace's pipeline
        # invoking another workspace's notebook had the foreign workspace id
        # rewritten to the target).
        self._source_workspace_ids: set[str] = set()
        # (source_file, guid) for each pipeline/copyjob workspaceId left
        # verbatim as a cross-workspace reference.
        self.offtarget_workspace_refs: list[tuple[str, str]] = []
        # (report_item, function_name, udf_display_name, item_id) for each PBIR
        # button whose UDF reference was auto-rebound to the target workspace.
        # Surfaced by the deployer so an auto-replace is never silent.
        self.udf_bindings: list[tuple[str, str, str, str]] = []
        # (report_item, function_name, item_id, reason) for each PBIR button
        # whose UDF reference could NOT be rebound. Warned once per distinct
        # reason at the end of detection rather than once per visual file.
        self.udf_skipped: list[tuple[str, str, str, str]] = []
        # Function name -> {UDF display names declaring it}, from each
        # .UserDataFunction item's definition.json.
        self._udf_functions: dict[str, set[str]] = {}
        self._userdatafunction_names: list[str] = []
        self.logger: logging.Logger = get_logger(logger_name or __name__)

    @property
    def sqldatabase_names(self) -> list[str]:
        """Display names of .SQLDatabase items found in the repo."""
        return list(self._sqldatabase_names)

    def _log_file_warning(self, file_path: Path, error: Exception, context: str = "") -> None:
        """
        Log warning message for file processing errors.

        Args:
            file_path: Path to the file that failed
            error: Exception that was raised
            context: Optional context description (e.g., "lakehouse metadata")
        """
        context_str = f" {context} from" if context else ""
        self.logger.warning(f"Failed to read{context_str} {file_path}: {error}")

    def detect_all(self):
        "Detect all parameters from workspace files."
        self.parameters = {}
        self.stale_attachments = []
        self.unresolved_warehouse_bindings = []
        self.udf_bindings = []
        self.udf_skipped = []
        self._source_workspace_ids = set()
        self.offtarget_workspace_refs = []
        self.auto_bound_notebooks = []

        # Item names must be resolved before _generate_dynamic_variable runs.
        self._lakehouse_names = self._detect_item_names("Lakehouse")
        self._lakehouse_logical_ids = self._lakehouse_logical_id_index()
        self._sqldatabase_names = self._detect_item_names("SQLDatabase")
        self._warehouse_names = self._detect_item_names("Warehouse")
        self._mirroreddatabase_names = self._detect_item_names("MirroredDatabase")
        self._index_user_data_functions()
        for workspace_id in sorted(self._seed_workspace_ids):
            self._anchor_workspace(workspace_id)

        # Scans that can anchor the source workspace (notebooks, models,
        # reports) run before the pipeline/copyjob scans that classify
        # workspaceIds against the anchors, so the cross-workspace warning
        # only fires for ids nothing in the repo claims. Correctness does not
        # depend on this order — _anchor_workspace repairs a parameter that
        # was classified first — only the log does.
        # Notebook content is `.py` (Python/PySpark), `.sql` (T-SQL), etc.
        # Metadata block is identical JSON-in-comments — the prefix differs
        # (`# META` vs `-- META`) but the find_replace patterns don't anchor on it.
        # Discovered via .platform type, not directory suffix: Synapse-migrated
        # notebooks live in `*.SynapseNotebook/` dirs but are type Notebook.
        notebook_files = [
            content_file
            for item_dir, _ in self._platform_items("Notebook")
            for ext in ("py", "sql")
            if (content_file := item_dir / f"notebook-content.{ext}").exists()
        ]
        self._process_files(notebook_files, self._extract_from_notebook)
        self._process_files(
            self.repo_dir.glob("**/*.SemanticModel/definition/**/*.tmdl"),
            self._extract_from_semantic_model,
        )
        # Only worth walking report definitions when the repo actually ships a
        # User Data Function to rebind the buttons to.
        if self._userdatafunction_names:
            self._process_files(
                self.repo_dir.glob("**/*.Report/definition/**/*.json"),
                self._extract_from_report,
                parse_json=True,
            )
        self._process_files(
            self.repo_dir.glob("**/*.DataPipeline/pipeline-content.json"),
            self._extract_from_pipeline,
            parse_json=True,
        )
        self._process_files(
            self.repo_dir.glob("**/*.CopyJob/copyjob-content.json"),
            self._extract_from_copyjob,
            parse_json=True,
        )
        self._register_source_ids()
        self._warn_skipped_udf_buttons()

    def _warn_skipped_udf_buttons(self) -> None:
        """One warning per distinct skip reason, not one per visual file.

        The same stale function name typically appears on several pages of the
        same report; a per-file warning is noise that buries the actionable
        part (which function, and why it didn't resolve).
        """
        counts: dict[str, int] = {}
        reports: dict[str, set[str]] = {}
        for report_name, _, _, reason in self.udf_skipped:
            counts[reason] = counts.get(reason, 0) + 1
            reports.setdefault(reason, set()).add(report_name)
        for reason, count in counts.items():
            names = ", ".join(f"'{r}'" for r in sorted(reports[reason]))
            self.logger.warning(
                f"{count} User Data Function button(s) in {names} not rebound — "
                f"{reason}. They will deploy still pointing at the source "
                f"workspace's function."
            )

    def _lakehouse_logical_id_index(self) -> dict[str, str]:
        """logicalId → displayName for the repo's Lakehouse items."""
        index: dict[str, str] = {}
        for item_dir, meta in self._platform_items("Lakehouse"):
            try:
                config = json.loads((item_dir / ".platform").read_text(encoding="utf-8")).get(
                    "config", {}
                )
            except Exception:
                continue
            logical_id = config.get("logicalId") if isinstance(config, dict) else None
            name = meta.get("displayName")
            if (
                isinstance(logical_id, str)
                and name
                and self.UUID_PATTERN.fullmatch(logical_id)
                and logical_id != self.NULL_UUID
            ):
                index[logical_id] = name
        return index

    def _auto_binding_enabled(self, item_dir: Path) -> bool:
        """Whether a notebook's `notebook-settings.json` turns lakehouse
        auto-binding on. Fabric writes the file when the setting is enabled
        in the notebook's git settings and owns its content."""
        settings = item_dir / "notebook-settings.json"
        if not settings.exists():
            return False
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
        except Exception as e:
            self._log_file_warning(settings, e, "notebook settings")
            return False
        binding = data.get("auto-binding") if isinstance(data, dict) else None
        return isinstance(binding, dict) and binding.get("lakehouse") == "on"

    def _platform_items(self, item_type: str) -> list[tuple[Path, dict]]:
        """(item_dir, .platform metadata) pairs for repo items of `item_type`.

        Matches on the .platform `metadata.type` — which is how fabric-cicd
        discovers items — NOT the directory suffix. The two can differ:
        Synapse-migrated notebooks sit in `*.SynapseNotebook/` dirs whose
        .platform says `type: Notebook`.

        Recursive: items can live in workspace subfolders (e.g.
        `ERP/northwind_sql_db.SQLDatabase`). Shallower paths sort first so a
        top-level item stays the primary for first-match resolution.
        """
        items: list[tuple[Path, dict]] = []
        platform_files = sorted(
            self.repo_dir.glob("**/.platform"),
            key=lambda p: (len(p.relative_to(self.repo_dir).parts), str(p)),
        )
        for platform_file in platform_files:
            try:
                metadata = json.loads(platform_file.read_text(encoding="utf-8")).get("metadata", {})
            except Exception as e:
                self._log_file_warning(platform_file, e, "item metadata")
                continue
            if metadata.get("type") == item_type:
                items.append((platform_file.parent, metadata))
        return items

    def _detect_item_names(self, item_type: str) -> list[str]:
        """
        Detect item names from .platform files of the given item type.

        Args:
            item_type: Fabric item type (e.g., "Lakehouse", "SQLDatabase")

        Returns:
            List of display names found for items of this type
        """
        return [
            meta["displayName"]
            for _, meta in self._platform_items(item_type)
            if meta.get("displayName")
        ]

    def _process_files(self, files, extract_func, parse_json: bool = False) -> None:
        """
        Generic file processor that handles iteration, reading, and error handling.

        Args:
            files: Iterable of file paths to process
            extract_func: Callback function that receives (file_path, content_or_data)
            parse_json: If True, parse content as JSON before passing to extract_func
        """
        for file_path in files:
            try:
                content = file_path.read_text(encoding="utf-8")
                data = json.loads(content) if parse_json else content
                extract_func(file_path, data)
            except Exception as e:
                self._log_file_warning(file_path, e)

    def _add_parameter(
        self, find_value: str, source_file: str, description: str = ""
    ) -> DetectedParameter:
        """Add or update a detected parameter with dynamic variable generation."""
        if find_value not in self.parameters:
            self.parameters[find_value] = DetectedParameter(
                find_value=find_value,
                description=description,
                dynamic_variable=self._generate_dynamic_variable(description),
            )

        param = self.parameters[find_value]
        param.source_files.add(source_file)
        if description and not param.description:
            param.description = description

        return param

    def _add_uuid_parameter(self, value: str, source_file: str, description: str = ""):
        """Add parameter if value is a valid UUID."""
        if value and self.UUID_PATTERN.match(value):
            _ = self._add_parameter(
                find_value=value,
                source_file=source_file,
                description=description,
            )

    def _generate_dynamic_variable(self, description: str) -> str:
        """
        Generate smart replace dynamic variable based on parameter description.

        Args:
            description: Parameter description (e.g., "Workspace ID", "Lakehouse ID")

        Returns:
            Dynamic variable string (e.g., "$workspace.$id")
        """
        desc_lower = description.lower()

        if "workspace id" in desc_lower:
            return "$workspace.$id"

        # Multi-item repos are rare; first match is the common case.
        if self._lakehouse_names:
            lakehouse_name = self._lakehouse_names[0]
            if "lakehouse id" in desc_lower:
                return f"$items.Lakehouse.{lakehouse_name}.$id"
            if "sql analytics endpoint connection string" in desc_lower:
                return f"$items.Lakehouse.{lakehouse_name}.$sqlendpoint"
            # Distinct from "Lakehouse ID" — this is the SQL endpoint's item ID.
            if "lakehouse sql analytics endpoint item id" in desc_lower:
                return f"$items.Lakehouse.{lakehouse_name}.$sqlendpointid"

        # A MirroredDatabase's SQL analytics endpoint lives on the same
        # `datawarehouse.*` domain as a Lakehouse's, so it is detected under
        # the same descriptions. It only claims them when the repo has no
        # Lakehouse — with both present, first-match Lakehouse wins (above).
        # Requires fabric-cicd >= 1.3 (MirroredDatabase $sqlendpoint support).
        if self._mirroreddatabase_names and not self._lakehouse_names:
            mirrored_name = self._mirroreddatabase_names[0]
            if "sql analytics endpoint connection string" in desc_lower:
                return f"$items.MirroredDatabase.{mirrored_name}.$sqlendpoint"
            if "lakehouse sql analytics endpoint item id" in desc_lower:
                return f"$items.MirroredDatabase.{mirrored_name}.$sqlendpointid"

        if self._sqldatabase_names:
            sqldatabase_name = self._sqldatabase_names[0]
            # Resolves to body/properties/serverFqdn — which carries a ",1433"
            # port suffix, so the deployer replaces this dynamic with a
            # literal bare host at deploy time (see
            # params.resolve_sqldatabase_hostname_params); this is the
            # fallback if that lookup fails. Caveat: on a fresh `-N` deploy
            # the SQLDatabase may not be provisioned yet — manual_parameters
            # overrides take precedence.
            if "sql database connection string" in desc_lower:
                return f"$items.SQLDatabase.{sqldatabase_name}.$sqlendpoint"
            if "sql database item id" in desc_lower:
                return f"$items.SQLDatabase.{sqldatabase_name}.$id"
            # "<display_name>-<uuid>" form — no $name attribute in fabric-cicd,
            # so this requires a manual_parameters entry.
            if "sql database name" in desc_lower:
                return ""
            # fabric-cicd has no $sqlendpointid attribute for SQLDatabase
            # (only Lakehouse), so this can't be a dynamic variable — the
            # deployer resolves it to a literal GUID from the target
            # workspace (see params.resolve_warehouse_endpoint_params).
            if "sql database sql endpoint item id" in desc_lower:
                return ""

        if self._warehouse_names:
            warehouse_name = self._warehouse_names[0]
            if "warehouse id" in desc_lower:
                return f"$items.Warehouse.{warehouse_name}.$id"

        # External connection IDs are tenant-wide; not resolvable from repo.
        if "connection id" in desc_lower:
            return ""

        return ""

    def _extract_from_notebook(self, file_path: Path, content: str) -> None:
        """Extract workspace and lakehouse IDs from notebook metadata."""
        source_file = str(file_path.relative_to(self.repo_dir))

        # Unattached notebooks carry the null-UUID sentinel. Emitting a rule
        # for it would globally rewrite every placeholder fabric-cicd manages
        # itself (e.g. TridentNotebook workspaceIds in pipeline JSON).
        for workspace_id in self.WORKSPACE_ID_PATTERN.findall(content):
            if workspace_id == self.NULL_UUID:
                continue
            # A legacy attachment names the workspace the repo was exported
            # from — an anchor for pipeline/copyjob workspaceIds.
            self._anchor_workspace(workspace_id, source_file)

        for lakehouse_id in self.LAKEHOUSE_ID_PATTERN.findall(content):
            if lakehouse_id == self.NULL_UUID:
                continue
            param = self._add_parameter(lakehouse_id, source_file, "Lakehouse ID")
            # Auto-bound metadata carries the lakehouse's logicalId. fabric-cicd
            # resolves logicalIds itself before parameters apply; this rule is
            # the backstop, and it names the owning lakehouse rather than the
            # repo's first one.
            owner = self._lakehouse_logical_ids.get(lakehouse_id)
            if owner:
                param.dynamic_variable = f"$items.Lakehouse.{owner}.$id"

        if self._auto_binding_enabled(file_path.parent):
            self.auto_bound_notebooks.append(str(file_path.parent.relative_to(self.repo_dir)))

        self._extract_warehouse_attachments(source_file, content)
        self._warn_on_stale_lakehouse_attachments(source_file, content)

    def _extract_warehouse_attachments(self, source_file: str, content: str) -> None:
        """Detect `dependencies.warehouse` bindings from notebook metadata.

        T-SQL notebooks attach to a lakehouse SQL analytics endpoint
        (`type: Lakewarehouse`) or a SQL database's endpoint
        (`type: MountedWarehouse`) via `default_warehouse`/`known_warehouses`.
        These GUIDs are environment-specific just like `default_lakehouse` —
        left unreplaced, the deployed notebook stays wired to the *source*
        workspace's endpoint (visible as a cross-workspace link in lineage).
        """
        for block in _iter_meta_blocks(content):
            try:
                data = json.loads(block)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            warehouse_meta = data.get("dependencies", {}).get("warehouse", {})
            if not isinstance(warehouse_meta, dict):
                continue

            known = warehouse_meta.get("known_warehouses") or []
            types_by_id: dict[str, str] = {
                k["id"]: (k.get("type") or "") for k in known if isinstance(k, dict) and k.get("id")
            }
            default_wh = warehouse_meta.get("default_warehouse")
            if default_wh:
                types_by_id.setdefault(default_wh, "")

            for wh_id, wh_type in types_by_id.items():
                if not self.UUID_PATTERN.fullmatch(wh_id):
                    continue
                description = self.WAREHOUSE_ATTACHMENT_DESCRIPTIONS.get(
                    wh_type.lower(), "Notebook warehouse attachment"
                )
                param = self._add_parameter(wh_id, source_file, description)

                # MountedWarehouse is resolved to a literal at deploy time —
                # but only if the repo has a SQLDatabase item to resolve from.
                resolvable_later = description == "SQL Database SQL Endpoint Item ID" and bool(
                    self._sqldatabase_names
                )
                if not param.dynamic_variable and not resolvable_later:
                    self.unresolved_warehouse_bindings.append((source_file, wh_id, wh_type))
                    self.logger.warning(
                        f"{source_file}: warehouse attachment {wh_id} "
                        f"(type={wh_type or 'unknown'}) has no replacement mapping — "
                        f"the source-workspace GUID will deploy verbatim and the "
                        f"target notebook will stay bound to the source endpoint. "
                        f"Add the matching item to the repo or a manual_parameters "
                        f"entry in fabdep.yml."
                    )

    def _warn_on_stale_lakehouse_attachments(self, source_file: str, content: str) -> None:
        """Warn when a notebook has multiple lakehouses attached.

        Fabric retains every lakehouse a notebook has ever been attached to
        in `known_lakehouses`, so switching defaults (e.g. dev → prod) leaves
        the prior attachment behind. Deploying that file rewrites IDs by
        regex but doesn't drop the extra entry — the target workspace ends up
        with a cross-workspace lakehouse reference.
        """
        for block in _iter_meta_blocks(content):
            try:
                data = json.loads(block)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            lakehouse_meta = data.get("dependencies", {}).get("lakehouse", {})
            known = lakehouse_meta.get("known_lakehouses", [])
            if len(known) <= 1:
                continue
            default_lh = lakehouse_meta.get("default_lakehouse")
            stale = [k.get("id") for k in known if k.get("id") != default_lh]
            self.stale_attachments.append((source_file, stale))
            self.logger.warning(
                f"{source_file}: {len(known)} lakehouses attached "
                f"(default={default_lh}, stale={stale}). "
                f"Detach extras in the Fabric UI and re-export before deploying."
            )

    def _extract_from_semantic_model(self, file_path: Path, content: str) -> None:
        """Extract SQL connection strings and endpoint IDs from semantic models."""
        source_file = str(file_path.relative_to(self.repo_dir))

        for workspace_id, lakehouse_id in self.ONELAKE_DFS_PATTERN.findall(content):
            # A Direct Lake model's OneLake URL points at its own workspace —
            # a source-workspace anchor.
            self._anchor_workspace(workspace_id, source_file)
            self._add_uuid_parameter(lakehouse_id, source_file, "Lakehouse ID")

        for (
            host_prefix,
            sql_string,
            domain_type,
            domain_suffix,
        ) in self.SQL_DATABASES_PATTERN.findall(content):
            hostname_param = self._add_parameter(
                f"{host_prefix}-{sql_string}.{domain_type}.{domain_suffix}",
                source_file,
                "SQL Database Connection String"
                if domain_type.lower() == "database"
                else "SQL Analytics Endpoint Connection String",
            )
            # setdefault: a two-arg Sql.Database elsewhere may have already
            # recorded richer metadata (db_name) for the same hostname.
            hostname_param.metadata.setdefault("domain_type", domain_type.lower())
            hostname_param.metadata.setdefault("domain_suffix", domain_suffix.lower())
            hostname_param.metadata.setdefault("host_prefix", host_prefix)

        for (
            host_prefix,
            sql_string,
            domain_type,
            domain_suffix,
            db_name,
            uuid,
        ) in self.SQL_DATABASE_PATTERN.findall(content):
            full_hostname = f"{host_prefix}-{sql_string}.{domain_type}.{domain_suffix}"
            is_sql_database = domain_type.lower() == "database"

            hostname_param = self._add_parameter(
                full_hostname,
                source_file,
                "SQL Database Connection String"
                if is_sql_database
                else "SQL Analytics Endpoint Connection String",
            )
            if hostname_param:
                hostname_param.metadata["domain_type"] = domain_type.lower()
                hostname_param.metadata["domain_suffix"] = domain_suffix.lower()
                hostname_param.metadata["host_prefix"] = host_prefix
                hostname_param.metadata["db_name"] = db_name

            # The UUID may appear as an explicit "-<uuid>" suffix OR as the
            # db_name itself when there's no human prefix.
            uuid_to_add = uuid
            if not uuid_to_add and db_name and self.UUID_PATTERN.fullmatch(db_name):
                uuid_to_add = db_name

            if uuid_to_add:
                _ = self._add_parameter(
                    uuid_to_add,
                    source_file,
                    "SQL Database Item ID"
                    if is_sql_database
                    else "Lakehouse SQL Analytics Endpoint Item ID",
                )

            # SQL Database connections store the full "<display_name>-<itemId>"
            # form in TMDL — capture it so users can override via manual_parameters.
            if uuid and db_name:
                _ = self._add_parameter(f"{db_name}-{uuid}", source_file, "SQL Database Name")

    def _extract_from_pipeline(self, file_path: Path, data: dict) -> None:
        """Extract connection IDs from pipeline JSON."""
        source_file = str(file_path.relative_to(self.repo_dir))
        self._extract_connections_recursive(data, source_file)

    def _extract_connections_recursive(self, obj, source_file: str) -> None:
        """Recursively extract connection IDs from nested JSON structures."""
        if isinstance(obj, dict):
            if "externalReferences" in obj:
                ext_refs = obj["externalReferences"]
                if isinstance(ext_refs, dict) and "connection" in ext_refs:
                    self._add_uuid_parameter(
                        value=ext_refs["connection"],
                        source_file=source_file,
                        description="External connection ID",
                    )

            # workspaceId appears in TridentNotebook / InvokePipeline activities.
            if "workspaceId" in obj:
                _ = self._classify_activity_workspace(obj["workspaceId"], source_file)

            for value in obj.values():
                self._extract_connections_recursive(value, source_file)

        elif isinstance(obj, list):
            for item in obj:
                self._extract_connections_recursive(item, source_file)

    def _anchor_workspace(self, ws_id: str, source_file: str | None = None) -> None:
        """Record `ws_id` as a workspace this repo's content was exported from.

        Anchors come from legacy notebook attachments, Direct Lake OneLake
        URLs, bound UDF buttons and the deployer-listed sibling workspaces.
        With `source_file` the id also becomes a `$workspace.$id` rule; a
        seed without one stays an anchor only and gets a rule when
        `_register_source_ids` finds it in an item file. The order anchors
        and pipeline references are found in must not matter: an id already
        classified as a cross-workspace reference is upgraded here and
        dropped from the off-target list.
        """
        if not self.UUID_PATTERN.fullmatch(ws_id) or ws_id == self.NULL_UUID:
            return
        self._source_workspace_ids.add(ws_id)
        if source_file is not None:
            _ = self._add_parameter(ws_id, source_file, "Workspace ID")
        param = self.parameters.get(ws_id)
        if param is not None and not param.dynamic_variable:
            param.description = "Workspace ID"
            param.dynamic_variable = "$workspace.$id"
        self.offtarget_workspace_refs = [
            ref for ref in self.offtarget_workspace_refs if ref[1] != ws_id
        ]

    def _register_source_ids(self) -> None:
        """Turn deployer-resolved source ids into rules where item files use them.

        Legacy notebook metadata made the source workspace and lakehouse ids
        detected values, so fabric-cicd's global find_replace also rewrote any
        other occurrence — an `abfss://<ws>@…/<lakehouse>/` literal in
        notebook code, say. Auto-bound metadata carries neither id, so the
        same coverage comes from the ids the deployer resolved from the
        sibling workspaces: a rule per id, only where an item file contains
        it. A lakehouse id maps by display name and must belong to a repo
        Lakehouse item, or the rule could not resolve in the target.
        """
        wanted: dict[str, tuple[str, str]] = {}
        for workspace_id in self._seed_workspace_ids:
            if self.UUID_PATTERN.fullmatch(workspace_id) and workspace_id != self.NULL_UUID:
                wanted[workspace_id] = ("Workspace ID", "$workspace.$id")
        for lakehouse_id, name in self._seed_lakehouse_ids.items():
            if name not in self._lakehouse_names:
                continue
            if not self.UUID_PATTERN.fullmatch(lakehouse_id) or lakehouse_id == self.NULL_UUID:
                continue
            wanted[lakehouse_id] = ("Lakehouse ID", f"$items.Lakehouse.{name}.$id")
        if not wanted:
            return

        found = find_in_item_files(self.repo_dir, wanted)
        for value, (description, dynamic_variable) in wanted.items():
            files = found.get(value, set())
            param = self.parameters.get(value)
            if param is None:
                if not files:
                    continue
                param = DetectedParameter(find_value=value, description=description)
                self.parameters[value] = param
            param.source_files |= files
            if not param.dynamic_variable:
                param.description = description
                param.dynamic_variable = dynamic_variable

    def _classify_activity_workspace(self, ws_id, source_file: str) -> bool:
        """Remap or preserve a pipeline/copyjob workspaceId. Returns True if
        the id already points at the deploy target: the source workspace
        (parameterized to `$workspace.$id`) or the null-UUID "this workspace"
        placeholder (environment-independent, needs no rule — see NULL_UUID).

        Only ids the repo's own items anchor (notebook attachments,
        semantic-model OneLake URLs) map to `$workspace.$id`. Anything else is
        a deliberate cross-workspace reference — e.g. one workspace's
        `merge_shared_facts` invoking another workspace's notebook — recorded
        with no dynamic variable so it deploys verbatim. It still lands in
        `parameters`, so a fabdep.yml manual_parameters entry can remap one.
        """
        if not isinstance(ws_id, str) or not self.UUID_PATTERN.fullmatch(ws_id):
            return False
        if ws_id == self.NULL_UUID:
            return True
        if ws_id in self._source_workspace_ids:
            _ = self._add_parameter(ws_id, source_file, "Workspace ID")
            return True
        _ = self._add_parameter(ws_id, source_file, "Cross-workspace reference")
        if (source_file, ws_id) not in self.offtarget_workspace_refs:
            self.offtarget_workspace_refs.append((source_file, ws_id))
            self.logger.warning(
                f"{source_file}: workspaceId {ws_id} is not a workspace this "
                f"repo's content anchors — treating it as a cross-workspace "
                f"reference and deploying it unchanged. If it is one of this "
                f"repo's own workspaces, the deploying identity must be able "
                f"to list it (fabdep detect takes --source-workspace-id); "
                f"otherwise a manual_parameters entry in fabdep.yml can remap it."
            )
        return False

    def _extract_from_copyjob(self, file_path: Path, data: dict) -> None:
        """Extract workspace and lakehouse IDs from CopyJob JSON."""
        source_file = str(file_path.relative_to(self.repo_dir))

        for section_name in ["source", "destination"]:
            section = data.get("properties", {}).get(section_name, {})
            type_properties = section.get("connectionSettings", {}).get("typeProperties", {})

            ws_id = type_properties.get("workspaceId")
            artifact_id = type_properties.get("artifactId")
            if ws_id and not self._classify_activity_workspace(ws_id, source_file):
                # The artifact lives in that foreign workspace too — mapping
                # it to the repo's Lakehouse would corrupt the reference.
                if artifact_id and self.UUID_PATTERN.fullmatch(str(artifact_id)):
                    _ = self._add_parameter(artifact_id, source_file, "Cross-workspace reference")
                continue
            self._add_uuid_parameter(artifact_id, source_file, "Lakehouse ID")

    def _index_user_data_functions(self) -> None:
        """Index the repo's `.UserDataFunction` items by the functions they declare.

        A PBIR button names the *function* it calls (`update_report_json`),
        not the item that hosts it, and the itemId it carries is the source
        workspace's — useless for identifying the repo item. The declared
        function names in each item's `definition.json` are the only portable
        link, so they are what a rebind is matched on.
        """
        self._udf_functions = {}
        self._userdatafunction_names = []
        for item_dir, meta in self._platform_items("UserDataFunction"):
            display_name = meta.get("displayName")
            if not display_name:
                continue
            self._userdatafunction_names.append(display_name)
            definition = item_dir / "definition.json"
            if not definition.exists():
                continue
            try:
                functions = json.loads(definition.read_text(encoding="utf-8")).get("functions", [])
            except Exception as e:
                self._log_file_warning(definition, e, "user data function definition")
                continue
            for fn in functions:
                name = fn.get("name") if isinstance(fn, dict) else None
                if name:
                    self._udf_functions.setdefault(name, set()).add(display_name)

    @staticmethod
    def _pbir_literal(node) -> str:
        """Unwrap a PBIR `{"expr": {"Literal": {"Value": "'x'"}}}` node to `x`.

        Returns "" for any other shape — including the `"''"` empty literal an
        unbound button carries. This is the structural gate: anything that
        isn't exactly this shape is left untouched rather than guessed at.
        """
        if not isinstance(node, dict):
            return ""
        value = node.get("expr", {})
        if not isinstance(value, dict):
            return ""
        literal = value.get("Literal", {})
        if not isinstance(literal, dict):
            return ""
        raw = literal.get("Value")
        if not isinstance(raw, str):
            return ""
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
            raw = raw[1:-1]
        return raw.strip()

    def _iter_data_function_links(self, obj):
        """Yield every `dataFunction` block with a `byReference` under `obj`."""
        if isinstance(obj, dict):
            candidate = obj.get("dataFunction")
            if isinstance(candidate, dict) and isinstance(candidate.get("byReference"), dict):
                yield candidate
            for value in obj.values():
                yield from self._iter_data_function_links(value)
        elif isinstance(obj, list):
            for item in obj:
                yield from self._iter_data_function_links(item)

    def _resolve_udf_name(self, function_name: str) -> str | None:
        """Which repo `.UserDataFunction` item declares `function_name`, if exactly one.

        Ambiguity (two items declaring the same function) and absence (the
        button calls something no repo item provides) both return None — the
        caller then leaves the reference alone rather than binding it wrong.
        """
        if not function_name:
            return None
        owners = self._udf_functions.get(function_name, set())
        if len(owners) == 1:
            return next(iter(owners))
        return None

    def _extract_from_report(self, file_path: Path, data: dict) -> None:
        """Rebind PBIR User Data Function buttons to the target workspace.

        Conservative by design — a reference is only parameterized when every
        structural gate holds: the literal wrappers are the expected shape,
        both GUIDs are real UUIDs, and the named function resolves to exactly
        one `.UserDataFunction` item in the repo. Anything else is warned about
        and left verbatim, because a wrong rebind points a live button at the
        wrong workspace's function.
        """
        source_file = str(file_path.relative_to(self.repo_dir))
        report_name = self._report_item_name(file_path)

        for block in self._iter_data_function_links(data):
            by_reference = block["byReference"]
            item_id = self._pbir_literal(by_reference.get("itemId"))
            workspace_id = self._pbir_literal(by_reference.get("workspaceId"))

            # An unbound button carries "''" for both — nothing to rebind.
            if not item_id and not workspace_id:
                continue
            if not self.UUID_PATTERN.fullmatch(item_id):
                self.logger.warning(
                    f"{source_file}: User Data Function reference has no usable "
                    f"itemId ({item_id or 'empty'}) — left as-is."
                )
                continue

            metadata = block.get("metadata", {})
            function_name = ""
            if isinstance(metadata, dict):
                inner = metadata.get("dataFunction", {})
                if isinstance(inner, dict):
                    function_name = inner.get("name") or ""

            udf_name = self._resolve_udf_name(function_name)
            if not udf_name:
                owners = self._udf_functions.get(function_name, set())
                reason = (
                    f"'{function_name}' is declared by {len(owners)} "
                    f".UserDataFunction items ({', '.join(sorted(owners))}), so the "
                    f"host is ambiguous"
                    if len(owners) > 1
                    else f"no .UserDataFunction item in the repo declares "
                    f"'{function_name or '(unnamed)'}'"
                )
                self.udf_skipped.append((report_name, function_name, item_id, reason))
                continue

            param = self._add_parameter(item_id, source_file, "User Data Function Item ID")
            param.dynamic_variable = f"$items.UserDataFunction.{udf_name}.$id"

            # The hosting workspace is the one the UDF item deploys into — the
            # deploy target — since it was matched from this same repo. That
            # also makes it a source-workspace anchor.
            if workspace_id:
                self._anchor_workspace(workspace_id, source_file)

            binding = (report_name, function_name, udf_name, item_id)
            if binding not in self.udf_bindings:
                self.udf_bindings.append(binding)

    def _report_item_name(self, file_path: Path) -> str:
        """Display name of the `.Report` item a definition file belongs to."""
        for parent in file_path.parents:
            if parent.name.endswith(".Report"):
                try:
                    meta = json.loads((parent / ".platform").read_text(encoding="utf-8"))[
                        "metadata"
                    ]
                    return meta.get("displayName") or parent.stem
                except Exception:
                    return parent.stem
        return file_path.name

    def _ensure_detected(self) -> None:
        """Run detect_all() if no parameters have been detected yet.

        Detection is idempotent within a session — once parameters are
        populated, repeat calls are no-ops. Public methods that depend on
        detected state call this so users can skip the explicit detect_all()
        invocation.
        """
        if not self.parameters:
            self.detect_all()

    def generate_parameter_yml(self, environment: str = "prod") -> dict:
        """
        Generate parameter structure with detected parameters for auto-detect mode.

        Uses dynamic variables that are resolved at deploy time by fabric-cicd.
        Parameters without a dynamic variable are skipped.

        Args:
            environment: Environment name (default: "prod")

        Returns:
            Dictionary with find_replace entries using dynamic variables
        """
        self._ensure_detected()

        find_replace = []

        def _get_sort_key(param: DetectedParameter) -> tuple[int, str]:
            if "Workspace ID" in param.description:
                priority = 0
            elif "Lakehouse ID" in param.description:
                priority = 1
            elif "SQL" in param.description:
                priority = 2
            else:
                priority = 3
            return (priority, param.description)

        for param in sorted(self.parameters.values(), key=_get_sort_key):
            if not param.dynamic_variable:
                continue
            find_replace.append(param.to_dict(environment=environment))

        return {"find_replace": find_replace}

    def save_parameter_yml(self, output_path: Path | str, environment: str = "prod") -> None:
        """
        Generate and save parameter file with dynamic variables for auto-detect mode.

        Args:
            output_path: Path to save parameter file
            environment: Environment name (default: "prod")
        """
        param_dict = self.generate_parameter_yml(environment=environment)

        # Hand-rolled YAML emit so every value stays quoted — dynamic
        # variables ($workspace.$id) and GUIDs read unambiguously.
        output_path = Path(output_path)
        with open(output_path, "w", encoding="utf-8") as f:
            _ = f.write("find_replace:\n")
            for entry in param_dict["find_replace"]:
                _ = f.write(f'  - find_value: "{entry["find_value"]}"\n')
                if "item_type" in entry:
                    _ = f.write(f"    item_type: {entry['item_type']}\n")
                _ = f.write("    replace_value:\n")
                for env, val in entry["replace_value"].items():
                    _ = f.write(f'      {env}: "{val}"\n')

    def should_skip_replacement(self, target_workspace_id: str) -> bool:
        """
        Check if target workspace matches ALL detected source workspace IDs.

        When ALL detected workspace IDs match the target workspace, parameter
        replacement should be skipped since all IDs are already correct.

        Args:
            target_workspace_id: Destination workspace ID to check

        Returns:
            True if ALL detected workspace IDs match target (skip replacement),
            False otherwise
        """
        self._ensure_detected()

        detected_workspace_ids = [
            param.find_value
            for param in self.parameters.values()
            if param.description == "Workspace ID"
        ]
        if not detected_workspace_ids:
            return False
        return all(wid == target_workspace_id for wid in detected_workspace_ids)

    def filter_lakehouse_params(self) -> list[str]:
        """
        Remove lakehouse-related parameters from detected parameters.

        Used for feature branch deployments where lakehouse references should
        remain pointing to the dev workspace's lakehouse instead of being replaced.

        Parameters with these descriptions are removed:
        - "Lakehouse ID"
        - "SQL Analytics Endpoint Connection String"
        - "Lakehouse SQL Analytics Endpoint Item ID"

        Returns:
            List of find_values that were removed
        """
        self._ensure_detected()

        removed = []
        for find_value, param in list(self.parameters.items()):
            if param.description in self.LAKEHOUSE_DESCRIPTIONS:
                del self.parameters[find_value]
                removed.append(find_value)

        return removed

    def print_detection_summary(self) -> None:
        """Log a summary of detected parameters."""
        self._ensure_detected()

        if self.auto_bound_notebooks:
            self.logger.info(
                f"{len(self.auto_bound_notebooks)} notebook(s) use lakehouse "
                f"auto-binding (metadata carries the lakehouse logicalId and "
                f"no workspace id)"
            )
        anchors = ", ".join(sorted(self._source_workspace_ids))
        self.logger.info(
            "Source workspace anchor(s): "
            + (anchors or "(none — unanchored workspaceIds deploy verbatim)")
        )
        with_dynamic = sum(1 for p in self.parameters.values() if p.dynamic_variable)
        without_dynamic = len(self.parameters) - with_dynamic

        self.logger.info(
            f"Detected {len(self.parameters)} parameters "
            f"({with_dynamic} with dynamic variables, {without_dynamic} require manual setup):"
        )

        for param in self.parameters.values():
            self.logger.info(f"  {param.description or 'Unknown'}")
            self.logger.info(f"    Source value: {param.find_value}")
            if param.dynamic_variable:
                self.logger.info(f"    Dynamic variable: {param.dynamic_variable}")
            elif param.description == "SQL Database SQL Endpoint Item ID":
                # No fabric-cicd dynamic exists — the deployer fills in a
                # literal from the target workspace (resolve_warehouse_endpoint_params).
                self.logger.info(
                    "    Dynamic variable: (resolved from target workspace at deploy time)"
                )
            else:
                self.logger.info("    Dynamic variable: (skipped - requires manual setup)")
            if param.item_types:
                self.logger.info(f"    Item Types: {', '.join(sorted(param.item_types))}")
            self.logger.info(f"    Found in: {len(param.source_files)} file(s)")

        if without_dynamic > 0:
            self.logger.info(
                f"Note: {without_dynamic} parameter(s) will be skipped in auto-generated parameter.yml"
            )
            self.logger.info(
                "These typically require external setup (e.g., connection IDs) and don't change between deployments."
            )
