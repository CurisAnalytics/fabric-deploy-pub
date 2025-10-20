import argparse
import yaml
from json import loads
from pathlib import Path

from fabric_cicd import (
    FabricWorkspace,
    publish_all_items,
    unpublish_all_orphan_items,
    append_feature_flag,
)
from fabric_cicd.constants import ACCEPTED_ITEM_TYPES  # CopyJob, Noteook, Report, &c

SEP = "=" * 60


def extract_config_from_parameters(param_file: Path) -> tuple[str, str]:
    """
    Extract workspace ID and environment from parameter.yml
    Reads the first entry's replace_value to get destination workspace ID
    Environment is determined by the keys present (prod, dev, env)
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
        raise ValueError(f"No workspace ID specified for environment '{environment}'")
    return workspace_id, environment


def detect_lakehouse(repo_dir: Path) -> str:
    """Detect lakehouse name from .Lakehouse directory."""
    lakehouses = list(repo_dir.glob("*.Lakehouse"))
    if len(lakehouses) == 0:
        raise ValueError("No lakehouse found in repository directory")
    if len(lakehouses) > 1:
        print(f"Warning: Multiple lakehouses found: {[d.name for d in lakehouses]}")
    try:
        platform_file = lakehouses[0] / ".platform"
        metadata = loads(platform_file.read_text())
        lakehouse_name: str = metadata["metadata"]["displayName"]
    except Exception as e:
        raise RuntimeError(f"Failed to read lakehouse metadata: {e}") from e
    return lakehouse_name


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
    args = parse_args()

    repo_dir = args.directory
    no_cleanup = args.no_cleanup
    item_types = args.item_types
    exclude_types = args.exclude_types
    include_items = args.include_items

    # Enable experimental feature flags if name-based filtering is requested
    if include_items:
        append_feature_flag("enable_experimental_features")
        append_feature_flag("enable_items_to_include")
        print(
            "\n⚠️  Warning: Name-based item filtering is experimental.\n"
            "Selective deployment may cause dependency issues. Use at your own risk.\n"
        )

    # Ensure we're in a valid workspace directory
    repo_dir = repo_dir.resolve()
    if not repo_dir.exists():
        raise FileNotFoundError(f"Directory does not exist: {repo_dir}")

    # Find parameter.yml
    param_file = repo_dir / "parameter.yml"
    if not param_file.exists():
        raise FileNotFoundError(
            f"parameter.yml not found in {repo_dir}. "
            "Make sure you're running this from a Fabric workspace directory"
        )

    workspace_id, _ = extract_config_from_parameters(param_file)

    lakehouse_name: str = detect_lakehouse(repo_dir)

    # Build item_type_in_scope based on include/exclude flags
    item_type_in_scope = None
    if item_types:
        item_type_in_scope = item_types
    elif exclude_types:
        # All supported types minus excluded ones
        item_type_in_scope = [t for t in ACCEPTED_ITEM_TYPES if t not in exclude_types]

    print(f"\n{SEP}\nFabric CI/CD Deployment\n{SEP}")
    print(f"Detected lakehouse name: {lakehouse_name}")
    print(f"Workspace ID: {workspace_id}")
    if item_type_in_scope:
        print(f"Item types in scope: {', '.join(item_type_in_scope)}")
    else:
        print("Item types in scope: All supported types")
    if include_items:
        print(f"Including items: {', '.join(include_items)}")
    print(SEP + "\n")

    target_workspace = FabricWorkspace(
        workspace_id=workspace_id,
        repository_directory=str(repo_dir),
        item_type_in_scope=item_type_in_scope,
    )

    _ = publish_all_items(target_workspace, items_to_include=include_items)

    if not no_cleanup:
        unpublish_all_orphan_items(
            target_workspace, item_name_exclude_regex=lakehouse_name
        )
    print("\nDeployment complete!")


if __name__ == "__main__":
    main()
