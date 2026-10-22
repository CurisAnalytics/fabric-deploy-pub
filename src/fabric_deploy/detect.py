"""Auto-detection of source IDs from workspace repository for parameter generation."""

# pyright: reportMissingTypeArgument=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownParameterType=false
# pyright: reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false

import json
import logging
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .log import get_logger

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


class Kind(StrEnum):
    """What a detected value is. The value doubles as its display label."""

    WORKSPACE = "Workspace ID"
    LAKEHOUSE = "Lakehouse ID"
    SQL_ENDPOINT_HOST = "SQL Analytics Endpoint Connection String"
    SQL_ENDPOINT_ID = "Lakehouse SQL Analytics Endpoint Item ID"
    SQLDB_HOST = "SQL Database Connection String"
    SQLDB_ID = "SQL Database Item ID"
    # No fabric-cicd attribute exists for it: the deployer fills in a literal
    # from the target workspace (params.resolve_warehouse_endpoint_params).
    SQLDB_ENDPOINT_ID = "SQL Database SQL Endpoint Item ID"
    WAREHOUSE = "Warehouse ID"
    WAREHOUSE_ATTACHMENT = "Notebook warehouse attachment"
    # Tenant-wide, so the same in every environment.
    CONNECTION = "External connection ID"
    CROSS_WORKSPACE = "Cross-workspace reference"
    UDF = "User Data Function Item ID"


# Kind -> (repo item types that can answer for it, in preference order; the
# fabric-cicd attribute; whether it must be one specific item). A host is the
# workspace's SQL server, shared by every endpoint in the workspace, so any
# matching item resolves it.
ITEM_RULES: dict[Kind, tuple[tuple[str, ...], str, bool]] = {
    Kind.LAKEHOUSE: (("Lakehouse",), "$id", True),
    Kind.SQL_ENDPOINT_HOST: (("Lakehouse", "MirroredDatabase"), "$sqlendpoint", False),
    Kind.SQL_ENDPOINT_ID: (("Lakehouse", "MirroredDatabase"), "$sqlendpointid", True),
    # serverFqdn carries a ",1433" suffix; the deployer swaps in the bare host
    # (params.resolve_sqldatabase_hostname_params) and keeps this as fallback.
    Kind.SQLDB_HOST: (("SQLDatabase",), "$sqlendpoint", False),
    Kind.SQLDB_ID: (("SQLDatabase",), "$id", True),
    Kind.WAREHOUSE: (("Warehouse",), "$id", True),
    Kind.UDF: (("UserDataFunction",), "$id", True),
}


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

    def to_dict(self, environment: str = "prod") -> dict:
        """The fabric-cicd find_replace entry, its replace_value keyed by `environment`."""
        entry: dict = {"find_value": self.find_value}
        if self.item_types:
            entry["item_type"] = sorted(self.item_types)
        entry["replace_value"] = {environment: self.dynamic_variable}
        return entry


