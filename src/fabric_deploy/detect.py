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
    """Represents a detected parameter with its context."""

    find_value: str
    item_types: set[str] = field(default_factory=set)
    source_files: set[str] = field(default_factory=set)
    description: str = ""
    dynamic_variable: str = ""  # Smart replace dynamic variable
    metadata: dict = field(
        default_factory=dict
    )  # Additional context (e.g., domain_type)

    def to_dict(self, use_dynamic: bool = True, environment: str = "prod") -> dict:
        """
        Convert to parameter entry format for auto-detect mode.

        Args:
            use_dynamic: Use dynamic variables in replace_value (default: True)
            environment: Environment name (default: "prod")

        Returns:
            Dictionary in parameter entry format with dynamic variables
        """
        entry = {
            "find_value": self.find_value,
            "item_type": sorted(list(self.item_types)),
        }

        # Dynamic variables must still be in dictionary format with environment keys
        if use_dynamic and self.dynamic_variable:
            entry["replace_value"] = {environment: self.dynamic_variable}  # pyright: ignore[reportArgumentType]

            # Store domain metadata for post-processing
            if self.metadata.get("domain_type") == "database":
                entry["_domain_type"] = "database"  # Custom field for post-processing

        else:  # Empty value for manual entry
            entry["replace_value"] = {environment: ""}  # pyright: ignore[reportArgumentType]

        return entry


