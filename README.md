# fabric-deploy

A thin CLI wrapper around Microsoft's [`fabric-cicd`](https://github.com/microsoft/fabric-cicd) package that simplifies deployment by auto-detecting configuration from `parameter.yml` files.

## Why This Tool?

Eliminates the need for per-workspace `cicd.py` scripts. Instead of maintaining deployment scripts in each workspace, just run one command.

## Installation

```bash
pip install fabric-deploy
```

Or with `uv`:
```bash
uv pip install fabric-deploy
```

## Quick Start

From your Fabric workspace directory:

```bash
fabric-cicd
```

That's it! The tool auto-detects everything from your `parameter.yml`.

## Usage

```bash
# Deploy using auto-detected configuration
fabric-cicd

# Deploy from a specific directory
fabric-cicd /path/to/workspace

# Skip orphan cleanup
fabric-cicd --no-cleanup
```

## How It Works

The CLI automatically detects:
1. **Workspace ID**: From the first entry's `replace_value` in `parameter.yml`
2. **Environment**: From the environment keys used in `replace_value` (must be consistent)
3. **Lakehouse Name**: From `*.Lakehouse/.platform` metadata files

### Example parameter.yml

```yaml
find_replace:
  # The first entry's replace_value is used to determine workspace ID and environment
  - find_value: "364abdbe-c061-4f74-b34a-9631bc7e3e4d"
    replace_value:
      prod: "6c5304f1-2e84-4add-bd46-25a1e943cdec"  # Destination workspace ID
    item_type: ["Notebook", "CopyJob"]

  # Additional find/replace entries for parameter substitution
  - find_value: "dev-lakehouse-id"
    replace_value:
      prod: "prod-lakehouse-id"  # Must use same environment key (prod)
    item_type: ["Notebook", "SemanticModel"]
```

**Important**: All `replace_value` entries must use the same environment key. The CLI validates this consistency.

## Replacing cicd.py

This tool is designed to replace workspace-specific `cicd.py` scripts. Instead of maintaining this in each workspace:

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
  --no-cleanup           Skip unpublishing orphaned items
  --help                 Show help message
```

## What Gets Deployed

The tool uses Microsoft's `fabric-cicd` package to:
1. **Publish all items** from your repository to the target workspace
2. **Apply find/replace** transformations from `parameter.yml`
3. **Clean up orphaned items** (optional) - removes items that exist in the workspace but not in the repository

### Supported Item Types

All Fabric item types supported by Microsoft's `fabric-cicd` package:
- **Data**: Lakehouse, Warehouse, SQLDatabase, MirroredDatabase
- **Compute**: Notebook, Environment, DataPipeline, Dataflow
- **Analytics**: Report, SemanticModel, KQLDashboard, Eventhouse
- **Integration**: CopyJob, Eventstream, GraphQLApi, VariableLibrary
- **Jobs**: ApacheAirflowJob, Reflex, MountedDataFactory

### Orphan Cleanup

By default, the tool removes items that exist in the workspace but not in your repository. The lakehouse's default semantic model is automatically excluded from cleanup.

To skip orphan cleanup:
```bash
fabric-cicd --no-cleanup
```

## Development

### Project Structure

```
fabric-deploy/
├── src/fabric_deploy/
│   ├── __init__.py      # Package initialization
│   └── cli.py           # CLI entry point with auto-detection
├── pyproject.toml       # Package configuration
└── example_parameter.yml # Example configuration file
```

### Building from Source

```bash
# Install with uv
uv sync

# Build distribution
uv build

# Install locally for development
pip install -e .

# Run locally
fabric-cicd
```

## Requirements

- Python 3.13
- `fabric-cicd>=0.1.29` (Microsoft's official package)
- `pyyaml` (for parsing parameter.yml)

## Troubleshooting

### Common Errors

**"parameter.yml not found"**
- Run the command from your Fabric workspace directory
- Ensure `parameter.yml` exists in the workspace root

**"No environments found in parameter.yml"**
- Check that `find_replace` entries have `replace_value` dictionaries
- Verify at least one environment key exists (e.g., `prod`, `dev`)

**"Multiple different environments found"**
- All `replace_value` entries must use the same environment key
- Update entries to use consistent naming (all `prod` or all `dev`, etc.)

**"No lakehouse found in repository directory"**
- Ensure at least one `*.Lakehouse` directory exists
- Verify the directory contains a `.platform` metadata file

## How It's Different

Unlike per-workspace `cicd.py` scripts, this tool:
- Works across all workspaces without modification
- Auto-detects configuration from existing `parameter.yml` files
- Eliminates code duplication across workspace repositories
- Provides consistent CLI interface for all deployments

## License

Internal tool forEngineering

## See Also

- [Microsoft Fabric CI/CD Documentation](https://microsoft.github.io/fabric-cicd/)
- [fabric-cicd on GitHub](https://github.com/microsoft/fabric-cicd)