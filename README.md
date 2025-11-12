# fabric-deploy

CLI wrapper around Microsoft's [fabric-cicd](https://github.com/microsoft/fabric-cicd) that deploys git-synced Fabric Workspaces.

### Features

- **Workspace Base Name + Git Branch**: Automatically resolve workspace ID from base name + branch
- **Dynamic Parameter Generation**: Auto-detect source IDs and generate parameter.yml with dynamic variables
- **Bootstrap Mode**: Two-phase deployment for blank workspaces (foundational items first, then all items)
- **Workspace Creation**: Create workspaces from scratch with `--create-workspace`
- **Item Filtering**: Deploy specific item types or exclude types
- **Auth Management**: Switch between Azure subscriptions
- **Rename Utility**: Rename directories to match display names


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


---

## Installation

Install it in editable mode so that you can pull changes directly from git:
```bash
git clone <repo>
cd fabric-deploy
pip install -e .
```

## Quick Start

### Create fabdep.yml

```yaml
workspace_base_name: "my_workspace"
```

### Deploy

```bash
cd path/to/workspace
fabdep
```

*How it works:*
1. Reads `workspace_base_name` from `fabdep.yml` (e.g., `"my_workspace"`)
2. Gets current git branch name (e.g., `"prod"`)
3. Combines them: `my_workspace [prod]`
4. Looks up workspace ID via Fabric REST API
5. Deploys to resolved workspace


---

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
  --no-auto-detect       Disable auto-detect (opt-out; use existing parameter.yml)
  --save-params          Save auto-detected parameters to parameter.yml.detected
  --branch, -b           Override git branch name for workspace resolution
  --bootstrap            Enable two-phase deployment (foundational items first, then all)
  --create-workspace     Create workspace if it doesn't exist (uses default capacity if not specified)
```

**Examples**:
```bash
# Auto-detect parameters (no parameter.yml needed) and deploy all items
fabdep

# Deploy only notebooks and pipelines
fabdep -t Notebook DataPipeline

# Deploy excluding reports
fabdep -x Report SemanticModel

# Deploy to specific branch workspace
fabdep -b prod  # Deploys to "workspace_base_name [prod]"

# Auto-detect and save generated parameters (auto-detect enabled by default)
fabdep --save-params

# Bootstrap mode - deploy to blank workspace
fabdep --bootstrap

# Bootstrap mode - create workspace first, then deploy
fabdep --bootstrap --create-workspace
```

### Bootstrap Mode

Deploy to blank workspaces or create workspaces from scratch with two-phase deployment.

Notebooks and DataPipelines reference lakehouse IDs that don't exist yet.

Bootstrap mode solves this by deploying foundational items (Lakehouse, SemanticModel, etc.) first, then deploying everything else.

*Phase 1 (Foundational items)*: Lakehouse, SemanticModel, SQLDatabase, Warehouse, MirroredDatabase, and Eventhouse items.

*Phase 2 (All items)*: Notebook, DataPipeline, Environment, Report, and all other items.

**Connection GUIDs**: Bootstrap mode detects external connection GUIDs and warns you that they must be configured manually in the target workspace.


---


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


---


### Rename Items

```bash
fabdep rename [DIRECTORY] [OPTIONS]

Options:
  --execute, -e          Actually perform renames (default is dry-run)
  --clean, -c            Apply .lower().replace(' ', '_') to directory names
```

Renames workspace item directories to match their display names from `.platform` metadata.


---

## Configuration

### fabdep.yml (required)

```yaml
workspace_base_name: "my_workspace"
capacity_id: "12345-67890-abcd-..."  # Optional, auto-detected if not specified

# Optional: Manual parameter overrides
manual_parameters:
  - find_value: "ffffffff-ffff-ffff-ffff-ffffffffffff"  # Auto-detected from source files
    replace_value: "ffffffff-ffff-ffff-ffff-ffffffffffff"  # Don't change in this branch
    item_type: ["SemanticModel"]
    description: "External lakehouse SQL endpoint"
```

**Notes**:
- When using `--create-workspace`, if `capacity_id` is not specified, fabric-deploy automatically uses the first active Fabric capacity
- `manual_parameters` allows you to override the replace_value for auto-detected parameters (e.g., to keep external references)


**How workspace resolution works**:
- Base name: `my_workspace`
- Current branch: `prod` (from `git branch --show-current`)
- Resolved workspace name: `my_workspace [prod]`
- Workspace ID: Looked up via Fabric REST API

**Override branch**:
```bash
# Deploy to staging workspace
fabdep -b staging
```


---


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

**"User does not have access to the connection"**
- Connection GUIDs are tenant-wide
- Edit pipeline files directly to update connection GUIDs
- Run `fabdep detect` to discover current connection GUIDs

**Authentication errors**
- Run `fabdep auth list` to see accounts
- Switch with `fabdep auth switch <sub-id-or-name>`

## Links

- [CLAUDE.md](./CLAUDE.md) - Detailed architecture and development guide
- [Microsoft Fabric CI/CD Documentation](https://microsoft.github.io/fabric-cicd/)
- [fabric-cicd on GitHub](https://github.com/microsoft/fabric-cicd)
