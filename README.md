# fabric-deploy

CLI wrapper around Microsoft's [fabric-cicd](https://github.com/microsoft/fabric-cicd) that auto-detects workspace configuration from `parameter.yml`.


## Features

- **Workspace Base Name + Git Branch**: Automatically resolve workspace ID from base name + current branch
- **Dynamic Parameter Generation**: Auto-detect source IDs and generate parameter.yml with dynamic variables
- **Item Filtering**: Deploy specific item types or exclude types
- **Auth Management**: Switch between Azure subscriptions
- **Rename Utility**: Rename directories to match display names

## Installation

```bash
pip install -e .
```

For development, changes to source code are immediately reflected without reinstalling.

## Quick Start

### Deployment

```bash
cd path/to/workspace
fabdep
```

**Requirements**: 
- `fabdep.yml` with `workspace_base_name` field
- Git repository with current branch

**How it works**:
1. Reads `workspace_base_name` from `fabdep.yml` (e.g., `"my_workspace"`)
2. Gets current git branch name (e.g., `"prod"`)
3. Combines them: `my_workspace [prod]`
4. Looks up workspace ID via Fabric REST API
5. Deploys to resolved workspace

**Why fabdep.yml instead of parameter.yml?**
- `fabdep.yml` is fabric-deploy specific configuration
- `parameter.yml` is read by Microsoft's fabric-cicd and only supports: `find_replace`, `key_value_replace`, `spark_pool`
- Adding custom fields to `parameter.yml` breaks fabric-cicd validation

**Benefits**:
- No manual workspace ID management
- Clean separation of fabric-deploy config vs fabric-cicd parameters
- Same config file works across all branches
- Automatic branch-based workspace routing

### Auto-Detect Mode (no parameter.yml needed)

```bash
cd path/to/workspace
fabdep -d -w <target-workspace-id>
```

This will:
1. Scan your workspace files to detect source IDs (workspace, lakehouse, SQL endpoints)
2. Check if target workspace matches source workspace:
   - If same: Skip parameter generation and deploy directly without replacements
   - If different: Generate parameter.yml with dynamic variables
3. Deploy to target workspace (fabric-cicd resolves IDs automatically)
4. Clean up generated files after deployment

**Same-Workspace Deployment**: When deploying to the same workspace as the source (e.g., updating items in place), the CLI automatically detects this and skips parameter generation, avoiding unnecessary replacements.

**Benefits**: No manual ID entry, same parameter file works across environments.


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
  --workspace-id, -w     Destination workspace ID (replaces parameter.yml)
  --auto-detect, -d      Auto-detect source IDs and generate parameters on-the-fly
  --save-params          Save auto-detected parameters to parameter.yml.detected
  --branch, -b           Override git branch name for workspace resolution
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

# Auto-detect mode: generate parameters on-the-fly
fabdep -d -w 19191919-1919-1919-1919-191919191919

# Auto-detect and save generated parameters for review
fabdep -d -w <workspace-id> --save-params

# Deploy to same workspace (updates items in place, no parameter replacement)
fabdep -d -w <same-workspace-id>  # Auto-detects match and skips parameter generation

# Override git branch for workspace resolution
fabdep --branch prod  # Deploy to "workspace_base_name [prod]"
fabdep -b staging     # Deploy to "workspace_base_name [staging]"
```


### Detect Parameters

Auto-detect source IDs from workspace files and optionally generate parameter.yml:

```bash
fabdep detect [DIRECTORY] [OPTIONS]

Arguments:
  DIRECTORY              Path to workspace directory (default: current directory)

Options:
  --save, -s             Save detected parameters to parameter.yml.detected
```

**What it detects**:
- Workspace IDs (from notebook metadata)
- Lakehouse IDs (from notebook metadata)
- SQL endpoint connection strings (from SemanticModel files)
- SQL endpoint IDs (from SemanticModel files)
- External connection IDs (detected but skipped - require manual setup)

**Examples**:
```bash
# View detected parameters
fabdep detect

# Generate parameter.yml.detected with dynamic variables
fabdep detect --save

# Detect from specific workspace
fabdep detect /path/to/workspace --save
```

**Generated format** uses dynamic variables:
```yaml
find_replace:
  # Workspace ID
  - find_value: "13131313-1313-1313-1313-131313131313"  # From source code
    replace_value:
      prod: "$workspace.$id"  # Resolved automatically at deploy time
    item_type: ['Notebook']

  # Lakehouse ID
  - find_value: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    replace_value:
      prod: "$items.Lakehouse.my_lakehouse.$id"
    item_type: ['Notebook']
