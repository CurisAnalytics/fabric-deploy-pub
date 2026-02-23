# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`fabric-deploy` is a **thin CLI wrapper** around Microsoft's official [`fabric-cicd`](https://github.com/microsoft/fabric-cicd) package. It provides workspace-based deployment using workspace name + git branch resolution via Fabric REST API.

**Key Features**:
- **Workspace Resolution**: Combines workspace base name + git branch to resolve workspace ID via Fabric API
- **Auto-Detect (Default)**: Scans workspace files to detect source IDs and generates dynamic parameter replacements
- **Feature Branch Support**: Skip lakehouse replacement with `--skip-lakehouse` to use dev lakehouse data
- **CLI Convenience**: Simple command-line interface wrapping Microsoft's fabric-cicd package

## Architecture

```sh
fabric-deploy/
├── src/fabric_deploy/         # Package directory
│   ├── __init__.py            # Package initialization (minimal public API)
│   ├── workspace.py           # WorkspaceDeployer class - deployment business logic
│   ├── detect.py              # ParameterDetector class - auto-detect source IDs
│   ├── api.py                 # Fabric REST API integration - workspace lookup
│   ├── cli.py                 # ALL CLI logic - parsers, handlers, user-facing text
│   ├── auth.py                # Azure auth utilities - business logic only
│   ├── log.py                 # Custom logging & console formatting utilities
│   └── rename.py              # Rename utility - business logic only
├── pyproject.toml             # Package configuration, depends on fabric-cicd
└── README.md                  # User-facing documentation
```

