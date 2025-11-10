# fabric-deploy

CLI wrapper around Microsoft's [fabric-cicd](https://github.com/microsoft/fabric-cicd) that provides workspace-based deployment using workspace name + git branch resolution.

## Features

- **Workspace Base Name + Git Branch**: Automatically resolve workspace ID from base name + branch
- **Dynamic Parameter Generation**: Auto-detect source IDs and generate parameter.yml with dynamic variables
- **Bootstrap Mode**: Two-phase deployment for blank workspaces (foundational items first, then all items)
- **Workspace Creation**: Create workspaces from scratch with `--create-workspace`
- **Item Filtering**: Deploy specific item types or exclude types
- **Auth Management**: Switch between Azure subscriptions
- **Rename Utility**: Rename directories to match display names

## Installation

```bash
pip install fabric-deploy
```

For development:
```bash
git clone <repo>
cd fabric-deploy
pip install -e .
```

## Quick Start

### Create fabdep.yml

```yaml
workspace_base_name: "my_workspace"
capacity_id: "12345-67890-abcd-..."  # Optional, auto-detected if not specified
```

### Deploy

```bash
cd path/to/workspace
fabdep
```

**How it works**:
1. Reads `workspace_base_name` from `fabdep.yml` (e.g., `"my_workspace"`)
2. Gets current git branch name (e.g., `"prod"`)
3. Combines them: `my_workspace [prod]`
4. Looks up workspace ID via Fabric REST API
5. Deploys to resolved workspace

**Benefits**:
- No manual workspace ID management
- Same config file works across all branches
- Automatic branch-based workspace routing
- Clean separation from fabric-cicd's parameter.yml

## Commands

### Deploy (default command)

```bash
fabdep [deploy] [DIRECTORY] [OPTIONS]

Arguments:
  DIRECTORY              Path to workspace directory (default: current)

Options:
  --no-cleanup, -n       Skip unpublishing orphaned items
  --item-types, -t       Deploy specific types (e.g., Notebook DataPipeline)
  --exclude-types, -x    Exclude types (e.g., Report SemanticModel)
  --include-items, -i    Include specific items by name
  --auto-detect, -d      Auto-detect source IDs and generate parameters on-the-fly
  --save-params          Save auto-detected parameters to parameter.yml.detected
  --branch, -b           Override git branch name for workspace resolution
  --bootstrap            Enable two-phase deployment (foundational items first, then all)
  --create-workspace     Create workspace if it doesn't exist (uses default capacity if not specified)
```

**Examples**:
```bash
# Deploy all items using git branch
fabdep

# Deploy only notebooks and pipelines
fabdep -t Notebook DataPipeline

# Deploy excluding reports
fabdep -x Report SemanticModel

# Deploy to specific branch workspace
fabdep --branch prod  # Deploys to "workspace_base_name [prod]"

# Auto-detect parameters (no parameter.yml needed)
fabdep -d

# Auto-detect and save generated parameters
fabdep -d --save-params

# Bootstrap mode - deploy to blank workspace
fabdep --bootstrap

# Bootstrap mode - create workspace first, then deploy
fabdep --bootstrap --create-workspace

# Auto-detect + bootstrap (recommended for new workspaces)
fabdep -d --bootstrap
```

### Bootstrap Mode

Deploy to blank workspaces or create workspaces from scratch with two-phase deployment.

**Problem**: Notebooks and DataPipelines reference lakehouse IDs that don't exist yet. Bootstrap mode solves this by deploying foundational items (Lakehouse, SemanticModel, etc.) first, then deploying everything else.

**Usage**:
```bash
# Deploy to existing blank workspace
fabdep --bootstrap

# Create workspace and deploy
fabdep --bootstrap --create-workspace

# Auto-detect + bootstrap (recommended)
fabdep -d --bootstrap
```

**Phase 1** (Foundational items):
- Lakehouse, SemanticModel, SQLDatabase, Warehouse, MirroredDatabase, Eventhouse

**Phase 2** (All items):
- Notebook, DataPipeline, Environment, Report, and all other items

**Connection GUIDs**: Bootstrap mode detects external connection GUIDs and warns you that they must be configured manually in the target workspace.

```

### Detect Parameters

```bash
fabdep detect [DIRECTORY] [OPTIONS]

Options:
  --save, -s             Save detected parameters to parameter.yml.detected
```

Auto-detects source IDs from workspace files and optionally generates parameter.yml with dynamic variables.

**What it detects**:
- Workspace IDs (from notebook metadata)
- Lakehouse IDs (from notebook metadata)
- SQL endpoint connection strings (from SemanticModel files)
- External connection IDs (detected but skipped - require manual setup)

**Examples**:
```bash
# View detected parameters
fabdep detect

# Generate parameter.yml.detected with dynamic variables
fabdep detect --save
```

**Generated format** uses dynamic variables:
```yaml
find_replace:
  # Workspace ID
  - find_value: "fb61a71c-b2af-49dc-9150-9040010c79bb"
    replace_value:
      prod: "$workspace.$id"  # Resolved automatically
    item_type: ['Notebook']

  # Lakehouse ID
  - find_value: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    replace_value:
      prod: "$items.Lakehouse.my_lakehouse.$id"
    item_type: ['Notebook']
```


### Authentication

```bash
fabdep auth <command>

