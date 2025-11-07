"""Auto-detection of source IDs from workspace repository for parameter generation."""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Set

import yaml


@dataclass
class DetectedParameter:
    """Represents a detected parameter with its context."""

    find_value: str
    item_types: Set[str] = field(default_factory=set)
    source_files: Set[str] = field(default_factory=set)
    description: str = ""
    dynamic_variable: str = ""  # Smart replace dynamic variable

    def to_dict(self, use_dynamic: bool = True, environment: str = "prod") -> dict:
        """
        Convert to parameter.yml entry format.

        Args:
            use_dynamic: Use dynamic variables in replace_value (default: True)
            environment: Environment name (default: "prod")

        Returns:
            Dictionary in parameter.yml format
        """
        entry = {
            "find_value": self.find_value,
            "item_type": sorted(list(self.item_types)),
        }

        # Dynamic variables must still be in dictionary format with environment keys
        if use_dynamic and self.dynamic_variable:
            entry["replace_value"] = {environment: self.dynamic_variable}
        else:
            # Empty value for manual entry
            entry["replace_value"] = {environment: ""}

        return entry


class ParameterDetector:
    """Detects source IDs from workspace repository files."""

    # Regex patterns for ID detection
    UUID_PATTERN = re.compile(
        r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
        re.IGNORECASE
    )

    # SQL Connection pattern: Sql.Database("HOST-SQLSTRING.datawarehouse...", "ENDPOINT-ID")
    SQL_DATABASE_PATTERN = re.compile(
        r'Sql\.Database\(\s*"([^"]+)-([A-Z0-9]+)\.datawarehouse\.fabric\.microsoft\.com"\s*,\s*"([0-9a-f-]+)"\s*\)',
        re.IGNORECASE
    )

    # Notebook metadata patterns
    WORKSPACE_ID_PATTERN = re.compile(
        r'"default_lakehouse_workspace_id":\s*"([0-9a-f-]+)"',
        re.IGNORECASE
    )
    LAKEHOUSE_ID_PATTERN = re.compile(
        r'"default_lakehouse":\s*"([0-9a-f-]+)"',
        re.IGNORECASE
    )

    def __init__(self, repo_dir: Path | str):
        """
        Initialize parameter detector.

        Args:
            repo_dir: Path to workspace repository directory
        """
        self.repo_dir = Path(repo_dir).resolve()
        self.parameters: dict[str, DetectedParameter] = {}
        self._lakehouse_names: list[str] = []

    def detect_all(self) -> dict[str, DetectedParameter]:
        """
        Detect all parameters from workspace files.

        Returns:
            Dictionary mapping find_value to DetectedParameter
        """
        self.parameters = {}

        # Detect lakehouse names first (needed for dynamic variables)
        self._detect_lakehouse_names()

        # Detect from notebooks
        self._detect_from_notebooks()

        # Detect from semantic models
        self._detect_from_semantic_models()

        # Detect from pipelines (connections, etc.)
        self._detect_from_pipelines()

        return self.parameters

    def _detect_lakehouse_names(self) -> None:
        """Detect lakehouse names from .Lakehouse/.platform files."""
        lakehouse_dirs = self.repo_dir.glob("*.Lakehouse")
        for lakehouse_dir in lakehouse_dirs:
            platform_file = lakehouse_dir / ".platform"
            if platform_file.exists():
                try:
                    metadata = json.loads(platform_file.read_text(encoding='utf-8'))
                    display_name = metadata.get("metadata", {}).get("displayName", "")
                    if display_name:
                        self._lakehouse_names.append(display_name)
                except Exception as e:
                    print(f"Warning: Failed to read lakehouse metadata from {platform_file}: {e}")

    def _add_parameter(
        self,
        find_value: str,
        item_type: str,
        source_file: str,
        description: str = ""
    ) -> None:
        """Add or update a detected parameter with dynamic variable generation."""
        if find_value not in self.parameters:
            # Generate dynamic variable based on description and context
            dynamic_var = self._generate_dynamic_variable(description)

            self.parameters[find_value] = DetectedParameter(
                find_value=find_value,
                description=description,
                dynamic_variable=dynamic_var
            )

        param = self.parameters[find_value]
        param.item_types.add(item_type)
        param.source_files.add(source_file)
        if description and not param.description:
            param.description = description

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

        # Lakehouse ID
        if "lakehouse id" in desc_lower and self._lakehouse_names:
            # Use first lakehouse name (most common case is single lakehouse)
            lakehouse_name = self._lakehouse_names[0]
            return f"$items.Lakehouse.{lakehouse_name}.$id"

        # SQL Analytics Endpoint Connection String
        if "connection substring" in desc_lower or "connection string" in desc_lower:
            if self._lakehouse_names:
                lakehouse_name = self._lakehouse_names[0]
                # Use $sqlendpoint attribute (fabric-cicd extracts connection string automatically)
                return f"$items.Lakehouse.{lakehouse_name}.$sqlendpoint"

        # SQL Analytics Endpoint Item ID
        if "endpoint item id" in desc_lower or "endpoint id" in desc_lower:
            if self._lakehouse_names:
                lakehouse_name = self._lakehouse_names[0]
                # This is the lakehouse's SQL endpoint ID, which is the same as lakehouse ID
                return f"$items.Lakehouse.{lakehouse_name}.$id"

        # External connection IDs - leave empty for manual entry
        if "connection id" in desc_lower:
            return ""

        return ""

    def _detect_from_notebooks(self) -> None:
        """Detect workspace and lakehouse IDs from notebook metadata."""
        notebook_files = self.repo_dir.glob("Notebooks/*.Notebook/notebook-content.py")

        for notebook_file in notebook_files:
            try:
                content = notebook_file.read_text(encoding='utf-8')

                # Extract workspace ID
                workspace_matches = self.WORKSPACE_ID_PATTERN.findall(content)
                for workspace_id in workspace_matches:
                    self._add_parameter(
                        find_value=workspace_id,
                        item_type="Notebook",
                        source_file=str(notebook_file.relative_to(self.repo_dir)),
                        description="Workspace ID"
                    )

                # Extract lakehouse ID
                lakehouse_matches = self.LAKEHOUSE_ID_PATTERN.findall(content)
                for lakehouse_id in lakehouse_matches:
                    self._add_parameter(
                        find_value=lakehouse_id,
                        item_type="Notebook",
                        source_file=str(notebook_file.relative_to(self.repo_dir)),
                        description="Lakehouse ID"
                    )

            except Exception as e:
                print(f"Warning: Failed to read {notebook_file}: {e}")

    def _detect_from_semantic_models(self) -> None:
        """Detect SQL connection strings and endpoint IDs from semantic models."""
        expression_files = self.repo_dir.glob(
            "Power BI/*.SemanticModel/definition/expressions.tmdl"
        )

        for expr_file in expression_files:
            try:
                content = expr_file.read_text(encoding='utf-8')

                # Extract SQL connection string and endpoint ID
                matches = self.SQL_DATABASE_PATTERN.findall(content)
                for host_prefix, sql_string, endpoint_id in matches:
                    # Add SQL connection string component
                    self._add_parameter(
                        find_value=sql_string,
                        item_type="SemanticModel",
                        source_file=str(expr_file.relative_to(self.repo_dir)),
                        description="Lakehouse SQL Analytics Endpoint Connection Substring"
                    )

                    # Add SQL endpoint item ID
                    self._add_parameter(
                        find_value=endpoint_id,
                        item_type="SemanticModel",
                        source_file=str(expr_file.relative_to(self.repo_dir)),
                        description="Lakehouse SQL Analytics Endpoint Item ID"
                    )

            except Exception as e:
                print(f"Warning: Failed to read {expr_file}: {e}")

    def _detect_from_pipelines(self) -> None:
        """Detect connection IDs and other references from pipeline JSON files."""
        pipeline_files = self.repo_dir.glob("Pipelines/*.DataPipeline/pipeline-content.json")

        for pipeline_file in pipeline_files:
            try:
                content = pipeline_file.read_text(encoding='utf-8')
                data = json.loads(content)

                # Look for external connection references
                self._extract_connections_recursive(
                    data,
                    str(pipeline_file.relative_to(self.repo_dir))
                )

            except Exception as e:
                print(f"Warning: Failed to read {pipeline_file}: {e}")

    def _extract_connections_recursive(self, obj, source_file: str) -> None:
        """Recursively extract connection IDs from nested JSON structures."""
        if isinstance(obj, dict):
            # Check for externalReferences.connection pattern
            if "externalReferences" in obj:
                ext_refs = obj["externalReferences"]
                if isinstance(ext_refs, dict) and "connection" in ext_refs:
                    connection_id = ext_refs["connection"]
                    if self.UUID_PATTERN.match(connection_id):
                        self._add_parameter(
                            find_value=connection_id,
                            item_type="DataPipeline",
                            source_file=source_file,
                            description="External connection ID"
                        )

            # Recurse into nested dictionaries
            for value in obj.values():
                self._extract_connections_recursive(value, source_file)

        elif isinstance(obj, list):
            # Recurse into lists
            for item in obj:
                self._extract_connections_recursive(item, source_file)

    def generate_parameter_yml(
        self,
        use_dynamic: bool = True,
        environment: str = "prod",
        include_descriptions: bool = True,
        skip_empty: bool = True
    ) -> dict:
        """
        Generate parameter.yml structure with detected parameters.

        Args:
            use_dynamic: Use dynamic variables in replace_value (default: True)
            environment: Environment name (default: "prod")
            include_descriptions: Include comments with descriptions
            skip_empty: Skip parameters without dynamic variables (default: True)

        Returns:
            Dictionary in parameter.yml format
        """
        if not self.parameters:
            self.detect_all()

        find_replace = []

        # Sort parameters by description for better organization
        sorted_params = sorted(
            self.parameters.values(),
            key=lambda p: (
                0 if "Workspace ID" in p.description else
                1 if "Lakehouse ID" in p.description else
                2 if "SQL" in p.description else
                3,
                p.description
            )
        )

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
        skip_empty: bool = True
    ) -> None:
        """
        Generate and save parameter.yml file.

        Args:
            output_path: Path to save parameter.yml
            use_dynamic: Use dynamic variables in replace_value (default: True)
            environment: Environment name (default: "prod")
            skip_empty: Skip parameters without dynamic variables (default: True)
        """
        param_dict = self.generate_parameter_yml(
            use_dynamic=use_dynamic,
            environment=environment,
            skip_empty=skip_empty
        )

        output_path = Path(output_path)
        with open(output_path, 'w', encoding='utf-8') as f:
            # Custom YAML dump with comments
            f.write("find_replace:\n")

            for entry in param_dict["find_replace"]:
                comment = entry.pop("_comment", None)
                if comment:
                    f.write(f"  # {comment}\n")

                # Write the entry
                f.write(f"  - find_value: \"{entry['find_value']}\"\n")

                # Replace value is always a dict with environment keys
                replace_value = entry['replace_value']
                f.write(f"    replace_value:\n")
                for env, val in replace_value.items():
                    f.write(f"      {env}: \"{val}\"\n")

                f.write(f"    item_type: {entry['item_type']}\n")

    def print_detection_summary(self) -> None:
        """Print a summary of detected parameters."""
        if not self.parameters:
            self.detect_all()

        # Count parameters with and without dynamic variables
        with_dynamic = sum(1 for p in self.parameters.values() if p.dynamic_variable)
        without_dynamic = len(self.parameters) - with_dynamic

        print(f"\nDetected {len(self.parameters)} parameters ({with_dynamic} with dynamic variables, {without_dynamic} require manual setup):\n")

        for param in self.parameters.values():
            print(f"  {param.description or 'Unknown'}")
            print(f"    Source value: {param.find_value}")
            if param.dynamic_variable:
                print(f"    Dynamic variable: {param.dynamic_variable}")
            else:
                print(f"    Dynamic variable: (skipped - requires manual setup)")
            print(f"    Item Types: {', '.join(sorted(param.item_types))}")
            print(f"    Found in: {len(param.source_files)} file(s)")
            print()

        if without_dynamic > 0:
            print(f"Note: {without_dynamic} parameter(s) will be skipped in auto-generated parameter.yml")
            print("These typically require external setup (e.g., connection IDs) and don't change between deployments.\n")