class ParameterDetector:
    """Detects source IDs from workspace repository files."""

    # Regex patterns for ID detection
    UUID_PATTERN: regex = re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
    )

    # SQL Connection pattern: Sql.Database("HOST-SQLSTRING.datawarehouse|database.DOMAIN", "DB-NAME" or "DB-NAME-UUID")
    # Captures: (1) host prefix, (2) SQL endpoint string, (3) domain type, (4) domain suffix, (5) db name, (6) optional UUID
    SQL_DATABASE_PATTERN: regex = re.compile(
        r'Sql\.Database\(\s*"([^"]+)-([A-Z0-9]+)\.(datawarehouse|database)\.(fabric\.microsoft\.com|pbidedicated\.windows\.net)"\s*,\s*"([^"]+?)(?:-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}))?"\s*\)',
        re.IGNORECASE,
    )

    # Notebook metadata patterns
    WORKSPACE_ID_PATTERN: regex = re.compile(
        r'"default_lakehouse_workspace_id":\s*"([0-9a-f-]+)"', re.IGNORECASE
    )
    LAKEHOUSE_ID_PATTERN: regex = re.compile(
        r'"default_lakehouse":\s*"([0-9a-f-]+)"', re.IGNORECASE
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

        # Detect item names first (needed for dynamic variables)
        self._detect_lakehouse_names()
        self._detect_sqldatabase_names()

        # Process all file types with their respective extractors
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

    def _detect_lakehouse_names(self) -> None:
        """Detect lakehouse names from .Lakehouse/.platform files."""
        lakehouse_dirs = self.repo_dir.glob("*.Lakehouse")
        for lakehouse_dir in lakehouse_dirs:
            platform_file = lakehouse_dir / ".platform"
            if not platform_file.exists():
                continue
            try:
                metadata = json.loads(platform_file.read_text(encoding="utf-8"))
                display_name = metadata.get("metadata", {}).get("displayName", "")
                if display_name:
                    self._lakehouse_names.append(display_name)
            except Exception as e:
                self._log_file_warning(platform_file, e, "lakehouse metadata")

    def _detect_sqldatabase_names(self) -> None:
        """Detect SQL Database names from .SQLDatabase/.platform files."""
        sqldatabase_dirs = self.repo_dir.glob("*.SQLDatabase")
        for sqldatabase_dir in sqldatabase_dirs:
            platform_file = sqldatabase_dir / ".platform"
            if not platform_file.exists():
                continue
            try:
                metadata = json.loads(platform_file.read_text(encoding="utf-8"))
                display_name = metadata.get("metadata", {}).get("displayName", "")
                if display_name:
                    self._sqldatabase_names.append(display_name)
            except Exception as e:
                self._log_file_warning(platform_file, e, "SQL Database metadata")

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
        self, find_value: str, item_type: str, source_file: str, description: str = ""
    ) -> DetectedParameter:
        """Add or update a detected parameter with dynamic variable generation."""
        if find_value not in self.parameters:
            # Generate dynamic variable based on description and context
            dynamic_var = self._generate_dynamic_variable(description)

            self.parameters[find_value] = DetectedParameter(
                find_value=find_value,
                description=description,
                dynamic_variable=dynamic_var,
            )

        param = self.parameters[find_value]
        param.item_types.add(item_type)
        param.source_files.add(source_file)
        if description and not param.description:
            param.description = description

        return param

    def _add_uuid_parameter(
        self, value: str, item_type: str, source_file: str, description: str = ""
    ):
        """
        Add parameter if value is a valid UUID.

        Args:
            value: Value to validate and add
            item_type: Type of item where ID was found
            source_file: Relative path to source file
            description: Human-readable description
        """
        if value and self.UUID_PATTERN.match(value):
            _ = self._add_parameter(
                find_value=value,
                item_type=item_type,
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

        # Workspace ID
        if "workspace id" in desc_lower:
            return "$workspace.$id"

        if self._lakehouse_names:
            # Use first lakehouse name (most common case is single lakehouse)
            lakehouse_name = self._lakehouse_names[0]
            # Lakehouse ID
            if "lakehouse id" in desc_lower:
                return f"$items.Lakehouse.{lakehouse_name}.$id"

            # SQL Analytics Endpoint Connection String
            if "sql analytics endpoint connection string" in desc_lower:
                # Use $sqlendpoint attribute (returns full .datawarehouse hostname)
                return f"$items.Lakehouse.{lakehouse_name}.$sqlendpoint"

            # Lakehouse SQL Analytics Endpoint Item ID (distinct from Lakehouse ID)
            if "lakehouse sql analytics endpoint item id" in desc_lower:
                return f"$items.Lakehouse.{lakehouse_name}.$sqlendpointid"

        if self._sqldatabase_names:
            # Use first SQL Database name (most common case is single database)
            sqldatabase_name = self._sqldatabase_names[0]

            # SQL Database Connection String
            # Attribute exists but value may not be available until endpoint is provisioned
            # Requires manual configuration in fabdep.yml for now
            if "sql database connection string" in desc_lower:
                return ""  # External - endpoint may not be provisioned

            # SQL Database Item ID
            if "sql database item id" in desc_lower:
                return f"$items.SQLDatabase.{sqldatabase_name}.$id"

            # SQL Database Name (full name with UUID suffix)
            # Not supported - requires manual configuration (no $name attribute)
            if "sql database name" in desc_lower:
                return ""  # External - mark for manual entry

        # External connection IDs - leave empty for manual entry
        if "connection id" in desc_lower:
            return ""

        return ""

    def _extract_from_notebook(self, file_path: Path, content: str) -> None:
        """Extract workspace and lakehouse IDs from notebook metadata."""
        source_file = str(file_path.relative_to(self.repo_dir))

        # Extract workspace ID
        for workspace_id in self.WORKSPACE_ID_PATTERN.findall(content):
            _ = self._add_parameter(
                workspace_id, "Notebook", source_file, "Workspace ID"
            )

        # Extract lakehouse ID
        for lakehouse_id in self.LAKEHOUSE_ID_PATTERN.findall(content):
            _ = self._add_parameter(
                lakehouse_id, "Notebook", source_file, "Lakehouse ID"
            )

    def _extract_from_semantic_model(self, file_path: Path, content: str) -> None:
        """Extract SQL connection strings and endpoint IDs from semantic models."""
        source_file = str(file_path.relative_to(self.repo_dir))

        # Extract SQL connection string and endpoint ID
        # Captures: (1) host prefix, (2) SQL endpoint string, (3) domain type, (4) domain suffix, (5) db name, (6) optional UUID
        for (
            host_prefix,
            sql_string,
            domain_type,
            domain_suffix,
            db_name,
            uuid,
        ) in self.SQL_DATABASE_PATTERN.findall(content):
            # Store the full hostname with domain for replacement
            full_hostname = f"{host_prefix}-{sql_string}.{domain_type}.{domain_suffix}"
            is_sql_database = domain_type.lower() == "database"

            # Add hostname parameter with domain metadata
            hostname_description = (
                "SQL Database Connection String"
                if is_sql_database
                else "SQL Analytics Endpoint Connection String"
            )
            hostname_param = self._add_parameter(
                full_hostname,
                "SemanticModel",
                source_file,
                hostname_description,
            )

            # Store domain type, domain suffix, and database name in metadata for later processing
            if hostname_param:
                hostname_param.metadata["domain_type"] = domain_type.lower()
                hostname_param.metadata["domain_suffix"] = domain_suffix.lower()
                hostname_param.metadata["host_prefix"] = host_prefix
                hostname_param.metadata["db_name"] = db_name  # Always store db_name

            # Add UUID parameter if present (either as explicit suffix or as the db_name itself)
            uuid_to_add = uuid  # Check explicit UUID suffix first
            if not uuid_to_add and db_name and self.UUID_PATTERN.fullmatch(db_name):
                # db_name is itself a UUID (no prefix)
                uuid_to_add = db_name

            if uuid_to_add:
                uuid_description = (
                    "SQL Database Item ID"
                    if is_sql_database
                    else "Lakehouse SQL Analytics Endpoint Item ID"
                )
                _ = self._add_parameter(
                    uuid_to_add,
                    "SemanticModel",
                    source_file,
                    uuid_description,
                )

            # Add full database name parameter if it includes a UUID
            # This is needed for SQL Database connections where the database name is "{displayName}-{itemId}"
            if uuid and db_name:
                full_db_name = f"{db_name}-{uuid}"
                _ = self._add_parameter(
                    full_db_name,
                    "SemanticModel",
                    source_file,
                    "SQL Database Name",
                )

    def _extract_from_pipeline(self, file_path: Path, data: dict) -> None:
        """Extract connection IDs from pipeline JSON."""
        source_file = str(file_path.relative_to(self.repo_dir))
        self._extract_connections_recursive(data, source_file)

    def _extract_connections_recursive(self, obj, source_file: str) -> None:
        """Recursively extract connection IDs from nested JSON structures."""
        if isinstance(obj, dict):
            # Check for externalReferences.connection pattern
            if "externalReferences" in obj:
                ext_refs = obj["externalReferences"]
                if isinstance(ext_refs, dict) and "connection" in ext_refs:
                    self._add_uuid_parameter(
                        value=ext_refs["connection"],
                        item_type="DataPipeline",
                        source_file=source_file,
                        description="External connection ID",
                    )

            # Recurse into nested dictionaries
            for value in obj.values():
                self._extract_connections_recursive(value, source_file)

        elif isinstance(obj, list):
            # Recurse into lists
            for item in obj:
                self._extract_connections_recursive(item, source_file)

    def _extract_from_copyjob(self, file_path: Path, data: dict) -> None:
        """Extract workspace and lakehouse IDs from CopyJob JSON."""
        source_file = str(file_path.relative_to(self.repo_dir))

        # Extract workspace and lakehouse IDs from source and destination
        for section_name in ["source", "destination"]:
            section = data.get("properties", {}).get(section_name, {})
            type_properties = section.get("connectionSettings", {}).get(
                "typeProperties", {}
            )

            # Extract workspace ID and lakehouse ID (artifactId)
            self._add_uuid_parameter(
                type_properties.get("workspaceId"),
                "CopyJob",
                source_file,
                "Workspace ID",
            )
            self._add_uuid_parameter(
                type_properties.get("artifactId"),
                "CopyJob",
                source_file,
                "Lakehouse ID",
            )

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
        if not self.parameters:
            self.detect_all()

        find_replace = []

        # Sort parameters by description for better organization
        def _get_sort_key(param: DetectedParameter) -> tuple[int, str]:
            """Return sort key tuple for parameter ordering."""
            if "Workspace ID" in param.description:
                priority = 0
            elif "Lakehouse ID" in param.description:
                priority = 1
            elif "SQL" in param.description:
                priority = 2
            else:
                priority = 3
            return (priority, param.description)

        sorted_params = sorted(self.parameters.values(), key=_get_sort_key)

        # Add parameters with dynamic variables
        for param in sorted_params:
            # Skip parameters without dynamic variables if requested
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

        output_path = Path(output_path)
        with open(output_path, "w", encoding="utf-8") as f:
            # Custom YAML dump with comments
            _ = f.write("find_replace:\n")

            for entry in param_dict["find_replace"]:
                comment = entry.pop("_comment", None)
                if comment:
                    _ = f.write(f"  # {comment}\n")

                # Write the entry
                _ = f.write(f'  - find_value: "{entry["find_value"]}"\n')

                # Replace value is always a dict with environment keys
                replace_value = entry["replace_value"]
                _ = f.write("    replace_value:\n")
                for env, val in replace_value.items():
                    _ = f.write(f'      {env}: "{val}"\n')

                _ = f.write(f"    item_type: {entry['item_type']}\n")

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
        if not self.parameters:
            self.detect_all()

        # Find all detected workspace IDs
        detected_workspace_ids = [
            param.find_value
            for param in self.parameters.values()
            if param.description == "Workspace ID"
        ]

        # If no workspace IDs detected, don't skip replacement
        if not detected_workspace_ids:
            return False

        # Only skip if ALL detected workspace IDs match the target
        return all(wid == target_workspace_id for wid in detected_workspace_ids)

    def print_detection_summary(self) -> None:
        """Print a summary of detected parameters."""
        if not self.parameters:
            self.detect_all()

        # Count parameters with and without dynamic variables
        with_dynamic = sum(1 for p in self.parameters.values() if p.dynamic_variable)
        without_dynamic = len(self.parameters) - with_dynamic

        print(
            f"\nDetected {len(self.parameters)} parameters ({with_dynamic} with dynamic variables, {without_dynamic} require manual setup):\n"
        )

        for param in self.parameters.values():
            print(f"  {param.description or 'Unknown'}")
            print(f"    Source value: {param.find_value}")
            if param.dynamic_variable:
                print(f"    Dynamic variable: {param.dynamic_variable}")
            else:
                print("    Dynamic variable: (skipped - requires manual setup)")
            print(f"    Item Types: {', '.join(sorted(param.item_types))}")
            print(f"    Found in: {len(param.source_files)} file(s)")
            print()

        if without_dynamic > 0:
            print(
                f"Note: {without_dynamic} parameter(s) will be skipped in auto-generated parameter.yml"
            )
            print(
                "These typically require external setup (e.g., connection IDs) and don't change between deployments.\n"
            )

    def count_connection_guids(self) -> int:
        "Returns the number of connection GUIDs detected"
        if not self.parameters:
            self.detect_all()

        connection_count = 0
        for param in self.parameters.values():
            description = param.description or ""
            if "connection" in description.lower() and not param.dynamic_variable:
                connection_count += 1
        return connection_count
