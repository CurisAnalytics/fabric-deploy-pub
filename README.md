# fabric-deploy

CLI wrapper around Microsoft's [fabric-cicd](https://github.com/microsoft/fabric-cicd) that auto-detects workspace configuration from `parameter.yml`.

## Installation

Install in editable mode:

```bash
git clone <repository-url>
cd fabric-deploy
pip install -e .
```

Changes to the source code will be immediately reflected without reinstalling.

## Quick Start

```bash
cd path/to/workspace
fabdep
```

**Requirements**: `parameter.yml` must exist in workspace directory.

## CLI Commands

### Deploy (default)

```bash
fabdep [deploy] [DIRECTORY] [OPTIONS]

Options:
  --no-cleanup, -n       Skip unpublishing orphaned items
  --item-types, -t       Deploy specific types (e.g., Notebook DataPipeline)
  --exclude-types, -x    Exclude types (e.g., Report SemanticModel)
  --include-items, -i    Include specific items by name
```

The `deploy` subcommand is optional and defaults when deploy-specific options are used:

```bash
# These are equivalent:
fabdep -t DataPipeline
fabdep deploy -t DataPipeline
```

### Rename Items

Rename workspace item directories to match their display names from `.platform` metadata:

```bash
fabdep rename [DIRECTORY] [OPTIONS]

Options:
  --execute, -e          Actually perform renames (default is dry-run)
```

**Example usage**:
```bash
# Preview what would be renamed (dry-run)
fabdep rename

# Actually perform the renames
fabdep rename --execute

# Rename items in specific directory
fabdep rename /path/to/workspace -e
```

This utility searches for all `.platform` files in the workspace and renames their parent directories to use lowercase, underscore-separated versions of the display names. By default, it runs in dry-run mode to preview changes before applying them.

### Authentication

Manage Azure accounts for Fabric deployments:

```bash
# List current and available accounts
fabdep auth list

# Switch to different account
fabdep auth switch <subscription-id>

# Rename a subscription (requires Owner permissions)
# Automatically switches to the subscription if needed
fabdep auth rename <subscription-id> <new-name>
```

**Common issue**: If deployment fails with "workspace not found", you may be authenticated to the wrong tenant. Use `fabdep auth list` to see current and available accounts, then `fabdep auth switch` to change. Add a new tenant with `az login --allow-no-subscriptions`.


## Configuration

The destination Workspace ID is taken from the first `find_replace entry` in the target repo's parameters (see `example_parameter.yml`).

**parameter.yml**:

```yaml
find_replace:
  - find_value: "dev-workspace-id"
    replace_value:
      prod: "prod-workspace-id"  # Destination workspace
    item_type: ["Notebook"]
find_replace: # - find_value, etc.
```

All `replace_value` entries must use the same environment key (`prod`, `dev`, etc.).

### Parameter File Validation

The CLI automatically validates `parameter.yml` and detects invalid configurations:

**Invalid scenarios** (triggers automatic `.bak` renaming):
- Duplicate `find_value` entries (same value appears multiple times)
- Blank or empty destination values (e.g., `prod: ""` or `prod: null`)

**What happens when invalid**:
1. CLI logs warning messages explaining the validation failures
2. Renames `parameter.yml` → `parameter.yml.bak`
3. Deployment continues using workspace ID from first entry only
4. Microsoft's `fabric-cicd` ignores `.bak` files (no parameter replacement occurs)

This is useful when you want to deploy without parameter replacement (e.g., deploying unchanged items to test workspace configuration).

**Example**: See `example_parameter_invalid.yml` for a file with intentional validation failures.

## Programmatic Usage

Both `WorkspaceDeployer` and `rename_items_by_display_name` can be imported and used in your own Python code:

```python
from fabric_deploy import WorkspaceDeployer, rename_items_by_display_name

# Deploy workspace items
deployer = WorkspaceDeployer(repo_dir="/path/to/workspace")
deployer.deploy()

# With custom options
deployer.deploy(
    item_types=["Notebook", "DataPipeline"],
    no_cleanup=False,
)

# Rename items to match display names
rename_items_by_display_name(
    repo_dir="/path/to/workspace",
    dry_run=True,  # Set to False to actually rename
)
```


## Links

- [Microsoft Fabric CI/CD Documentation](https://microsoft.github.io/fabric-cicd/)
- [fabric-cicd on GitHub](https://github.com/microsoft/fabric-cicd)
