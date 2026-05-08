"""Auto-detection of source IDs from workspace repository for parameter generation."""

# pyright: reportMissingTypeArgument=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownParameterType=false
# pyright: reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .log import get_logger

regex = re.Pattern[str]


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

    def to_dict(self, use_dynamic: bool = True, environment: str = "prod") -> dict:
        """
        Convert to parameter entry format for auto-detect mode.

        Args:
            use_dynamic: Use dynamic variables in replace_value (default: True)
            environment: Environment name (default: "prod")

        Returns:
            Dictionary in parameter entry format with dynamic variables
        """
        entry: dict = {"find_value": self.find_value}
        if self.item_types:
            entry["item_type"] = sorted(self.item_types)

        # fabric-cicd requires replace_value to be an env-keyed dict, even
        # though we only emit one environment ("fabdep") in practice.
        if use_dynamic and self.dynamic_variable:
            entry["replace_value"] = {environment: self.dynamic_variable}  # pyright: ignore[reportArgumentType]
        else:
            entry["replace_value"] = {environment: ""}  # pyright: ignore[reportArgumentType]

        return entry


class ParameterDetector:
    """Detects source IDs from workspace repository files."""

    # Stripped during feature-branch deploys (`-L` / skip_lakehouse).
    LAKEHOUSE_DESCRIPTIONS: set[str] = {
        "Lakehouse ID",
        "SQL Analytics Endpoint Connection String",
        "Lakehouse SQL Analytics Endpoint Item ID",
    }

    UUID_PATTERN: regex = re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
    )

    # Sql.Database("HOST-SQLSTRING.{datawarehouse|database}.{fabric.microsoft.com|pbidedicated.windows.net}", "DB-NAME" or "DB-NAME-UUID")
    # Groups: (1) host prefix, (2) sql endpoint string, (3) domain type, (4) domain suffix, (5) db name, (6) optional UUID suffix
    SQL_DATABASE_PATTERN: regex = re.compile(
        r'Sql\.Database\(\s*"([^"]+)-([A-Z0-9]+)\.(datawarehouse|database)\.(fabric\.microsoft\.com|pbidedicated\.windows\.net)"\s*,\s*"([^"]+?)(?:-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}))?"\s*\)',
        re.IGNORECASE,
    )

    WORKSPACE_ID_PATTERN: regex = re.compile(
        r'"default_lakehouse_workspace_id":\s*"([0-9a-f-]+)"', re.IGNORECASE
    )
    LAKEHOUSE_ID_PATTERN: regex = re.compile(
        r'"default_lakehouse":\s*"([0-9a-f-]+)"', re.IGNORECASE
    )

    # DirectLake semantic models reference the lakehouse via OneLake DFS URL:
    #   https://onelake.dfs.fabric.microsoft.com/<workspace_id>/<lakehouse_id>
    ONELAKE_DFS_PATTERN: regex = re.compile(
        r"onelake\.dfs\.fabric\.microsoft\.com/"
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/"
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        re.IGNORECASE,
    )

    def __init__(self, repo_dir: Path | str, logger_name: str | None = None):
        """
        Initialize parameter detector.

        Args:
            repo_dir: Path to workspace repository directory
            logger_name: Optional logger name (defaults to module name)
        """
        self.repo_dir = Path(repo_dir).resolve()  # pyright: ignore[reportUnannotatedClassAttribute]
        self.parameters: dict[str, DetectedParameter] = {}
        self._lakehouse_names: list[str] = []
        self._sqldatabase_names: list[str] = []
        self.logger: logging.Logger = get_logger(logger_name or __name__)

    def _log_file_warning(
        self, file_path: Path, error: Exception, context: str = ""
    ) -> None:
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

        # Item names must be resolved before _generate_dynamic_variable runs.
        self._detect_lakehouse_names()
        self._detect_sqldatabase_names()

        self._process_files(
            "**/*.Notebook/notebook-content.py", self._extract_from_notebook
        )
        self._process_files(
            "**/*.SemanticModel/definition/**/*.tmdl",
            self._extract_from_semantic_model,
        )
        self._process_files(
            "**/*.DataPipeline/pipeline-content.json",
            self._extract_from_pipeline,
            parse_json=True,
        )
        self._process_files(
            "**/*.CopyJob/copyjob-content.json",
            self._extract_from_copyjob,
            parse_json=True,
        )

    def _detect_item_names(self, item_type: str) -> list[str]:
        """
        Detect item names from .{item_type}/.platform files.

        Args:
            item_type: Fabric item type (e.g., "Lakehouse", "SQLDatabase")

        Returns:
            List of display names found for items of this type
        """
        names: list[str] = []
        item_dirs = self.repo_dir.glob(f"*.{item_type}")
        for item_dir in item_dirs:
            platform_file = item_dir / ".platform"
            if not platform_file.exists():
                continue
            try:
                metadata = json.loads(platform_file.read_text(encoding="utf-8"))
                display_name = metadata.get("metadata", {}).get("displayName", "")
                if display_name:
                    names.append(display_name)
            except Exception as e:
                self._log_file_warning(platform_file, e, f"{item_type} metadata")
        return names

    def _detect_lakehouse_names(self) -> None:
        """Detect lakehouse names from .Lakehouse/.platform files."""
        self._lakehouse_names = self._detect_item_names("Lakehouse")

    def _detect_sqldatabase_names(self) -> None:
        """Detect SQL Database names from .SQLDatabase/.platform files."""
        self._sqldatabase_names = self._detect_item_names("SQLDatabase")

    def _process_files(
        self, glob_pattern: str, extract_func, parse_json: bool = False
    ) -> None:
        """
        Generic file processor that handles glob, iteration, reading, and error handling.

        Args:
            glob_pattern: Glob pattern to match files
            extract_func: Callback function that receives (file_path, content_or_data)
            parse_json: If True, parse content as JSON before passing to extract_func
        """
        files = self.repo_dir.glob(glob_pattern)
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

    def _add_uuid_parameter(
        self, value: str, source_file: str, description: str = ""
    ):
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

        if self._sqldatabase_names:
            sqldatabase_name = self._sqldatabase_names[0]
            # Resolves to body/properties/serverFqdn (.database.fabric.microsoft.com).
            # Caveat: on a fresh `-N` deploy the SQLDatabase may not be
            # provisioned yet — manual_parameters overrides take precedence.
            if "sql database connection string" in desc_lower:
                return f"$items.SQLDatabase.{sqldatabase_name}.$sqlendpoint"
            if "sql database item id" in desc_lower:
                return f"$items.SQLDatabase.{sqldatabase_name}.$id"
            # "<display_name>-<uuid>" form — no $name attribute in fabric-cicd,
            # so this requires a manual_parameters entry.
            if "sql database name" in desc_lower:
                return ""

        # External connection IDs are tenant-wide; not resolvable from repo.
        if "connection id" in desc_lower:
            return ""

        return ""

    def _extract_from_notebook(self, file_path: Path, content: str) -> None:
        """Extract workspace and lakehouse IDs from notebook metadata."""
        source_file = str(file_path.relative_to(self.repo_dir))

        for workspace_id in self.WORKSPACE_ID_PATTERN.findall(content):
            _ = self._add_parameter(workspace_id, source_file, "Workspace ID")

        for lakehouse_id in self.LAKEHOUSE_ID_PATTERN.findall(content):
            _ = self._add_parameter(lakehouse_id, source_file, "Lakehouse ID")

    def _extract_from_semantic_model(self, file_path: Path, content: str) -> None:
        """Extract SQL connection strings and endpoint IDs from semantic models."""
        source_file = str(file_path.relative_to(self.repo_dir))

        for workspace_id, lakehouse_id in self.ONELAKE_DFS_PATTERN.findall(content):
            self._add_uuid_parameter(workspace_id, source_file, "Workspace ID")
            self._add_uuid_parameter(lakehouse_id, source_file, "Lakehouse ID")

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
                _ = self._add_parameter(
                    f"{db_name}-{uuid}", source_file, "SQL Database Name"
                )

    def _extract_from_pipeline(self, file_path: Path, data: dict) -> None:
        """Extract connection IDs from pipeline JSON."""
        source_file = str(file_path.relative_to(self.repo_dir))
        self._extract_connections_recursive(data, source_file)

    # TridentNotebook activities use this placeholder; fabric-cicd's
    # _replace_workspace_ids handles them directly, so don't double-substitute.
    NULL_UUID = "00000000-0000-0000-0000-000000000000"

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
            if "workspaceId" in obj and obj.get("workspaceId") != self.NULL_UUID:
                self._add_uuid_parameter(
                    value=obj["workspaceId"],
                    source_file=source_file,
                    description="Workspace ID",
                )

            for value in obj.values():
                self._extract_connections_recursive(value, source_file)

        elif isinstance(obj, list):
            for item in obj:
                self._extract_connections_recursive(item, source_file)

    def _extract_from_copyjob(self, file_path: Path, data: dict) -> None:
        """Extract workspace and lakehouse IDs from CopyJob JSON."""
        source_file = str(file_path.relative_to(self.repo_dir))

        for section_name in ["source", "destination"]:
            section = data.get("properties", {}).get(section_name, {})
            type_properties = section.get("connectionSettings", {}).get(
                "typeProperties", {}
            )

            self._add_uuid_parameter(
                type_properties.get("workspaceId"),
                source_file,
                "Workspace ID",
            )
            self._add_uuid_parameter(
                type_properties.get("artifactId"),
                source_file,
                "Lakehouse ID",
            )

    def _ensure_detected(self) -> None:
        """Run detect_all() if no parameters have been detected yet.

        Detection is idempotent within a session — once parameters are
        populated, repeat calls are no-ops. Public methods that depend on
        detected state call this so users can skip the explicit detect_all()
        invocation.
        """
        if not self.parameters:
            self.detect_all()

    def generate_parameter_yml(
        self,
        use_dynamic: bool = True,
        environment: str = "prod",
        include_descriptions: bool = False,
        skip_empty: bool = True,
    ) -> dict:
        """
        Generate parameter structure with detected parameters for auto-detect mode.

        Uses dynamic variables that are resolved at deploy time by fabric-cicd.

        Args:
            use_dynamic: Use dynamic variables in replace_value (default: True)
            environment: Environment name (default: "prod")
            include_descriptions: Include comments with descriptions
            skip_empty: Skip parameters without dynamic variables (default: True)

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
            if skip_empty and use_dynamic and not param.dynamic_variable:
                continue
            entry = param.to_dict(use_dynamic=use_dynamic, environment=environment)
            if include_descriptions and param.description:
                entry["_comment"] = param.description
            find_replace.append(entry)

        return {"find_replace": find_replace}

    def save_parameter_yml(
        self,
        output_path: Path | str,
        use_dynamic: bool = True,
        environment: str = "prod",
        skip_empty: bool = True,
    ) -> None:
        """
        Generate and save parameter file with dynamic variables for auto-detect mode.

        Args:
            output_path: Path to save parameter file
            use_dynamic: Use dynamic variables in replace_value (default: True)
            environment: Environment name (default: "prod")
            skip_empty: Skip parameters without dynamic variables (default: True)
        """
        param_dict = self.generate_parameter_yml(
            use_dynamic=use_dynamic, environment=environment, skip_empty=skip_empty
        )

        # Hand-rolled YAML emit so we can interleave description comments
        # above each entry — yaml.dump can't.
        output_path = Path(output_path)
        with open(output_path, "w", encoding="utf-8") as f:
            _ = f.write("find_replace:\n")
            for entry in param_dict["find_replace"]:
                comment = entry.pop("_comment", None)
                if comment:
                    _ = f.write(f"  # {comment}\n")
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
            else:
                self.logger.info("    Dynamic variable: (skipped - requires manual setup)")
            if param.item_types:
                self.logger.info(
                    f"    Item Types: {', '.join(sorted(param.item_types))}"
                )
            self.logger.info(f"    Found in: {len(param.source_files)} file(s)")

        if without_dynamic > 0:
            self.logger.info(
                f"Note: {without_dynamic} parameter(s) will be skipped in auto-generated parameter.yml"
            )
            self.logger.info(
                "These typically require external setup (e.g., connection IDs) and don't change between deployments."
            )

    def count_connection_guids(self) -> int:
        "Returns the number of connection GUIDs detected"
        self._ensure_detected()

        connection_count = 0
        for param in self.parameters.values():
            description = param.description or ""
            if "connection" in description.lower() and not param.dynamic_variable:
                connection_count += 1
        return connection_count
