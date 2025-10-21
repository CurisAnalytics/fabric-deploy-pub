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
