import argparse
import yaml
from json import loads
from pathlib import Path

from fabric_cicd import FabricWorkspace, publish_all_items, unpublish_all_orphan_items

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


def main(args: argparse.Namespace):
    repo_dir = args.directory
    no_cleanup = args.no_cleanup

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

    workspace_id, environment = extract_config_from_parameters(param_file)

    lakehouse_name: str = detect_lakehouse(repo_dir)
    print(f"Detected lakehouse: {lakehouse_name}")

    target_workspace = FabricWorkspace(
        workspace_id=workspace_id,
        repository_directory=str(repo_dir),
        environment=environment,
    )

    print(f"\n{SEP}\nFabric CI/CD Deployment\n{SEP}")
    print(f"Workspace ID: {workspace_id}")
    print(SEP + "\n")
    _ = publish_all_items(target_workspace)
    if not no_cleanup:
        unpublish_all_orphan_items(target_workspace, lakehouse_name)
    print(f"{SEP}\nDeployment complete!\n{SEP}")


if __name__ == "__main__":
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
        action="store_true",
        help="Skip unpublishing orphaned items",
    )

    args = parser.parse_args()
    main(args)