class ParameterDetector:
    """Detects source IDs from workspace repository files."""

    # Stripped during feature-branch deploys (`-L` / skip_lakehouse).
    LAKEHOUSE_KINDS = frozenset({Kind.LAKEHOUSE, Kind.SQL_ENDPOINT_HOST, Kind.SQL_ENDPOINT_ID})

    # `known_warehouses[].type` in notebook metadata. The GUID is a SQLEndpoint
    # item id for Lakewarehouse (the lakehouse's SQL analytics endpoint) and
    # MountedWarehouse (the SQL database's endpoint); a Warehouse item id for
    # Datawarehouse.
    WAREHOUSE_ATTACHMENT_KINDS: dict[str, Kind] = {
        "lakewarehouse": Kind.SQL_ENDPOINT_ID,
        "mountedwarehouse": Kind.SQLDB_ENDPOINT_ID,
        "datawarehouse": Kind.WAREHOUSE,
    }

    UUID_PATTERN = re.compile(UUID, re.IGNORECASE)

    # Fabric's "unattached / placeholder" sentinel. Appears as workspaceId in
    # TridentNotebook pipeline activities (fabric-cicd's _replace_workspace_ids
    # handles those itself) and as default_lakehouse_workspace_id in notebooks
    # that were never attached. Never emit a find_replace rule for it.
    NULL_UUID = "00000000-0000-0000-0000-000000000000"

    # Sql.Database("<host>", "<database>") and the one-argument server-level
    # Sql.Databases("<host>"), on a Fabric SQL endpoint or SQL database host.
    SQL_HOST_PATTERN = re.compile(
        r'Sql\.Databases?\(\s*"([^"]+\.(datawarehouse|database)\.'
        r'(?:fabric\.microsoft\.com|pbidedicated\.windows\.net))"(?:\s*,\s*"([^"]*)")?',
        re.IGNORECASE,
    )
    # A database argument naming an item: "<display name>-<item id>" (SQL
    # databases) or the bare item id (Direct Lake on a SQL endpoint).
    DB_ITEM_PATTERN = re.compile(rf"(?:(.+)-)?({UUID})", re.IGNORECASE)

    # DirectLake semantic models reference the lakehouse via OneLake DFS URL:
    #   https://onelake.dfs.fabric.microsoft.com/<workspace_id>/<lakehouse_id>
    ONELAKE_DFS_PATTERN = re.compile(
        rf"onelake\.dfs\.fabric\.microsoft\.com/({UUID})/({UUID})", re.IGNORECASE
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
        self._detected = False
        self._seed_workspace_ids: set[str] = set(source_workspace_ids or ())
        self._seed_lakehouse_ids: dict[str, str] = dict(source_lakehouse_ids or {})
        # (item_dir, .platform metadata) for every repo item, shallowest first.
        self._items: list[tuple[Path, dict]] = []
        # Item type -> display names of the repo's items of that type.
        self._names: defaultdict[str, list[str]] = defaultdict(list)
        # Repo-relative notebook dirs whose notebook-settings.json turns
        # Fabric's lakehouse auto-binding on.
        self.auto_bound_notebooks: list[str] = []
        # (source_file, guid, type) for each notebook warehouse attachment
        # that has no replacement mapping — the source-workspace GUID would
        # deploy verbatim. Deploy with --fail-on-stale promotes it to an error.
        self.unresolved_warehouse_bindings: list[tuple[str, str, str]] = []
        # Workspace ids this repo's own items were exported from (legacy
        # notebook attachments, semantic-model OneLake URLs, bound UDF buttons,
        # or the repo's sibling workspaces the deployer listed). Pipeline/
        # CopyJob workspaceIds are only remapped to the target when they match
        # one of these — anything else is a deliberate cross-workspace
        # reference that must deploy verbatim.
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
        self.logger: logging.Logger = get_logger(logger_name or __name__)

    @property
    def sqldatabase_names(self) -> list[str]:
        """Display names of .SQLDatabase items found in the repo."""
        return list(self._names["SQLDatabase"])

    def _log_file_warning(self, file_path: Path, error: Exception, context: str = "") -> None:
        context_str = f" {context} from" if context else ""
        self.logger.warning(f"Failed to read{context_str} {file_path}: {error}")

    def _is_id(self, value: object) -> bool:
        """Whether `value` is a real item/workspace GUID (not the null placeholder)."""
        return (
            isinstance(value, str)
            and bool(self.UUID_PATTERN.fullmatch(value))
            and value != self.NULL_UUID
        )

    def detect_all(self):
        "Detect all parameters from workspace files."
        self.parameters = {}
        self.unresolved_warehouse_bindings = []
        self.udf_bindings = []
        self.udf_skipped = []
        self._source_workspace_ids = set()
        self.offtarget_workspace_refs = []
        self.auto_bound_notebooks = []

        # Item names must be resolved before any parameter is added.
        self._index_items()
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
        self._process_files(
            self._item_files("Notebook", "notebook-content.py", "notebook-content.sql"),
            self._extract_from_notebook,
        )
        self._process_files(
            self._item_files("SemanticModel", "definition/**/*.tmdl"),
            self._extract_from_semantic_model,
        )
        # Only worth walking report definitions when the repo actually ships a
        # User Data Function to rebind the buttons to.
        if self._names["UserDataFunction"]:
            self._process_files(
                self._item_files("Report", "definition/**/*.json"),
                self._extract_from_report,
                parse_json=True,
            )
        self._process_files(
            self._item_files("DataPipeline", "pipeline-content.json"),
            self._extract_from_pipeline,
            parse_json=True,
        )
        self._process_files(
            self._item_files("CopyJob", "copyjob-content.json"),
            self._extract_from_copyjob,
            parse_json=True,
        )
        self._register_source_ids()
        self._warn_skipped_udf_buttons()
        self._detected = True

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

    def _index_items(self) -> None:
        """Read every item's `.platform` once — fabric-cicd discovers items by it.

        An item's type is its `.platform` `metadata.type`, NOT its directory
        suffix: Synapse-migrated notebooks sit in `*.SynapseNotebook/` dirs
        typed Notebook. Items can live in workspace subfolders (e.g.
        `ERP/northwind_sql_db.SQLDatabase`); shallower paths sort first.
        """
        self._items = []
        self._names = defaultdict(list)
        for platform_file in sorted(
            self.repo_dir.glob("**/.platform"), key=lambda p: (len(p.parts), str(p))
        ):
            try:
                metadata = json.loads(platform_file.read_text(encoding="utf-8"))["metadata"]
                item_type, name = metadata.get("type"), metadata.get("displayName")
            except Exception as e:
                self._log_file_warning(platform_file, e, "item metadata")
                continue
            self._items.append((platform_file.parent, metadata))
            if name:
                self._names[item_type].append(name)

    def _platform_items(self, item_type: str) -> list[tuple[Path, dict]]:
        """(item_dir, .platform metadata) for the repo's items of `item_type`."""
        return [(d, meta) for d, meta in self._items if meta.get("type") == item_type]

    def _item_files(self, item_type: str, *patterns: str) -> list[Path]:
        """Files matching `patterns` (globs relative to the item dir) in items of `item_type`."""
        return [
            f
            for item_dir, _ in self._platform_items(item_type)
            for pattern in patterns
            for f in sorted(item_dir.glob(pattern))
        ]

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

    def _process_files(self, files, extract_func, parse_json: bool = False) -> None:
        """Read each file (parsed as JSON with `parse_json`) and hand it to
        `extract_func(file_path, content)`; a file that fails is warned about
        and skipped."""
        for file_path in files:
            try:
                content = file_path.read_text(encoding="utf-8")
                extract_func(file_path, json.loads(content) if parse_json else content)
            except Exception as e:
                self._log_file_warning(file_path, e)

    def _add_parameter(
        self, find_value: str, source_file: str, kind: Kind, name: str | None = None
    ) -> DetectedParameter:
        """Record `find_value`, seen in `source_file`, as a reference of `kind`.

        `name` is the repo item the source says it refers to, if it says. The
        first sighting decides the replacement; later ones add their file.
        """
        param = self.parameters.get(find_value)
        if param is None:
            param = DetectedParameter(
                find_value=find_value,
                description=kind,
                dynamic_variable=self._dynamic(kind, find_value, name),
            )
            self.parameters[find_value] = param
        param.source_files.add(source_file)
        return param

    def _dynamic(self, kind: Kind, find_value: str, name: str | None = None) -> str:
        """The fabric-cicd dynamic variable a `kind` reference maps to, or ""
        when no repo item answers for it.

        A named reference binds to the repo item of that name, or to nothing
        (it lives outside the repo). An unnamed one that needs one specific
        item binds to the repo's only candidate; with several, guessing could
        rewire it to the wrong item, so it is left unchanged and warned about.
        """
        if kind is Kind.WORKSPACE:
            return "$workspace.$id"
        if kind not in ITEM_RULES:
            return ""
        item_types, attr, specific = ITEM_RULES[kind]
        if name:
            item_type = next((t for t in item_types if name in self._names[t]), None)
            return f"$items.{item_type}.{name}.{attr}" if item_type else ""
        item_type = next((t for t in item_types if self._names[t]), None)
        if item_type is None:
            return ""
        names = self._names[item_type]
        if len(names) > 1 and specific:
            self.logger.warning(
                f"{kind} {find_value}: nothing says which of the repo's {item_type} items "
                f"({', '.join(names)}) it refers to, so it deploys unchanged. Map it with a "
                f"manual_parameters entry in fabdep.yml."
            )
            return ""
        return f"$items.{item_type}.{names[0]}.{attr}"

    def _extract_from_notebook(self, file_path: Path, content: str) -> None:
        """Extract the lakehouse and warehouse attachments from notebook metadata."""
        source_file = str(file_path.relative_to(self.repo_dir))
        for block in _iter_meta_blocks(content):
            try:
                dependencies = json.loads(block).get("dependencies")
            except (json.JSONDecodeError, AttributeError):
                continue
            if not isinstance(dependencies, dict):
                continue
            if isinstance(lakehouse := dependencies.get("lakehouse"), dict):
                self._extract_lakehouse_attachment(source_file, lakehouse)
            if isinstance(warehouse := dependencies.get("warehouse"), dict):
                self._extract_warehouse_attachments(source_file, warehouse)

        if self._auto_binding_enabled(file_path.parent):
            self.auto_bound_notebooks.append(str(file_path.parent.relative_to(self.repo_dir)))

    def _extract_lakehouse_attachment(self, source_file: str, lakehouse: dict) -> None:
        """Map a notebook's default lakehouse and the workspace it lives in.

        An attachment to one of the repo's lakehouses also anchors its
        workspace as the one the repo was exported from. One naming no repo
        lakehouse (a shared lakehouse in another workspace, say) deploys
        unchanged. The null-UUID workspace is auto-binding's form: the
        lakehouse is named by its git logicalId, which fabric-cicd resolves
        before any parameter applies, so neither id needs a rule.
        """
        ws_id = lakehouse.get("default_lakehouse_workspace_id")
        lh_id = lakehouse.get("default_lakehouse")
        if ws_id == self.NULL_UUID or not self._is_id(lh_id):
            return
        name = lakehouse.get("default_lakehouse_name") or self._seed_lakehouse_ids.get(lh_id)
        if self._add_parameter(lh_id, source_file, Kind.LAKEHOUSE, name).dynamic_variable:
            self._anchor_workspace(ws_id, source_file)
        else:
            _ = self._classify_workspace(ws_id, source_file)

    def _extract_warehouse_attachments(self, source_file: str, warehouse: dict) -> None:
        """Map a notebook's `dependencies.warehouse` bindings.

        T-SQL notebooks attach to a lakehouse SQL analytics endpoint
        (`type: Lakewarehouse`) or a SQL database's endpoint
        (`type: MountedWarehouse`) via `default_warehouse`/`known_warehouses`.
        These GUIDs are environment-specific just like `default_lakehouse` —
        left unreplaced, the deployed notebook stays wired to the *source*
        workspace's endpoint (visible as a cross-workspace link in lineage).
        """
        known = warehouse.get("known_warehouses") or []
        types_by_id: dict[str, str] = {
            k["id"]: (k.get("type") or "") for k in known if isinstance(k, dict) and k.get("id")
        }
        if default_wh := warehouse.get("default_warehouse"):
            types_by_id.setdefault(default_wh, "")

        for wh_id, wh_type in types_by_id.items():
            if not self._is_id(wh_id):
                continue
            kind = self.WAREHOUSE_ATTACHMENT_KINDS.get(wh_type.lower(), Kind.WAREHOUSE_ATTACHMENT)
            param = self._add_parameter(wh_id, source_file, kind)

            # MountedWarehouse is resolved to a literal at deploy time —
            # but only if the repo has a SQLDatabase item to resolve from.
            resolvable_later = kind is Kind.SQLDB_ENDPOINT_ID and bool(self._names["SQLDatabase"])
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

    def _extract_from_semantic_model(self, file_path: Path, content: str) -> None:
        """Extract SQL connection strings and endpoint IDs from semantic models."""
        source_file = str(file_path.relative_to(self.repo_dir))

        for workspace_id, lakehouse_id in self.ONELAKE_DFS_PATTERN.findall(content):
            # A Direct Lake model's OneLake URL points at its own workspace —
            # a source-workspace anchor.
            self._anchor_workspace(workspace_id, source_file)
            if self._is_id(lakehouse_id):
                name = self._seed_lakehouse_ids.get(lakehouse_id)
                _ = self._add_parameter(lakehouse_id, source_file, Kind.LAKEHOUSE, name)

        for host, domain_type, database in self.SQL_HOST_PATTERN.findall(content):
            is_sql_database = domain_type.lower() == "database"
            _ = self._add_parameter(
                host, source_file, Kind.SQLDB_HOST if is_sql_database else Kind.SQL_ENDPOINT_HOST
            )
            if item := self.DB_ITEM_PATTERN.fullmatch(database):
                kind = Kind.SQLDB_ID if is_sql_database else Kind.SQL_ENDPOINT_ID
                _ = self._add_parameter(item[2], source_file, kind, name=item[1])

    def _extract_from_pipeline(self, file_path: Path, data: dict) -> None:
        """Extract connection IDs from pipeline JSON."""
        source_file = str(file_path.relative_to(self.repo_dir))
        self._extract_connections_recursive(data, source_file)

    def _extract_connections_recursive(self, obj, source_file: str) -> None:
        """Recursively extract connection IDs from nested JSON structures."""
        if isinstance(obj, dict):
            ext_refs = obj.get("externalReferences")
            if isinstance(ext_refs, dict) and self._is_id(ext_refs.get("connection")):
                _ = self._add_parameter(ext_refs["connection"], source_file, Kind.CONNECTION)

            # workspaceId appears in TridentNotebook / InvokePipeline activities.
            if "workspaceId" in obj:
                _ = self._classify_workspace(obj["workspaceId"], source_file)

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
        if not self._is_id(ws_id):
            return
        self._source_workspace_ids.add(ws_id)
        if source_file is not None:
            _ = self._add_parameter(ws_id, source_file, Kind.WORKSPACE)
        param = self.parameters.get(ws_id)
        if param is not None and not param.dynamic_variable:
            param.description = Kind.WORKSPACE
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
        wanted: dict[str, tuple[Kind, str]] = {
            workspace_id: (Kind.WORKSPACE, "$workspace.$id")
            for workspace_id in self._seed_workspace_ids
            if self._is_id(workspace_id)
        }
        for lakehouse_id, name in self._seed_lakehouse_ids.items():
            if name in self._names["Lakehouse"] and self._is_id(lakehouse_id):
                wanted[lakehouse_id] = (Kind.LAKEHOUSE, f"$items.Lakehouse.{name}.$id")
        if not wanted:
            return

        found = find_in_item_files(self.repo_dir, wanted)
        for value, (kind, dynamic_variable) in wanted.items():
            files = found.get(value, set())
            param = self.parameters.get(value)
            if param is None:
                if not files:
                    continue
                param = DetectedParameter(find_value=value, description=kind)
                self.parameters[value] = param
            param.source_files |= files
            if not param.dynamic_variable:
                param.description = kind
                param.dynamic_variable = dynamic_variable

    def _classify_workspace(self, ws_id, source_file: str) -> bool:
        """Remap or preserve a workspace id that no repo item of its own anchors.

        Returns True if the id already points at the deploy target: a source
        workspace (parameterized to `$workspace.$id`) or the null-UUID "this
        workspace" placeholder (environment-independent, needs no rule — see
        NULL_UUID). Anything else is a deliberate cross-workspace reference —
        e.g. a pipeline invoking a notebook in another workspace — recorded
        with no dynamic variable so it deploys verbatim.
        """
        if ws_id == self.NULL_UUID:
            return True
        if not self._is_id(ws_id):
            return False
        if ws_id in self._source_workspace_ids:
            _ = self._add_parameter(ws_id, source_file, Kind.WORKSPACE)
            return True
        _ = self._add_parameter(ws_id, source_file, Kind.CROSS_WORKSPACE)
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
            if not self._is_id(artifact_id):
                artifact_id = None
            if ws_id and not self._classify_workspace(ws_id, source_file):
                # The artifact lives in that foreign workspace too — mapping
                # it to the repo's Lakehouse would corrupt the reference.
                if artifact_id:
                    _ = self._add_parameter(artifact_id, source_file, Kind.CROSS_WORKSPACE)
            elif artifact_id:
                _ = self._add_parameter(artifact_id, source_file, Kind.LAKEHOUSE)

    def _index_user_data_functions(self) -> None:
        """Index the repo's `.UserDataFunction` items by the functions they declare.

        A PBIR button names the *function* it calls, not the item that hosts
        it, and the itemId it carries is the source workspace's — useless for
        identifying the repo item. The declared function names in each item's
        `definition.json` are the only portable link, so they are what a
        rebind is matched on.
        """
        self._udf_functions = {}
        for item_dir, meta in self._platform_items("UserDataFunction"):
            display_name = meta.get("displayName")
            definition = item_dir / "definition.json"
            if not display_name or not definition.exists():
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
        """Yield every `dataFunction` block with a `byReference` under `obj`.

        A PBIR action button invokes a User Data Function through a
        `visualLink` whose `dataFunction.byReference` carries the item and
        workspace ids as literals, and whose `metadata.dataFunction.name`
        names the function inside the item. Both ids are the SOURCE
        workspace's deployed ids, so a cross-workspace deploy ships buttons
        still calling the source function unless they are parameterized.
        """
        if isinstance(obj, dict):
            candidate = obj.get("dataFunction")
            if isinstance(candidate, dict) and isinstance(candidate.get("byReference"), dict):
                yield candidate
            for value in obj.values():
                yield from self._iter_data_function_links(value)
        elif isinstance(obj, list):
            for item in obj:
                yield from self._iter_data_function_links(item)

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
        report_name = self._item_name(file_path)

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

            # Matched on the declared function name, and only when exactly one
            # repo item declares it: an ambiguous or unknown function is left
            # alone rather than bound wrong.
            owners = self._udf_functions.get(function_name, set()) if function_name else set()
            if len(owners) != 1:
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
            udf_name = next(iter(owners))

            _ = self._add_parameter(item_id, source_file, Kind.UDF, udf_name)

            # The hosting workspace is the one the UDF item deploys into — the
            # deploy target — since it was matched from this same repo. That
            # also makes it a source-workspace anchor.
            if workspace_id:
                self._anchor_workspace(workspace_id, source_file)

            binding = (report_name, function_name, udf_name, item_id)
            if binding not in self.udf_bindings:
                self.udf_bindings.append(binding)

    def _item_name(self, file_path: Path) -> str:
        """Display name of the repo item a file belongs to."""
        for item_dir, meta in reversed(self._items):
            if item_dir in file_path.parents:
                return meta.get("displayName") or item_dir.stem
        return file_path.name

    def _ensure_detected(self) -> None:
        """Run detect_all() unless it already ran.

        Keyed on a flag, not on `parameters` being empty: filtering can
        legitimately empty the set, and re-detecting would undo the filter.
        """
        if not self._detected:
            self.detect_all()

    def generate_parameter_yml(self, environment: str = "prod") -> dict:
        """The find_replace document for every parameter with a replacement.

        Parameters without one (external connections, cross-workspace
        references, unresolved bindings) deploy unchanged and are left out.
        """
        self._ensure_detected()
        params = sorted(self.parameters.values(), key=lambda p: (p.description, p.find_value))
        return {"find_replace": [p.to_dict(environment) for p in params if p.dynamic_variable]}

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
        """Whether every detected workspace id already is the target's.

        Then nothing needs replacing. False when no workspace id was detected.
        """
        self._ensure_detected()
        workspace_ids = [
            p.find_value for p in self.parameters.values() if p.description == Kind.WORKSPACE
        ]
        return bool(workspace_ids) and all(w == target_workspace_id for w in workspace_ids)

    def filter_lakehouse_params(self) -> list[str]:
        """Drop the lakehouse parameters (`--skip-lakehouse`) and return their find_values.

        Feature-branch deploys keep reading the dev workspace's lakehouse, so
        its id, SQL endpoint host and endpoint id stay as they are.
        """
        self._ensure_detected()
        removed = [v for v, p in self.parameters.items() if p.description in self.LAKEHOUSE_KINDS]
        for find_value in removed:
            del self.parameters[find_value]
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
            elif param.description == Kind.SQLDB_ENDPOINT_ID:
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
