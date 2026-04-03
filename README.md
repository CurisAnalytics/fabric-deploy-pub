# fabric-deploy

CLI wrapper around Microsoft's [fabric-cicd](https://github.com/microsoft/fabric-cicd) that deploys git-synced Fabric Workspaces.

### Features

- **Workspace Resolution**: Base name + git branch → workspace ID via Fabric API
- **Auto-Detect Parameters**: Scans workspace files, generates dynamic parameter.yml
- **Workspace Creation**: Create new workspaces with `-N`
- **Item Filtering**: Deploy/exclude specific item types
- **Feature Branch Support**: Skip lakehouse replacement with `-L` (use dev lakehouse)
- **Auth Management**: Switch accounts, persistent SPN logins & aliases
- **Configurable Retries**: Auto-retry transient errors (customizable with `-r`)


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

### Deploy (default)

```bash
fabdep [DIRECTORY] [OPTIONS]

Options:
  -n, --no-cleanup       Skip unpublishing orphaned items
  -t, --item-types       Item types to deploy (e.g., Notebook DataPipeline)
  -x, --exclude-types    Item types to exclude (e.g., Report SemanticModel)
  -i, --include-items    Specific items only (format: "Name.Type")
  -b, --branch           Override git branch for workspace resolution
  -N, --new-workspace    Create workspace if it doesn't exist
  -r, --max-retries      Max retries for transient errors (default: 1)
  -L, --skip-lakehouse   Skip lakehouse parameter replacement (for feature branches)
```

**Examples**:
```bash
fabdep                    # Deploy (auto-detect parameters)
fabdep -t Notebook        # Deploy only notebooks
fabdep -x Report          # Deploy excluding reports
fabdep -b prod            # Deploy to "base_name [prod]"
fabdep -N                 # Create workspace and deploy
fabdep -r 2               # Retry up to 2 times on errors
fabdep -L                 # Feature branch: use dev lakehouse references
```

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
# List accounts (shows whether authenticated as User or Service Principal)
fabdep auth list

# Switch to different account
fabdep auth switch xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# Rename account (persists across az login/refresh)
fabdep auth rename xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx "Production"
```

**Authenticating as a Service Principal** (e.g. for client tenants):
```bash
az login --service-principal \
  -u <CLIENT_ID> \
  -p <CLIENT_SECRET> \
  --tenant <TENANT_ID> \
  --allow-no-subscriptions
```

SPNs are auto-persisted to `~/.azure/azureProfile.fabdep.json` on first `fabdep auth list`, so they survive `az account list --refresh` and re-logins. Renames are also stored there as aliases.


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
- With `-N`, if `capacity_id` is not specified, first active Fabric capacity is used
- `manual_parameters` overrides auto-detected parameters (e.g., to keep external references)


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
