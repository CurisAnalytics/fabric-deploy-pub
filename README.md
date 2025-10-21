# fabric-deploy

CLI wrapper around Microsoft's [fabric-cicd](https://github.com/microsoft/fabric-cicd) that auto-detects workspace configuration from `parameter.yml`.

## Installation

```bash
pip install fabric-deploy
```

## Quick Start

```bash
cd path/to/workspace
fabric-cicd
```

**Requirements**: `parameter.yml` must exist in workspace directory. Authenticate with `az login`.

## CLI Options

```bash
fabric-cicd [DIRECTORY] [OPTIONS]

Options:
  --no-cleanup, -n       Skip unpublishing orphaned items
  --item-types, -t       Deploy specific types (e.g., Notebook DataPipeline)
  --exclude-types, -x    Exclude types (e.g., Report SemanticModel)
  --include-items, -i    Include specific items by name
```

## Authentication

Manage Azure accounts for Fabric deployments:

```bash
# List available accounts
fabric-cicd auth list

# Show current account
fabric-cicd auth current

# Switch to different account
fabric-cicd auth switch <subscription-id>
```

**Common issue**: If deployment fails with "workspace not found", you may be authenticated to the wrong tenant. Use `fabric-cicd auth list` to see available accounts and `fabric-cicd auth switch` to change.

## Configuration

**parameter.yml**:

```yaml
find_replace:
  - find_value: "dev-workspace-id"
    replace_value:
      prod: "prod-workspace-id"  # Destination workspace
    item_type: ["Notebook"]
```

All `replace_value` entries must use the same environment key (`prod`, `dev`, etc.).

## Programmatic Usage

```python
from fabric_deploy import FabricDeployer

deployer = FabricDeployer(repo_dir="/path/to/workspace")
deployer.deploy()
```


## Links

- [Microsoft Fabric CI/CD Documentation](https://microsoft.github.io/fabric-cicd/)
- [fabric-cicd on GitHub](https://github.com/microsoft/fabric-cicd)
