# fabric-deploy

CLI wrapper around Microsoft's [fabric-cicd](https://github.com/microsoft/fabric-cicd) that auto-detects workspace configuration from `parameter.yml`.


## Features

- **Auto-detection**: Reads workspace ID and environment from `parameter.yml`
- **Item Filtering**: Deploy specific item types or exclude types
- **Auth Management**: Switch between Azure subscriptions
- **Rename Utility**: Rename directories to match display names

## Installation

```bash
pip install -e .
```

For development, changes to source code are immediately reflected without reinstalling.

## Quick Start

```bash
cd path/to/workspace
fabdep
```

**Requirements**: `parameter.yml` must exist in workspace directory.


## Commands

### Deploy (default command)

Deploy workspace items to Microsoft Fabric:

```bash
fabdep [deploy] [DIRECTORY] [OPTIONS]

Arguments:
  DIRECTORY              Path to workspace directory (default: current directory)

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

**Examples**:
```bash
# Deploy all items from current directory
fabdep

# Deploy only notebooks and pipelines
fabdep -t Notebook DataPipeline

# Deploy excluding reports
fabdep -x Report SemanticModel

# Deploy without cleaning up orphaned items
fabdep -n

# Deploy from specific directory
fabdep /path/to/workspace
```


### Authentication

Manage Azure accounts for Fabric deployments:

```bash
fabdep auth <command>

Commands:
  list                   List current and all available Azure accounts
  switch <sub-id>        Switch to different subscription
  rename <sub-id> <name> Rename a subscription (auto-switches if needed)
```

**Examples**:
```bash
# List current and available accounts
fabdep auth list

# Switch to different account
fabdep auth switch xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# Rename a subscription (automatically switches if needed)
fabdep auth rename xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx "Production Subscription"
```

**Common issue**: If deployment fails with "workspace not found", you may be authenticated to the wrong tenant. Use `fabdep auth list` to see current and available accounts, then `fabdep auth switch` to change.


### Rename Items

Rename workspace item directories to match their display names from `.platform` metadata:

```bash
fabdep rename [DIRECTORY] [OPTIONS]

Arguments:
  DIRECTORY              Path to workspace directory (default: current directory)

Options:
  --execute, -e          Actually perform renames (default is dry-run)
  --clean, -c            Apply .lower().replace(' ', '_') to directory names
```

**Examples**:
```bash
# Preview what would be renamed (dry-run)
fabdep rename

# Actually perform the renames
fabdep rename -e

# Rename items in specific directory
fabdep rename /path/to/workspace -e -c
```

This utility:
- Searches for all `.platform` files in the workspace
- Renames parent directories to match their display names
- By default uses exact display name
- With `--clean`, converts to lowercase with underscores (e.g., "My Notebook" → "my_notebook")
- Runs in dry-run mode by default
- Shows which directories are skipped when they already match


## Configuration

### parameter.yml

The destination Workspace ID is extracted from the first `find_replace` entry in `parameter.yml`:

```yaml
find_replace:
  # First entry's replace_value is used as destination workspace ID
  - find_value: "source-workspace-id"
    replace_value:
      prod: "dest-workspace-id"  # CLI uses this as workspace_id
    item_type: ["Notebook", "CopyJob"]
  # Additional find/replace entries for parameter replacement
  - find_value: "source-lakehouse-id"
    replace_value:
      prod: "dest-lakehouse-id"
    item_type: ["Notebook", "SemanticModel"]
```

**Important**: All `replace_value` entries must use the same environment key (e.g., all use `prod` or all use `dev`).

See `example_parameter.yml` for a complete example.

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


## Architecture

The package is organized into focused modules:

- **workspace.py**: `WorkspaceDeployer` dataclass - deployment logic wrapping `fabric-cicd`
- **cli.py**: All CLI logic - argument parsing, command handlers, user-facing text
- **auth.py**: Azure authentication utilities - account management via az CLI
- **logging.py**: Custom logging and console formatting utilities
- **rename.py**: Rename utility for workspace item directories


## Links

- [Microsoft Fabric CI/CD Documentation](https://microsoft.github.io/fabric-cicd/)
- [fabric-cicd on GitHub](https://github.com/microsoft/fabric-cicd)
