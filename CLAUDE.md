# CLAUDE.md

## Project Overview

`fabric-deploy` is a **thin CLI wrapper** around Microsoft's official [`fabric-cicd`](https://github.com/microsoft/fabric-cicd) package. It simplifies deployment by auto-detecting configuration from `parameter.yml` files, eliminating the need for inline `cicd.py` scripts in each workspace repository.

## Purpose

- **Wrapper**: Thin CLI layer over Microsoft's `fabric-cicd` package
- **Auto-detection**: Reads `parameter.yml` to extract workspace ID and environment
- **CLI convenience**: Replace inline Python scripts with a simple command
- **No API implementation**: All Fabric REST API interaction is handled by Microsoft's package

## Architecture

```
fabric-deploy/
├── src/fabric_cicd/           # Note: Different package name, same import path
│   ├── __init__.py            # Re-exports from fabric-cicd (optional)
│   ├── cli.py                 # CLI entry point with auto-detection
│   └── parameters.py          # YAML parameter handling (if needed)
└── pyproject.toml             # Depends on fabric-cicd from pip
```

**Key Point**: This package wraps `fabric-cicd` from PyPI, not reimplements it.

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
cd Workspaces/MyClient
fabric-cicd  # Auto-detects workspace_id and environment from parameter.yml
```

## CLI Flow

1. User runs `fabric-cicd` from workspace directory
2. CLI reads `parameter.yml` to extract:
   - **Workspace ID**: From first entry's `replace_value[environment]`
   - **Environment**: From keys in `replace_value` (defaults to "env")
3. Auto-detects lakehouse from `*.Lakehouse/.platform` (if present)
4. Creates `FabricWorkspace` instance (from Microsoft's package)
5. Calls `publish_all_items()` (from Microsoft's package)
6. Calls `unpublish_all_orphan_items()` (from Microsoft's package)

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
  # First entry's replace_value is used to detect workspace ID
  - find_value: "364abdbe-c061-4f74-b34a-9631bc7e3e4d"
    replace_value:
      prod: "6c5304f1-2e84-4add-bd46-25a1e943cdec"  # CLI uses this as workspace_id
      dev: "abc123..."
      env: "def456..."
    item_type: ["Notebook", "CopyJob"]
  
  # Additional find/replace entries for parameter replacement
  - find_value: "dev-lakehouse-id"
    replace_value:
      prod: "prod-lakehouse-id"
      dev: "dev-lakehouse-id"
    item_type: ["Notebook", "SemanticModel"]
```

The CLI extracts the destination `workspace_id` from the first entry's `replace_value` based on the target environment.

## Implementation Notes

### Current State (INCORRECT)
The codebase currently **reimplements** `FabricWorkspace`, `publish_all_items()`, and `unpublish_all_orphan_items()` with stub implementations. This is wrong.

### Correct Implementation
Should **import** these from `fabric-cicd`:

```python
# cli.py
from fabric_cicd import FabricWorkspace, publish_all_items, unpublish_all_orphan_items

# Extract workspace_id and environment from parameter.yml
workspace_id, environment = extract_config_from_parameters(param_file)

# Create workspace using Microsoft's class
target_workspace = FabricWorkspace(
    workspace_id=workspace_id,
    repository_directory=str(repo_dir),
    environment=environment,
)

# Call Microsoft's deployment functions
publish_all_items(target_workspace)
unpublish_all_orphan_items(target_workspace, lakehouse_name)
```

### Files That Should Be Removed
- `src/fabric_cicd/workspace.py` - Use Microsoft's `FabricWorkspace` instead
- `src/fabric_cicd/deployment.py` - Use Microsoft's functions instead
- Possibly `src/fabric_cicd/__init__.py` - No need to re-export

### Files That Should Exist
- `src/fabric_cicd/cli.py` - CLI entry point with auto-detection
- Possibly `src/fabric_cicd/parameters.py` - Only if Microsoft's package doesn't handle `parameter.yml` parsing

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

## Development Strategy

1. **Add dependency**: `pip install fabric-cicd` to `pyproject.toml`
2. **Simplify cli.py**: Import from `fabric-cicd`, remove custom implementations
3. **Remove unnecessary files**: Delete `workspace.py` and `deployment.py`
4. **Test parameter detection**: Ensure workspace_id extraction from `parameter.yml` works
5. **Test end-to-end**: Verify deployment works with Microsoft's package

## Related Projects

- **[microsoft/fabric-cicd](https://github.com/microsoft/fabric-cicd)**: The underlying library this wraps
- **Workspaces/**: Client workspace repos that will use this CLI tool
- **example-fabric-etl**: Core Python ETL framework (separate concern)