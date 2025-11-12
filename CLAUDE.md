# CLAUDE.md

## Project Overview

`fabric-deploy` is a **thin CLI wrapper** around Microsoft's official [`fabric-cicd`](https://github.com/microsoft/fabric-cicd) package. It provides workspace-based deployment using workspace name + git branch resolution via Fabric REST API.

## Purpose

- **Wrapper**: Thin CLI layer over Microsoft's `fabric-cicd` package
- **Workspace Resolution**: Combines workspace base name + git branch to resolve workspace ID via Fabric API
- **Dynamic Parameter Generation**: Auto-detect source IDs and generate parameter.yml with dynamic variables (enabled by default)
- **CLI convenience**: Simple command-line interface for deployments
- **Smart Replace Support**: Handles dynamic parameterization with auto-generated variables

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

## Usage Pattern

**Old way** (inline cicd.py in workspace repo):
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

**New way** (CLI wrapper):
```bash
pip install -e .  # Installs Microsoft's fabric-cicd as dependency
cd path/to/workspace
fabdep  # Auto-resolves workspace from base name + git branch, auto-detects parameters
```

## Deployment Flow

### CLI Flow
1. `cli.py`: `parse_args()` parses command-line arguments
2. `cli.py`: `main()` creates `WorkspaceDeployer` dataclass instance
3. `workspace.py`: `WorkspaceDeployer.__post_init__()` loads configuration
4. `workspace.py`: `WorkspaceDeployer.deploy()` orchestrates deployment

### WorkspaceDeployer Initialization Flow
1. Load fabdep.yml to get `workspace_base_name`
2. Get branch name (from git or `--branch` override)
3. Resolve workspace_id via Fabric API: `get_workspace_id_by_name(f"{base_name} [{branch}]")`
4. If auto-detect enabled (default): generate parameter.yml dynamically
5. Detect lakehouse name from `.platform` files