```

**Note**: External connection IDs are automatically skipped because they require manual setup in the target workspace and typically don't change between environments.

See [SMART_REPLACE.md](./SMART_REPLACE.md) for complete documentation on dynamic variables.


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

### fabdep.yml

The fabric-deploy configuration file uses a workspace base name combined with the current git branch:

```yaml
# fabdep.yml
workspace_base_name: "my_workspace"
```

**Why fabdep.yml?**
- Keeps fabric-deploy configuration separate from fabric-cicd's `parameter.yml`
- Prevents validation errors (fabric-cicd only accepts: `find_replace`, `key_value_replace`, `spark_pool`)
- Clear separation of concerns

**How it resolves**:
- Base name: `my_workspace`
- Current branch: `prod` (from `git branch --show-current`)
- Resolved workspace name: `my_workspace [prod]`
- Workspace ID: Looked up via Fabric REST API

**Branch examples**:
- On `prod` branch → deploys to `my_workspace [prod]`
- On `dev` branch → deploys to `my_workspace [dev]`
- On `staging` branch → deploys to `my_workspace [staging]`

**Override branch name**:
You can override the git branch detection with the `--branch` flag:
```bash
# Deploy to prod workspace from any branch
fabdep --branch prod

# Deploy to staging workspace regardless of current git branch
fabdep -b staging
```
This is useful when:
- You want to deploy to a different environment without switching branches
- You're in a feature branch but want to deploy to a main workspace
- Your CI/CD pipeline needs to deploy to specific environments

**Requirements**:
- Must be in a git repository
- Workspace must exist in Fabric with exact name: `{base_name} [{branch}]`
- Branch names are matched case-insensitively
- Must have access to workspace in current Azure account

**Error handling**:
- If workspace not found, deployment fails with list of available workspaces
- If not in git repository, deployment fails with helpful error message
- Run `fabdep auth list` to verify correct Azure account

### Dynamic Variables (Auto-Generated)

In addition to the workspace base name, you can use `fabdep detect --save` to generate additional parameter replacements with dynamic variables that are resolved automatically at deploy time:

```yaml
find_replace:
  # Workspace ID - resolved from target workspace
  - find_value: "source-workspace-id"
    replace_value:
      prod: "$workspace.$id"  # No manual ID entry needed
    item_type: ["Notebook"]

  # Lakehouse ID - resolved after lakehouse is deployed
  - find_value: "source-lakehouse-id"
    replace_value:
      prod: "$items.Lakehouse.my_lakehouse.$id"  # Auto-resolved
    item_type: ["Notebook"]
```

**Benefits**:
- No manual ID maintenance - IDs are resolved automatically
- Same parameter file works across dev/test/prod environments
- Automatic dependency resolution (lakehouse deployed before notebooks)

See [SMART_REPLACE.md](./SMART_REPLACE.md) for complete documentation.



## Architecture

The package is organized into focused modules:

- **workspace.py**: `WorkspaceDeployer` dataclass - deployment logic wrapping `fabric-cicd`
- **param_detector.py**: `ParameterDetector` class - auto-detect source IDs and generate dynamic variables
- **cli.py**: All CLI logic - argument parsing, command handlers, user-facing text
- **auth.py**: Azure authentication utilities - account management via az CLI
- **logging.py**: Custom logging and console formatting utilities
- **rename.py**: Rename utility for workspace item directories
- **types.py**: Type definitions for Azure accounts and other shared types


## Migration Guide

### Moving from parameter.yml to fabdep.yml

If you previously had `workspace_base_name` in your `parameter.yml`, you need to migrate to the new `fabdep.yml` config file:

**Step 1: Create fabdep.yml**
```yaml
# fabdep.yml
workspace_base_name: "your_workspace_name"
```

**Step 2: Remove workspace_base_name from parameter.yml**

Before:
```yaml
# parameter.yml - DON'T DO THIS (breaks fabric-cicd validation)
workspace_base_name: "my_workspace"

find_replace:
  - find_value: "some-id"
    replace_value:
      prod: "replacement"
```

After:
```yaml
# parameter.yml - Only fabric-cicd fields
find_replace:
  - find_value: "some-id"
    replace_value:
      prod: "replacement"
```

**Why this change?**
- Microsoft's fabric-cicd validates `parameter.yml` strictly
- Only three top-level fields are allowed: `find_replace`, `key_value_replace`, `spark_pool`
- Custom fields like `workspace_base_name` cause validation errors
- Separating configs ensures compatibility and clean separation of concerns

**Quick Migration Script:**

If you have `workspace_base_name` in `parameter.yml`:

```bash
# Extract workspace_base_name and create fabdep.yml
grep "workspace_base_name:" parameter.yml > fabdep.yml

# Remove it from parameter.yml
sed -i.bak '/workspace_base_name:/d' parameter.yml
```

## Links

- [SMART_REPLACE.md](./SMART_REPLACE.md) - Complete guide to dynamic parameter generation
- [CLAUDE.md](./CLAUDE.md) - Detailed architecture and development guide
- [Microsoft Fabric CI/CD Documentation](https://microsoft.github.io/fabric-cicd/)
- [fabric-cicd on GitHub](https://github.com/microsoft/fabric-cicd)
- [Parameterization Guide](https://microsoft.github.io/fabric-cicd/latest/how_to/parameterization/) - Microsoft's dynamic variables documentation
