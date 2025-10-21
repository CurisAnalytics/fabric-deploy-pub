import argparse
import yaml
from json import loads
from pathlib import Path
from typing import final

from fabric_cicd import (
    FabricWorkspace,
    publish_all_items,
    unpublish_all_orphan_items,
    append_feature_flag,
)
from fabric_cicd.constants import ACCEPTED_ITEM_TYPES  # CopyJob, Noteook, Report, &c

from .logging import get_logger, print_header


@final
class FabricDeployer:
    """
    Main deployer class for Fabric workspace items.

    Encapsulates configuration extraction, validation, and deployment logic
    for programmatic usage with proper logging namespace.
    """

    def __init__(
        self,
        repo_dir: Path | str,
        logger_name: str | None = None,
        workspace_id: str | None = None,
        lakehouse_name: str | None = None,
    ):
        self.repo_dir = Path(repo_dir).resolve()
        self.workspace_id = workspace_id
        self.lakehouse_name = lakehouse_name
        self.logger = get_logger(logger_name or __name__)
        self.workspace_id = workspace_id if workspace_id else self._load_config()
        self.lakehouse_name = (
            lakehouse_name if lakehouse_name else self._detect_lakehouse()
        )

    def _load_config(self) -> str:
        """Load and validate workspace configuration from parameter.yml."""
        if not self.repo_dir.exists():
            raise FileNotFoundError(f"Directory does not exist: {self.repo_dir}")
        param_file = self.repo_dir / "parameter.yml"
        if not param_file.exists():
            raise FileNotFoundError(
                f"parameter.yml not found in {self.repo_dir}. "
                "Make sure you're running this from a Fabric workspace directory"
            )
        workspace_id, _ = self._extract_config_from_parameters(param_file)
        return workspace_id

    def _extract_config_from_parameters(self, param_file: Path) -> tuple[str, str]:
        """
        Extract workspace ID and environment from parameter.yml.
        Reads the first entry's replace_value to get destination workspace ID.
        Environment is determined by the keys present (prod, dev, env).
        Args: param_file: Path to parameter.yml
        Returns: Tuple of (workspace_id, environment)
        """
        with open(param_file) as f:
            params = yaml.safe_load(f)
        first_entry = params.get("find_replace", [])[0]
        replace_values = first_entry.get("replace_value", {})
        # Get available environments from the replace_value keys
        available_envs = list(replace_values.keys())
        if not available_envs:
            raise ValueError("No environments found in parameter.yml")
        # Assert that all available environments are the same
        if len(set(available_envs)) > 1:
            raise ValueError(
                f"Multiple different environments found in parameter.yml: {available_envs}"
            )
        environment = available_envs[0]
        # Get the destination workspace ID for this environment
        workspace_id = replace_values[environment]
        if not workspace_id:
            raise ValueError(
                f"No workspace ID specified for environment '{environment}'"
            )
        return workspace_id, environment

    def _detect_lakehouse(self) -> str:
        """
        Detect lakehouse name from .Lakehouse directory.

        Returns:
            Lakehouse display name from metadata
        """
        lakehouses = list(self.repo_dir.glob("*.Lakehouse"))
        if len(lakehouses) == 0:
            raise ValueError("No lakehouse found in repository directory")
        elif len(lakehouses) > 1:
            self.logger.warning(
                f"Multiple lakehouses found: {[d.name for d in lakehouses]}"
            )
        try:
            platform_file = lakehouses[0] / ".platform"
            metadata = loads(platform_file.read_text())
            lakehouse_name: str = metadata["metadata"]["displayName"]
        except Exception as e:
            raise RuntimeError(f"Failed to read lakehouse metadata: {e}") from e
        return lakehouse_name

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
        # Enable experimental feature flags if name-based filtering is requested
        if include_items:
            append_feature_flag("enable_experimental_features")
            append_feature_flag("enable_items_to_include")
            self.logger.warning(
                "Name-based item filtering is experimental. "
                "Selective deployment may cause dependency issues. Use at your own risk."
            )

        # Build item_type_in_scope based on include/exclude flags
        item_type_in_scope = None
        if item_types:
            item_type_in_scope = item_types
        elif exclude_types:
            # All supported types minus excluded ones
            item_type_in_scope = [
                t for t in ACCEPTED_ITEM_TYPES if t not in exclude_types
            ]

        print_header("Contoso  𐧾  Fabric CI/CD")  # \udb85\uddfc
        self.logger.info(f"Detected lakehouse name: {self.lakehouse_name}")
        self.logger.info(f"Workspace ID: {self.workspace_id}")
        if item_type_in_scope:
            self.logger.info(f"Item types in scope: {', '.join(item_type_in_scope)}")
        else:
            self.logger.info("Item types in scope: All supported types")
        if include_items:
            self.logger.info(f"Including items: {', '.join(include_items)}")

        target_workspace = FabricWorkspace(
            workspace_id=self.workspace_id,
            repository_directory=str(self.repo_dir),
            item_type_in_scope=item_type_in_scope,
        )

        _ = publish_all_items(target_workspace, items_to_include=include_items)

        if not no_cleanup:
            assert self.lakehouse_name is not None
            unpublish_all_orphan_items(
                target_workspace, item_name_exclude_regex=self.lakehouse_name
            )

        self.logger.info("\nDeployment complete!")


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="fabric-cicd",
        description="Deploy Fabric workspace items - auto-detects configuration from parameter.yml",
        epilog="Run from your Fabric workspace directory.",
    )
    _ = parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        type=Path,
        help="Workspace directory (defaults to current directory)",
    )
    _ = parser.add_argument(
        "--no-cleanup",
        "-n",
        action="store_true",
        help="Skip unpublishing orphaned items",
    )
    _ = parser.add_argument(
        "--item-types",
        "-t",
        nargs="+",
        help="Specific item types to deploy (e.g., Notebook DataPipeline Environment)",
    )
    _ = parser.add_argument(
        "--exclude-types",
        "-x",
        nargs="+",
        help="Item types to exclude from deployment (e.g., Report SemanticModel)",
    )
    _ = parser.add_argument(
        "--include-items",
        "-i",
        nargs="+",
        help='Include only specific items (format: "ItemName.ItemType", e.g., "MyNotebook.Notebook")',
    )
    return parser.parse_args()


def main():
    """CLI entry point."""
    args = parse_args()

    deployer = FabricDeployer(
        repo_dir=args.directory, logger_name="fabric_deploy.cli"
    )

    deployer.deploy(
        no_cleanup=args.no_cleanup,
        item_types=args.item_types,
        exclude_types=args.exclude_types,
        include_items=args.include_items,
    )


if __name__ == "__main__":
    main()
