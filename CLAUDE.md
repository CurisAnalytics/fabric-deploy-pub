# CLAUDE.md

## Project Overview

`fabric-deploy` is a **thin CLI wrapper** around Microsoft's official [`fabric-cicd`](https://github.com/microsoft/fabric-cicd) package. It auto-detects workspace configuration from `parameter.yml` files, eliminating per-workspace `cicd.py` scripts.

## Purpose

- **Wrapper**: Thin CLI layer over Microsoft's `fabric-cicd` package
- **Auto-detection**: Reads `parameter.yml` to extract workspace ID and environment
- **CLI convenience**: Replace inline Python scripts with a simple command
- **No API implementation**: All Fabric REST API interaction is handled by Microsoft's package

## Architecture

```
fabric-deploy/
├── src/fabric_deploy/     # Package directory
│   ├── __init__.py            # Package initialization
│   └── cli.py                 # CLI entry point with parse_args() and main()
├── pyproject.toml             # Package configuration, depends on fabric-cicd
├── example_parameter.yml      # Example configuration file
└── README.md                  # Brief usage documentation
```

**Key Point**: This package wraps `fabric-cicd` from PyPI, it does not reimplement any functionality.

## Usage Pattern

**Old way** (inline cicd.py in workspace repo):
```python
from fabric_cicd import FabricWorkspace, publish_all_items, unpublish_all_orphan_items

workspace_id = "bfefe424-11b0-4919-b395-0ad9692d4123"
target_workspace = FabricWorkspace(
    workspace_id=workspace_id,
    repository_directory=str(Path(__file__).resolve().parent),
    environment="env",
)
publish_all_items(target_workspace)
unpublish_all_orphan_items(target_workspace, lakehouse_name)
```

**New way** (CLI wrapper):
```bash
pip install fabric-deploy  # Installs Microsoft's fabric-cicd as dependency
cd path/to/workspace
fabric-cicd  # Auto-detects workspace_id and environment from parameter.yml
```

## CLI Flow

1. `parse_args()` parses command-line arguments
2. `main()` reads `parameter.yml` to extract:
   - **Workspace ID**: From first entry's `replace_value[environment]`
   - **Environment**: Auto-detected from keys in `replace_value` (must be consistent across entries)