### WorkspaceDeployer.deploy() Flow
1. Apply item type filtering (`item_types`, `exclude_types`, `include_items`)
2. If auto-detect mode + item filtering: regenerate parameter.yml with filtered parameters (prevents warnings about unused parameters)
3. Create `FabricWorkspace` instance (from Microsoft's package)
4. Call `publish_all_items()` with optional filtering
5. Optionally call `unpublish_all_orphan_items()` unless `no_cleanup=True`

## Key Implementation Details

### Auto-Detect (Default Behavior)

Auto-detect is **enabled by default**. Users can opt-out with `--no-auto-detect` to use an existing `parameter.yml` file.

**Detection Sources**:
- **Notebook Metadata**: Extracts `default_lakehouse_workspace_id` and `default_lakehouse`
- **SemanticModel Files**: Parses `expressions.tmdl` for SQL connection strings and endpoint IDs
- **Pipeline JSON**: Scans `pipeline-content.json` for connection references
- **Lakehouse Metadata**: Reads `.platform` files for display names

**Dynamic Variable Generation**:
```python
# Workspace ID → $workspace.$id
"fb61a71c-b2af-49dc-9150-9040010c79bb" → "$workspace.$id"

# Lakehouse ID → $items.Lakehouse.<name>.$id
"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" → "$items.Lakehouse.my_lakehouse.$id"

# SQL Connection → $items.Lakehouse.<name>.$sqlendpoint
"EXAMPLEABC123XYZ" → "$items.Lakehouse.my_lakehouse.$sqlendpoint"
```

**Auto-Skip Logic**:
- **External Connections**: Parameters without dynamic variables (e.g., external connection IDs) are skipped because they're tenant-wide and require manual setup
- External connections are NOT deployed by fabric-cicd and their IDs don't change between workspaces

**Parameter Filtering with Item Types**:
When using `--item-types` or `--exclude-types`, parameters are automatically filtered to only include those used by the targeted item types. This prevents warnings about unused parameters.

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

### Bootstrap Mode

Two-phase deployment for blank workspaces:

**Foundational Items** (Phase 1):
- Lakehouse, SemanticModel, SQLDatabase, Warehouse, MirroredDatabase, Eventhouse

**Dependent Items** (Phase 2):
- Notebook, DataPipeline, Environment, Report, and all other item types

**Workspace Creation**:
- Optional `capacity_id` in fabdep.yml
- If not specified, automatically uses the first active capacity the user has access to
- Workspace name format: `{workspace_base_name} [{branch}]`
- Checks if workspace exists before creating

**Connection GUIDs**:
- Bootstrap mode detects external connection GUIDs in DataPipelines/Dataflows
- Warns user that connection GUIDs are tenant-wide and must be configured manually
- Connection GUIDs are NOT deployed by fabric-cicd (they're tenant-wide references)

### Automatic Retry Logic

Deployment failures from connection issues are automatically retried to handle transient network errors.

**Behavior**:
- Retries up to 3 times for connection errors (`Connection aborted`, `RemoteDisconnected`)
- 2-second delay between retry attempts
- Logs warning message with attempt count: `Connection error on attempt 1/3: ...`
- After 3 failed attempts, raises the error normally
- Other error types (auth, validation, etc.) fail immediately without retry

### Smart Item Type Display

When using item type filters, only displays item types that are both in scope AND present in the repository.

## Implementation Details

### Key Modules

**`api.py` - Fabric REST API integration**
- `get_access_token()`: Acquires Azure access token via `az account get-access-token`
- `list_workspaces()`: Returns all accessible workspaces
- `get_workspace_id_by_name(workspace_name)`: Case-insensitive workspace lookup
- Uses Python's built-in `urllib` (no external dependencies)

**`workspace.py` - WorkspaceDeployer dataclass**
- `repo_dir`: Workspace directory path (required)
- `logger_name`: Optional custom logger name
- `branch_override`: Optional branch override (from --branch flag)
- `auto_detect`: Enable dynamic parameter generation (default: True)
- `save_detected_params`: Save generated parameters to file
- `bootstrap`: Enable two-phase deployment
- `create_workspace`: Create workspace if it doesn't exist

**Key Methods**:
- `_load_config()`: Load fabdep.yml and resolve workspace_id via API
- `_get_git_branch()`: Get current git branch name
- `_resolve_workspace_id_from_name()`: Combine base_name + branch and lookup via API
- `_get_repo_item_types()`: Scan repository to detect which item types are present
- `_detect_lakehouse()`: Auto-detect lakehouse name from `.platform` files
- `_generate_dynamic_params()`: Auto-detect source IDs and generate parameter.yml
- `_deploy_items()`: Deploy items to workspace with retry logic for connection errors
- `deploy()`: Main deployment method

**`detect.py` - ParameterDetector class**
- `detect_all()`: Scan workspace files and detect source IDs
- `generate_parameter_yml()`: Create parameter.yml structure with dynamic variables
- `save_parameter_yml()`: Write parameter.yml to file
- `print_detection_summary()`: Display detected parameters with dynamic variables

**`cli.py` - CLI interface**
- `parse_args()`: Configure argument parsers (auth, rename, deploy)
- `handle_deploy_command()`: Execute deployment with auto-detect support
- `handle_auth_command()`: Execute auth subcommands
- `handle_rename_command()`: Execute rename utility
- `main()`: CLI entry point - routes to appropriate handler

**`auth.py` - Azure auth utilities**
- `list_accounts()`: List all Azure accounts
- `get_current_account()`: Get currently active account
- `switch_account()`: Switch to different subscription
- `rename_subscription()`: Rename subscription (auto-switches if needed)
- `is_connection_error()`: Detect connection reference errors
- `is_auth_error()`: Detect authentication/authorization errors

**`log.py` - Logging and formatting**
- `get_logger()`: Configure logger with custom formatter
- `print_header()`: Print styled header
- `print_error()`: Print error message
- `format_account_list()`: Format Azure accounts with current marker
- `format_auth_error_message()`: Format auth error with troubleshooting tips
- `format_connection_error_message()`: Format connection error message

**`rename.py` - Rename utility**
- `rename_items_by_display_name()`: Rename workspace item directories to match display names
- Supports dry-run mode and cleaning (lowercase + underscores)

## Error Handling

**Automatic Retry Logic**:
- Connection errors (`Connection aborted`, `RemoteDisconnected`) are automatically retried up to 3 times
- 2-second delay between retry attempts
- Logs warning on each retry with attempt count
- Other errors (auth, config, etc.) fail immediately without retry

**Intelligent error detection**:
- **Connection errors**: Detects DataPipeline/Dataflow connection references, explains tenant-wide GUIDs must be edited in source files
- **Auth errors**: Detects authentication failures, displays account info and suggests auth commands
- **Config errors**: Missing fabdep.yml triggers helpful error with example configuration

**Validation**:
- Workspace directory exists
- fabdep.yml exists with `workspace_base_name` field
- Git repository available with valid branch
- Lakehouse `.platform` files readable

## Dependencies

```toml
[project]
dependencies = [
    "fabric-cicd>=0.1.29",    # Microsoft's official package for deployment
    "pyyaml",                 # For fabdep.yml parsing
]
```

## Programmatic Usage

**Note**: Primary interface is the CLI (`fabdep` command). Programmatic API is minimal and designed for Azure DevOps integration and testing.

```python
from fabric_deploy import WorkspaceDeployer

# Basic deployment (reads fabdep.yml from repo_dir)
deployer = WorkspaceDeployer(repo_dir="/path/to/workspace")
deployer.deploy()

# With item filtering
deployer = WorkspaceDeployer(repo_dir="/path/to/workspace")
deployer.deploy(item_types=["Notebook", "DataPipeline"], no_cleanup=False)

# Disable auto-detect (use existing parameter.yml)
deployer = WorkspaceDeployer(
    repo_dir="/path/to/workspace",
    auto_detect=False,
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

## Microsoft's fabric-cicd Package

**Documentation**: https://microsoft.github.io/fabric-cicd/

**Key Classes/Functions**:
- `FabricWorkspace(workspace_id, repository_directory, environment, item_type_in_scope)`
- `publish_all_items(workspace)` - Deploys items to Fabric
- `unpublish_all_orphan_items(workspace)` - Cleans up orphaned items

**Supported Item Types**:
- DataPipeline, Environment, Notebook, Report, SemanticModel
- Lakehouse, MirroredDatabase, VariableLibrary, CopyJob
- Eventhouse, KQLDatabase, KQLQueryset, Reflex, Eventstream
- Warehouse, SQLDatabase, KQLDashboard, Dataflow, GraphQLApi
- ApacheAirflowJob, MountedDataFactory