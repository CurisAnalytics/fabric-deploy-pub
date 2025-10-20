# fabric-deploy

CLI wrapper around Microsoft's [fabric-cicd](https://github.com/microsoft/fabric-cicd) that auto-detects workspace configuration from `parameter.yml`.

Eliminates per-workspace `cicd.py` scripts.

## Installation

```bash
pip install fabric-deploy
```

## Usage

Deploy a Fabric workspace:

```bash
cd path/to/workspace
fabric-cicd
```

**Requirements**:

1. `parameter.yml` must exist in workspace directory
2. Authenticate using `az login` or `az account set -s "Credential Name"`

The deployment:

- Auto-detects workspace ID and environment from `parameter.yml`
- Publishes all items to target workspace
- Applies find/replace transformations
- Cleans up orphaned items (unless `--no-cleanup` specified)

## CLI Options

```bash
fabric-cicd [DIRECTORY] [OPTIONS]

Arguments:
  DIRECTORY              Workspace directory (default: current)

Options:
  --no-cleanup, -n       Skip unpublishing orphaned items
  --item-types, -t       Deploy specific types (e.g., Notebook DataPipeline)
  --exclude-types, -x    Exclude types (e.g., Report SemanticModel)
  --include-items, -i    Include specific items by name (experimental)
```

### Examples

```bash
# Deploy from current directory
fabric-cicd

# Deploy specific directory without cleanup
fabric-cicd /path/to/workspace --no-cleanup

# Deploy only notebooks and pipelines
fabric-cicd --item-types Notebook DataPipeline

# Exclude reports and semantic models
fabric-cicd --exclude-types Report SemanticModel
```

## Configuration

**parameter.yml** (in workspace):

```yaml
find_replace:
  # First entry determines workspace ID and environment
  - find_value: "364abdbe-c061-4f74-b34a-9631bc7e3e4d"
    replace_value:
      prod: "6c5304f1-2e84-4add-bd46-25a1e943cdec"  # Destination workspace
    item_type: ["Notebook", "CopyJob"]

  # Additional find/replace entries
  - find_value: "dev-lakehouse-id"
    replace_value:
      prod: "prod-lakehouse-id"  # Must use same environment key
    item_type: ["Notebook", "SemanticModel"]
```

**Important**: All `replace_value` entries must use the same environment key (e.g., all `prod` or all `dev`).

## Replaces cicd.py

**Before** (in each workspace):

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

**After**:

```bash
fabric-cicd
```

## Development

```bash
# Build from source
uv build

# Install locally
pip install -e .
```

## Requirements

- Python 3.13
- `fabric-cicd>=0.1.29`
- `pyyaml`

## See Also

- [Microsoft Fabric CI/CD Documentation](https://microsoft.github.io/fabric-cicd/)
- [fabric-cicd on GitHub](https://github.com/microsoft/fabric-cicd)