3. Auto-detects lakehouse name from `*.Lakehouse/.platform` metadata file
4. Applies item type filtering based on CLI flags (`--item-types`, `--exclude-types`, `--include-items`)
5. Creates `FabricWorkspace` instance (from Microsoft's package)
6. Calls `publish_all_items()` with optional item filtering (from Microsoft's package)
7. Optionally calls `unpublish_all_orphan_items()` (from Microsoft's package) unless `--no-cleanup` is specified

## What This Package Does

### ✅ Provides
- CLI interface (`fabric-cicd` command)
- Auto-detection from `parameter.yml`
- Lakehouse name detection from `.platform` files
- User-friendly output formatting
- Command-line argument parsing (`--env`, `--dry-run`, etc.)

### ❌ Does NOT Provide
- Fabric REST API implementation (handled by Microsoft's `fabric-cicd`)
- `FabricWorkspace` class (imported from Microsoft's package)
- `publish_all_items()` function (imported from Microsoft's package)
- `unpublish_all_orphan_items()` function (imported from Microsoft's package)
- Authentication logic (handled by Microsoft's package)

## Dependencies

```toml
[project]
dependencies = [
    "fabric-cicd>=0.1.29",  # Microsoft's official package
    "pyyaml",               # For parameter.yml parsing (if not in fabric-cicd)
]
```

## Configuration

**parameter.yml** (in workspace repo):
```yaml
find_replace:
  # First entry's replace_value is used to detect workspace ID and environment
  - find_value: "364abdbe-c061-4f74-b34a-9631bc7e3e4d"
    replace_value:
      prod: "6c5304f1-2e84-4add-bd46-25a1e943cdec"  # CLI uses this as workspace_id
    item_type: ["Notebook", "CopyJob"]

  # Additional find/replace entries for parameter replacement
  - find_value: "dev-lakehouse-id"
    replace_value:
      prod: "prod-lakehouse-id"  # All entries must use the same environment key
    item_type: ["Notebook", "SemanticModel"]
```

**Important**: All `replace_value` entries must use the same environment key (e.g., all use `prod` or all use `dev`). The CLI validates this consistency and extracts the destination `workspace_id` from the first entry.

## Implementation Details

### Current Implementation
The CLI correctly imports from `fabric-cicd`:

```python
# cli.py
from fabric_cicd import FabricWorkspace, publish_all_items, unpublish_all_orphan_items

def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(...)
    # Configure argument parser
    return parser.parse_args()

def main():
    args = parse_args()
    
    # Extract workspace_id and environment from parameter.yml
    workspace_id, environment = extract_config_from_parameters(param_file)
    
    # Detect lakehouse name from .platform metadata
    lakehouse_name = detect_lakehouse(repo_dir)
    
    # Apply item type filtering from CLI args
    item_type_in_scope = None
    if args.item_types:
        item_type_in_scope = args.item_types
    elif args.exclude_types:
        item_type_in_scope = [t for t in ACCEPTED_ITEM_TYPES if t not in args.exclude_types]
    
    # Create workspace using Microsoft's class
    target_workspace = FabricWorkspace(
        workspace_id=workspace_id,
        repository_directory=str(repo_dir),
        item_type_in_scope=item_type_in_scope,
    )
    
    # Call Microsoft's deployment functions
    publish_all_items(target_workspace, items_to_include=args.include_items)
    if not args.no_cleanup:
        unpublish_all_orphan_items(target_workspace, item_name_exclude_regex=lakehouse_name)
```

### Key Functions

**`parse_args() -> argparse.Namespace`**
- Configures argument parser with all CLI options
- Returns parsed arguments including `directory`, `no_cleanup`, `item_types`, `exclude_types`, `include_items`
- Separated from `main()` for testability

**`extract_config_from_parameters(param_file: Path) -> tuple[str, str]`**
- Reads `parameter.yml` and extracts workspace ID and environment
- Validates that all `replace_value` entries use the same environment key
- Returns tuple of `(workspace_id, environment)`

**`detect_lakehouse(repo_dir: Path) -> str`**
- Searches for `*.Lakehouse` directories
- Reads `.platform` metadata file to extract `displayName`
- Returns lakehouse name for orphan cleanup exclusions

**`main() -> None`**
- Orchestrates the entire deployment workflow
- Calls `parse_args()`, validates inputs, creates `FabricWorkspace`, and deploys items

## Microsoft's fabric-cicd Package

**Documentation**: https://microsoft.github.io/fabric-cicd/

**Key Classes/Functions**:
- `FabricWorkspace(workspace_id, repository_directory, environment, item_type_in_scope)` - Main configuration class
- `publish_all_items(workspace)` - Deploys items to Fabric
- `unpublish_all_orphan_items(workspace)` - Cleans up orphaned items

**Supported Item Types** (handled by Microsoft's package):
- DataPipeline, Environment, Notebook, Report, SemanticModel
- Lakehouse, MirroredDatabase, VariableLibrary, CopyJob
- Eventhouse, KQLDatabase, KQLQueryset, Reflex, Eventstream
- Warehouse, SQLDatabase, KQLDashboard, Dataflow, GraphQLApi
- ApacheAirflowJob, MountedDataFactory

## CLI Arguments

```
fabric-cicd [DIRECTORY] [OPTIONS]

Arguments:
  DIRECTORY              Path to workspace directory (default: current directory)

Options:
  --no-cleanup, -n       Skip unpublishing orphaned items
  --item-types, -t       Deploy specific types (e.g., Notebook DataPipeline)
  --exclude-types, -x    Exclude types (e.g., Report SemanticModel)
  --include-items, -i    Include specific items by name (experimental)
  --help                 Show help message
```

**Example usage**:
```bash
# Deploy from current directory
fabric-cicd

# Deploy from specific directory
fabric-cicd /path/to/workspace

# Deploy without cleaning up orphaned items
fabric-cicd --no-cleanup

# Deploy only notebooks and pipelines
fabric-cicd --item-types Notebook DataPipeline

# Exclude reports and semantic models
fabric-cicd --exclude-types Report SemanticModel

# Deploy specific items by name (experimental)
fabric-cicd --include-items "MyNotebook.Notebook" "MyPipeline.DataPipeline"
```

## Error Handling

The CLI validates:
- Workspace directory exists
- `parameter.yml` exists in the workspace directory
- At least one `find_replace` entry exists
- All `replace_value` entries use the same environment key
- At least one `*.Lakehouse` directory exists
- Lakehouse `.platform` metadata file is readable

## Development

### Building the Package

```bash
# Install dependencies (requires uv)
uv sync

# Build distribution
uv build

# Install locally
pip install -e .
```

### Testing

Test with an example workspace:
```bash
cd /path/to/workspace
fabric-cicd --no-cleanup  # Preview without orphan cleanup
```

## Related Projects

- **[microsoft/fabric-cicd](https://github.com/microsoft/fabric-cicd)**: The underlying library this wraps
- **Workspaces/**: Client workspace repos that will use this CLI tool
- **example-fabric-etl**: Core Python ETL framework (separate concern)

## Troubleshooting

**"parameter.yml not found"**
- Ensure you're running the command from a Fabric workspace directory
- Check that `parameter.yml` exists in the workspace root

**"No environments found in parameter.yml"**
- Verify `find_replace` entries have `replace_value` dictionaries
- Check that at least one environment key exists (e.g., `prod`, `dev`, `env`)

**"Multiple different environments found"**
- All `replace_value` entries must use the same environment key
- Fix: Update all entries to use consistent environment naming

**"No lakehouse found in repository directory"**
- Ensure at least one `*.Lakehouse` directory exists
- Check that the directory contains a `.platform` metadata file