Commands:
  list                   List current and all available Azure accounts
  switch <sub-id>        Switch to different subscription
  rename <sub-id> <name> Rename a subscription
```

**Examples**:
```bash
# List accounts
fabdep auth list

# Switch to different account
fabdep auth switch xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# Rename subscription (auto-switches if needed)
fabdep auth rename xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx "Production"
```

### Rename Items

```bash
fabdep rename [DIRECTORY] [OPTIONS]

Options:
  --execute, -e          Actually perform renames (default is dry-run)
  --clean, -c            Apply .lower().replace(' ', '_') to directory names
```

Renames workspace item directories to match their display names from `.platform` metadata.

**Examples**:
```bash
# Preview renames (dry-run)
fabdep rename

# Execute renames
fabdep rename -e

# Rename with cleaning (lowercase + underscores)
fabdep rename -e -c
```

## Configuration

### fabdep.yml (required)

```yaml
workspace_base_name: "my_workspace"
capacity_id: "12345-67890-abcd-..."  # Optional, auto-detected if not specified

# Optional: Manual parameter overrides (replace_value only, find_value is auto-detected)
manual_parameters:
  - find_value: "ffffffff-ffff-ffff-ffff-ffffffffffff"  # Auto-detected from source files
    replace_value: "ffffffff-ffff-ffff-ffff-ffffffffffff"  # Keep same (external reference)
    item_type: ["SemanticModel"]
    description: "External lakehouse SQL endpoint"
```

**Notes**:
- When using `--create-workspace`, if `capacity_id` is not specified, fabric-deploy automatically uses the first active Fabric capacity
- `manual_parameters` allows you to override the replace_value for auto-detected parameters (e.g., to keep external references)

**Why fabdep.yml?**
- Separate from fabric-cicd's `parameter.yml`
- Prevents validation errors (fabric-cicd only accepts: `find_replace`, `key_value_replace`, `spark_pool`)
- Clear separation of concerns

**How workspace resolution works**:
- Base name: `my_workspace`
- Current branch: `prod` (from `git branch --show-current`)
- Resolved workspace name: `my_workspace [prod]`
- Workspace ID: Looked up via Fabric REST API

**Override branch**:
```bash
# Deploy to prod workspace from any branch
fabdep --branch prod

# Deploy to staging workspace
fabdep -b staging
```

### parameter.yml (optional)

Optional parameter file for fabric-cicd's find/replace functionality. See [Microsoft's documentation](https://microsoft.github.io/fabric-cicd/latest/how_to/parameterization/) for details.

Use `fabdep detect --save` to auto-generate with dynamic variables.

## Architecture

```
src/fabric_deploy/
├── __init__.py         # Package exports
├── workspace.py        # WorkspaceDeployer dataclass
├── detect.py           # ParameterDetector class
├── api.py              # Fabric REST API integration
├── cli.py              # CLI interface
├── auth.py             # Azure auth utilities
├── log.py              # Logging and formatting
└── rename.py           # Rename utility
```

**Key Components**:
- **WorkspaceDeployer**: Deployment logic wrapping fabric-cicd
- **ParameterDetector**: Auto-detect source IDs and generate dynamic variables
- **CLI Layer**: Centralized presentation with business logic separation

## Programmatic Usage

**Note**: Primary interface is the CLI. Programmatic API is minimal and designed for Azure DevOps integration.

```python
from fabric_deploy import WorkspaceDeployer

# Basic deployment (reads fabdep.yml from repo_dir)
deployer = WorkspaceDeployer(repo_dir="/path/to/workspace")
deployer.deploy()

# With filtering and options
deployer = WorkspaceDeployer(repo_dir="/path/to/workspace")
deployer.deploy(item_types=["Notebook", "DataPipeline"], no_cleanup=False)

# With auto-detect and branch override
deployer = WorkspaceDeployer(
    repo_dir="/path/to/workspace",
    auto_detect=True,
    branch_override="prod",
)
deployer.deploy()

# Bootstrap mode with workspace creation
deployer = WorkspaceDeployer(
    repo_dir="/path/to/workspace",
    auto_detect=True,
    bootstrap=True,
    create_workspace=True,
)
deployer.deploy()
```

## Troubleshooting

**"fabdep.yml not found"**
- Create `fabdep.yml` in workspace root with `workspace_base_name: "your_workspace"`

**"Workspace not found: 'workspace_name'"**
- Verify workspace exists in Fabric with name: `{base_name} [{branch}]`
- Run `fabdep auth list` to verify Azure account
- Check workspace access permissions

**"Failed to get git branch"**
- Ensure running from git repository (not detached HEAD)
- Use `--branch` flag to override

**"No lakehouse found"**
- Ensure at least one `*.Lakehouse` directory exists with `.platform` file

**"User does not have access to the connection"**
- Connection GUIDs are tenant-wide
- Edit pipeline files directly to update connection GUIDs
- Run `fabdep detect` to discover current connection GUIDs

**Authentication errors**
- Run `fabdep auth list` to see accounts
- Switch with `fabdep auth switch <sub-id>`

## Links

- [CLAUDE.md](./CLAUDE.md) - Detailed architecture and development guide
- [Microsoft Fabric CI/CD Documentation](https://microsoft.github.io/fabric-cicd/)
- [fabric-cicd on GitHub](https://github.com/microsoft/fabric-cicd)