**Key Components**:
- **WorkspaceDeployer**: Dataclass for deployment logic (wraps Microsoft's fabric-cicd)
- **ParameterDetector**: Auto-detect source IDs and generate dynamic variables
- **CLI Layer**: Centralized presentation with business logic separation

**CLI Pattern**: Centralized presentation layer:
- `cli.py` contains ALL CLI logic - argument parsing, command handling, error messages, user-facing text
- Feature modules (`auth.py`, `workspace.py`, `rename.py`) contain ONLY business logic
- Clear separation: CLI/presentation layer vs business logic layer

## Development Commands

```bash
pip install -e .              # Install in editable mode
fabdep                        # Deploy workspace (auto-detects from fabdep.yml)
fabdep -t Notebook Pipeline   # Deploy specific item types
fabdep -x Report              # Exclude item types
fabdep -N                     # Create new workspace
fabdep -b feature-branch      # Override git branch
fabdep -r 2                   # Retry up to 2 times on transient errors
fabdep -L                     # Skip lakehouse replacement (feature branch mode)
fabdep auth list              # Show Azure accounts
fabdep rename -e              # Rename directories to match display names
```

## Code Architecture

### Deployment Flow

**CLI Entry → WorkspaceDeployer → fabric-cicd**

1. **CLI Layer** (`cli.py`):
   - `parse_args()` → parses command-line arguments
   - `handle_deploy_command()` → creates `WorkspaceDeployer` instance and calls `deploy()`

2. **WorkspaceDeployer Initialization** (`workspace.py`):
   - Load `fabdep.yml` to get `workspace_base_name`
   - Get branch name (from git or `--branch` override)
   - Resolve workspace_id via Fabric API: `get_workspace_id_by_name(f"{base_name} [{branch}]")`
   - Call `ParameterDetector.detect_all()` and generate dynamic parameters
   - Detect lakehouse name from `.platform` files

3. **Deployment Execution** (`WorkspaceDeployer.deploy()`):
   - Apply item type filtering (`item_types`, `exclude_types`, `include_items`)
   - If item filtering active: regenerate parameters for filtered items only
   - Create `FabricWorkspace` instance (Microsoft's fabric-cicd)
   - Call `publish_all_items()` with retry logic for connection errors
   - Call `unpublish_all_orphan_items()` unless `no_cleanup=True`

## Key Implementation Details

### Auto-Detect (Default Behavior)

Auto-detect scans workspace files for source IDs and generates dynamic parameter replacements.

**How It Works**:
- `ParameterDetector.detect_all()` scans workspace files for source IDs (UUIDs, SQL endpoints, etc.)
- Generates dynamic variable replacements for Microsoft's fabric-cicd parameter system
- Automatically skips external connections (tenant-wide, not deployed by fabric-cicd)

**Detection Sources** (`detect.py`):
- **Notebooks**: `default_lakehouse_workspace_id` and `default_lakehouse` from metadata
- **SemanticModels**: SQL connection strings and endpoint IDs from `expressions.tmdl`
- **Pipelines**: Connection references from `pipeline-content.json`
- **Lakehouses**: Display names from `.platform` files

**Dynamic Variable Examples**:
```python
# Workspace ID → $workspace.$id
"fb61a71c-..." → "$workspace.$id"

# Lakehouse ID → $items.Lakehouse.<name>.$id
"74113cfe-..." → "$items.Lakehouse.my_lakehouse.$id"

# Lakehouse SQL Endpoint → $items.Lakehouse.<name>.$sqlendpoint
"ABC123...XYZ.datawarehouse.fabric.microsoft.com" → "$items.Lakehouse.my_lakehouse.$sqlendpoint"

# Lakehouse SQL Endpoint ID → $items.Lakehouse.<name>.$sqlendpointid
"dc24980a-..." → "$items.Lakehouse.my_lakehouse.$sqlendpointid"

# SQL Database Endpoint → $items.SQLDatabase.<name>.$sqlendpoint
"XYZ123...ABC.database.fabric.microsoft.com" → "$items.SQLDatabase.my_database.$sqlendpoint"

# SQL Database Name → $items.SQLDatabase.<name>.$name
"my_database-ba66912d-..." → "$items.SQLDatabase.my_database.$name"
```

**Parameter Filtering**:
When using `--item-types` or `--exclude-types`, parameters are regenerated to include only those used by targeted item types (prevents warnings about unused parameters).

### Feature Branch Support (`--skip-lakehouse` / `-L`)

For feature branches that should share the dev workspace's lakehouse data instead of getting their own:

```bash
fabdep -L  # Skip lakehouse parameter replacement
```

**What Gets Skipped**:
- Lakehouse ID → `$items.Lakehouse.<name>.$id`
- SQL Analytics Endpoint Connection String → `$items.Lakehouse.<name>.$sqlendpoint`
- Lakehouse SQL Analytics Endpoint Item ID → `$items.Lakehouse.<name>.$sqlendpointid`

**What Gets Kept**:
- Workspace ID → `$workspace.$id` (still needs replacement for feature branch workspace)
- SQL Database parameters
- External connection IDs

**Use Case**: When working on a feature branch, notebooks and semantic models keep their references to the dev lakehouse data. This avoids creating empty lakehouses in feature branch workspaces and eliminates GUID conflicts when merging back to dev.

### Manual Parameter Overrides

Override the **replace_value** and optionally scope to specific **item_type(s)** for auto-detected parameters that reference external resources.

**How It Works**:
1. **find_value** is still **auto-detected** from source files (e.g., SemanticModel expressions.tmdl)
2. **replace_value** is manually specified in `fabdep.yml` when you want to keep an external reference
3. **item_type** (optional) scopes the replacement to specific item types (e.g., `["SemanticModel"]`)

**Configuration Example**:
```yaml
workspace_base_name: "my_workspace"

manual_parameters:
  - find_value: "ffffffff-ffff-ffff-ffff-ffffffffffff"  # Auto-detected from expressions.tmdl
    replace_value: "ffffffff-ffff-ffff-ffff-ffffffffffff"  # Keep same (external reference)
    item_type: ["SemanticModel"]
    description: "External Translytical SQL Endpoint ID (not deployed)"
```

**Behavior**:
- Manual overrides are applied to auto-detected parameters only
- If find_value wasn't auto-detected, a warning is logged
- If `item_type` is specified, replacement is scoped to those item types only
- Overridden parameters are marked with "(manual override: description)" in logs

### Workspace Creation (`-N`)

Creates workspace if it doesn't exist: `{workspace_base_name} [{branch}]`
- Uses `capacity_id` from fabdep.yml, or auto-selects first active capacity

### Error Handling

**Automatic Retry**: Transient API errors (timeouts, 5xx, connection failures) retried automatically. Default: 1 retry, configurable with `--max-retries` / `-r`.

**Intelligent Error Messages** (`cli.py`):
- **Connection errors**: Detects DataPipeline/Dataflow connection references, explains tenant-wide GUIDs
- **Auth errors**: Shows current account and suggests `fabdep auth` commands
- **Config errors**: Missing fabdep.yml shows example configuration

## Module Architecture

### Core Modules

**`workspace.py`** - WorkspaceDeployer dataclass
- Main deployment orchestration logic
- Key methods: `_load_config()`, `_generate_dynamic_params()`, `_deploy_items()`, `deploy()`
- Wraps Microsoft's `FabricWorkspace` and `publish_all_items()`

**`detect.py`** - ParameterDetector class
- Scans workspace files for source IDs (UUIDs, SQL endpoints, connection strings)
- Generates dynamic variable mappings for fabric-cicd parameter system
- Methods: `detect_all()`, `generate_parameter_yml()`, `save_parameter_yml()`

**`api.py`** - Fabric REST API integration
- Authentication: `get_access_token()` via `az account get-access-token`
- Workspace operations: `list_workspaces()`, `get_workspace_id_by_name()`, `create_workspace()`
- Uses Python's built-in `urllib` (zero external dependencies)

**`cli.py`** - CLI interface (presentation layer only)
- Argument parsing: `parse_args()`
- Command handlers: `handle_deploy_command()`, `handle_auth_command()`, `handle_rename_command()`
- Error formatting and user-facing messages

**`auth.py`** - Azure authentication utilities
- Account management: `list_accounts()`, `switch_account()`, `rename_subscription()`

**`log.py`** - Logging and console formatting
- Custom logger with styled output
- Error detection: `is_auth_error()`, `is_connection_error()`, `is_transient_error()`

**`rename.py`** - Rename utility
- Rename workspace item directories to match `.platform` display names
- Supports dry-run and cleaning (lowercase + underscores)

### Dependencies

```toml
[project]
dependencies = [
    "fabric-cicd>=0.2.0",     # Microsoft's official deployment package
    "pyyaml",                 # fabdep.yml parsing
]
```

**Key Dependency**: `fabric-cicd` provides `FabricWorkspace`, `publish_all_items()`, `unpublish_all_orphan_items()`

## Programmatic Usage

```python
from fabric_deploy import WorkspaceDeployer

deployer = WorkspaceDeployer(repo_dir="/path/to/workspace")
deployer.deploy()

# With options
deployer.deploy(item_types=["Notebook", "DataPipeline"])

# New workspace with custom retries
deployer = WorkspaceDeployer(
    repo_dir="/path/to/workspace",
    new_workspace=True,
    max_retries=2
)
deployer.deploy()

# Feature branch mode (skip lakehouse replacement)
deployer = WorkspaceDeployer(
    repo_dir="/path/to/workspace",
    skip_lakehouse=True
)
deployer.deploy()
```

## Microsoft's fabric-cicd Package

**Documentation**: https://microsoft.github.io/fabric-cicd/

This package wraps Microsoft's `fabric-cicd` which provides:
- `FabricWorkspace(workspace_id, repository_directory, environment, item_type_in_scope)`
- `publish_all_items(workspace)` - Deploys items to Fabric
- `unpublish_all_orphan_items(workspace)` - Cleans up orphaned items

**Supported Item Types**: DataPipeline, Environment, Notebook, Report, SemanticModel, Lakehouse, MirroredDatabase, Warehouse, SQLDatabase, Eventhouse, KQLDatabase, Dataflow, and more.

## Configuration Files

**fabdep.yml** (required in workspace root):
```yaml
workspace_base_name: "my_workspace"  # Combined with git branch: "my_workspace [main]"
capacity_id: "optional-capacity-id"  # Auto-detected if omitted

# Optional: Override auto-detected parameters (e.g., external references)
manual_parameters:
  - find_value: "auto-detected-uuid"
    replace_value: "keep-same-or-change"
    item_type: ["SemanticModel"]  # Optional: scope to specific item types
    description: "External lakehouse SQL endpoint"
```

**parameter.yml** (auto-generated):
- Auto-generated from workspace file scans during deployment
- Use `fabdep detect --save` to save to `parameter.yml.detected` for inspection