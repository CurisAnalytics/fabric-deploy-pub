# fabric-deploy

Simplified CLI tool for deploying Microsoft Fabric workspaces. Automatically detects configuration from `parameter.yml` to replace per-workspace `cicd.py` scripts.

> **⚠️ Implementation Status**: The package structure is complete, but the Fabric REST API calls in `deployment.py` are currently stub implementations. These need to be completed with actual Microsoft Fabric API integration before the tool is fully functional.

## Installation

```bash
pip install fabric-deploy
```

## Usage

Run from your Fabric workspace directory (where `parameter.yml` exists):

```bash
# Deploy using auto-detected configuration
fabric-cicd

# Deploy to a specific environment
fabric-cicd --env prod

# Dry run (preview without making changes)
fabric-cicd --dry-run

# Skip orphan cleanup
fabric-cicd --no-cleanup
```

## How It Works

The CLI auto-detects:
1. **Workspace ID**: From the first entry's `replace_value` in `parameter.yml`
2. **Environment**: Defaults to `env`, or specify with `--env`
3. **Lakehouse**: Auto-detected from `*.Lakehouse` directories

### Example parameter.yml

```yaml
find_replace:
  # Workspace ID
  - find_value: "14141414-1414-1414-1414-141414141414"
    replace_value:
      prod: "15151515-1515-1515-1515-151515151515"  # <- destination workspace
      dev: "abc123..."
      env: "def456..."
    item_type: ["Notebook", "CopyJob"]
  
  # Additional find/replace entries...
  - find_value: "dev-lakehouse-id"
    replace_value:
      prod: "prod-lakehouse-id"
      dev: "dev-lakehouse-id"
    item_type: ["Notebook", "SemanticModel"]
```

## Replacing cicd.py

This tool is designed to replace workspace-specific `cicd.py` scripts. Instead of maintaining this in each workspace:

```python
from fabric_cicd import FabricWorkspace, publish_all_items, unpublish_all_orphan_items

workspace_id = "12121212-1212-1212-1212-121212121212"
target_workspace = FabricWorkspace(
    workspace_id=workspace_id,
    repository_directory=str(Path(__file__).resolve().parent),
    environment="env",
)
publish_all_items(target_workspace)
unpublish_all_orphan_items(target_workspace, lakehouse_name)
```

Just run:

```bash
fabric-cicd
```

The CLI reads your `parameter.yml` to determine the workspace ID and environment automatically.

## CLI Options

```
fabric-cicd [DIRECTORY] [OPTIONS]

Arguments:
  DIRECTORY              Path to workspace directory (default: current directory)

Options:
  --env ENV              Target environment (default: auto-detected, prefers 'env')
  --dry-run              Preview deployment without making changes
  --no-cleanup           Skip unpublishing orphaned items
  --help                 Show help message
```

## Configuration Details

### parameter.yml Structure

The tool reads `parameter.yml` to:
- Extract the **destination workspace ID** from the first entry's `replace_value.<environment>`
- Determine available environments from the `replace_value` keys
- Apply find/replace transformations during deployment

### Item Types Supported

The following Fabric item types are supported:
- **Data**: Lakehouse, Warehouse, SQLDatabase, MirroredDatabase
- **Compute**: Notebook, Environment, DataPipeline, Dataflow
- **Analytics**: Report, SemanticModel, KQLDashboard, Eventhouse
- **Integration**: CopyJob, Eventstream, GraphQLApi

### Orphan Cleanup

By default, the tool unpublishes items that exist in the workspace but not in the repository. It automatically excludes:
- The lakehouse's default semantic model (auto-detected)

To skip orphan cleanup, use `--no-cleanup`.

## Development

### Project Structure

```
src/fabric_cicd/
├── __init__.py          # Package exports
├── cli.py               # CLI entry point with auto-detection
├── workspace.py         # FabricWorkspace class
├── parameters.py        # Parameter file handling
└── deployment.py        # Publish/unpublish functions
```

### Building from Source

```bash
# Install dependencies
pip install -e .

# Run locally
fabric-cicd
```

## Requirements

- Python 3.13+
- `pyyaml`
- `requests`
- `azure-identity`

## Implementation Status

### ✅ Complete
- CLI with auto-detection from `parameter.yml`
- Parameter file parsing and find/replace logic
- Lakehouse auto-detection
- FabricWorkspace configuration class
- Dry-run support
- Package structure and installation setup

### 🚧 TODO
- **Fabric API Integration**: `deployment.py` needs actual REST API calls to:
  - Publish items to workspace
  - List workspace items
  - Delete orphaned items
- **Authentication**: Implement Azure credential handling
- **Error handling**: Add retry logic and better error messages
- **Testing**: Integration tests with real Fabric workspaces

See `SUMMARY.md` for detailed implementation notes.

## License

CLI tool for Microsoft Fabric deployments