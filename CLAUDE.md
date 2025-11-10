# CLAUDE.md

## Project Overview

`fabric-deploy` is a **thin CLI wrapper** around Microsoft's official [`fabric-cicd`](https://github.com/microsoft/fabric-cicd) package. It provides workspace-based deployment using workspace name + git branch resolution via Fabric REST API.

## Purpose

- **Wrapper**: Thin CLI layer over Microsoft's `fabric-cicd` package
- **Workspace Resolution**: Combines workspace base name + git branch to resolve workspace ID via Fabric API
- **Dynamic Parameter Generation**: Auto-detect source IDs and generate parameter.yml with dynamic variables on-the-fly
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
├── SMART_REPLACE.md           # Dynamic parameter generation documentation
└── README.md                  # Brief usage documentation
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
pip install fabric-deploy  # Installs Microsoft's fabric-cicd as dependency
cd path/to/workspace
fabdep  # Auto-resolves workspace from base name + git branch
```

## Core Workflow

1. **Create fabdep.yml** with workspace base name
2. **Run fabdep** (optionally with --branch, --auto-detect, filtering flags)
3. **Tool resolves workspace_id** from base_name + branch via Fabric API
4. **Deploy** to target workspace

### Example

```bash
# Create fabdep.yml in workspace directory
echo 'workspace_base_name: "my_workspace"' > fabdep.yml

# Deploy to workspace resolved from git branch
fabdep

# Deploy to specific branch
fabdep --branch prod  # Deploys to "my_workspace [prod]"

# Deploy with auto-detected parameters (no parameter.yml needed)
fabdep --auto-detect

# Deploy with filters
fabdep -t Notebook DataPipeline --no-cleanup
```

## Configuration

**fabdep.yml** (required):
```yaml
workspace_base_name: "my_workspace"
capacity_id: "12345-67890-abcd-..."  # Optional, auto-detected if not specified

# Optional: Manual parameter overrides (replace_value only, find_value is auto-detected)
manual_parameters:
  - find_value: "ffffffff-ffff-ffff-ffff-ffffffffffff"  # Auto-detected from expressions.tmdl
    replace_value: "ffffffff-ffff-ffff-ffff-ffffffffffff"  # Keep same (external lakehouse)
    item_type: ["SemanticModel"]
    description: "External Translytical SQL Endpoint ID"
```

**How workspace resolution works**:
1. Base name from fabdep.yml: `my_workspace`
2. Branch from git or --branch flag: `prod`
3. Combined workspace name: `my_workspace [prod]`
4. Workspace ID lookup via Fabric REST API
5. Deployment proceeds with resolved workspace ID

**Why fabdep.yml?**
- fabric-deploy specific configuration
- Separate from parameter.yml which is read by Microsoft's fabric-cicd
- Enables automatic workspace resolution based on branch
- Supports any branch name (prod, dev, staging, feature branches, etc.)

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
4. If `--auto-detect` flag is set: generate parameter.yml dynamically
5. Detect lakehouse name from `.platform` files

### WorkspaceDeployer.deploy() Flow
1. Apply item type filtering (`item_types`, `exclude_types`, `include_items`)
2. If auto-detect mode + item filtering: regenerate parameter.yml with filtered parameters (prevents warnings about unused parameters)
3. Create `FabricWorkspace` instance (from Microsoft's package)
4. Call `publish_all_items()` with optional filtering
5. Optionally call `unpublish_all_orphan_items()` unless `no_cleanup=True`

## Key Features

### Manual Parameter Overrides

Override the **replace_value** and optionally scope to specific **item_type(s)** for auto-detected parameters that reference external resources.

**How It Works**:
1. **find_value** is still **auto-detected** from source files (e.g., SemanticModel expressions.tmdl)
2. **replace_value** is manually specified in `fabdep.yml` when you want to keep an external reference
3. **item_type** (optional) scopes the replacement to specific item types (e.g., `["SemanticModel"]`)

**Use Cases**:
- External lakehouse SQL endpoint IDs (from workspaces not being deployed)
- Items that reference resources outside the current workspace
- SQL endpoint IDs that should stay the same across deployments
- Custom connection strings that shouldn't use dynamic variables

**Example Scenario**:
Your SemanticModel references a SQL endpoint ID `ffffffff-ffff-ffff-ffff-ffffffffffff` from an external "Translytical" lakehouse. The ID is auto-detected from `expressions.tmdl`, but you want to keep it the same (not replace it with the local lakehouse).

**Configuration**:
```yaml
workspace_base_name: "my_workspace"

manual_parameters:
  - find_value: "ffffffff-ffff-ffff-ffff-ffffffffffff"  # Auto-detected from expressions.tmdl
    replace_value: "ffffffff-ffff-ffff-ffff-ffffffffffff"  # Keep same (external reference)
    item_type: ["SemanticModel"]
    description: "External Translytical SQL Endpoint ID (not deployed)"
  
  - find_value: "old-server.database.windows.net"  # Auto-detected from pipeline
    replace_value: "new-server.database.windows.net"  # Custom replacement
    item_type: ["DataPipeline"]
    description: "Production SQL Server connection"
```

**Behavior**:
- Manual overrides are applied to auto-detected parameters only
- If find_value wasn't auto-detected, a warning is logged
- If `item_type` is specified, replacement is scoped to those item types only
- If `item_type` is omitted, the auto-detected item types are preserved
- Overridden parameters are marked with "(manual override: description)" in logs
- Logged as "Applied X manual parameter override(s)" in deployment summary

### Dynamic Parameter Generation (--auto-detect)

Auto-detect source IDs from workspace files and generate parameter.yml on-the-fly with dynamic variables.

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
When using `--item-types` or `--exclude-types` with auto-detect mode, parameters are automatically filtered to only include those used by the targeted item types. This prevents warnings about unused parameters.

For example, if deploying only Notebooks (`-t Notebook`), parameters scoped to SemanticModel or Report won't be included in the generated parameter.yml.

**Example Scenario**:
```
# Detected parameters before filtering:
1. Workspace ID "fb61a71c..." → item_types: [Notebook, DataPipeline, SemanticModel]
2. Lakehouse ID "74113cfe..." → item_types: [Notebook, DataPipeline, SemanticModel]
3. SQL Endpoint "EXAMPLEA..." → item_types: [SemanticModel]
4. External Connection GUID → item_types: [DataPipeline]

# Deploy with: fabdep -d -t Notebook
# Generated parameter.yml includes only:
- Workspace ID (used by Notebook)
- Lakehouse ID (used by Notebook)

# SQL Endpoint and External Connection filtered out (not used by Notebook)
```

**Usage**:
```bash
fabdep --auto-detect              # Generate parameters on-the-fly
fabdep -d --save-params           # Save generated parameters to parameter.yml.detected
fabdep -d -t Notebook DataPipeline  # Auto-detect with filtering (parameters filtered to these types)
fabdep -d -x Report SemanticModel  # Auto-detect excluding types (parameters for excluded types not included)
```

### Lakehouse Detection

Automatically detects lakehouse name from `.platform` files for orphan cleanup exclusion.

### Item Filtering

Deploy specific item types or exclude types:
```bash
fabdep -t Notebook DataPipeline              # Deploy only notebooks and pipelines
fabdep -x Report SemanticModel               # Exclude reports and semantic models
fabdep --include-items "MyNotebook.Notebook" # Deploy specific items (experimental)
```

### Rename Utility

Rename workspace item directories to match display names:
```bash
fabdep rename           # Dry-run preview
fabdep rename -e        # Execute renames
fabdep rename -e -c     # Execute with cleaning (lowercase + underscores)
```

### Authentication Management

Manage Azure accounts:
```bash
fabdep auth list                    # Show current + all available accounts
fabdep auth switch <sub-id>         # Switch to different subscription
fabdep auth rename <sub-id> <name>  # Rename subscription (auto-switches if needed)
```

### Bootstrap Mode (--bootstrap)

Deploy to blank workspaces or create workspaces from scratch with two-phase deployment.

**Problem**: Notebooks and DataPipelines reference lakehouse IDs that don't exist yet in a blank workspace. This creates a chicken-and-egg problem where dynamic variables can't resolve.

**Solution**: Bootstrap mode deploys foundational items first (Phase 1), then deploys everything else (Phase 2).

**Foundational Items** (Phase 1):
- Lakehouse
- SemanticModel
- SQLDatabase
- Warehouse
- MirroredDatabase
- Eventhouse

**Dependent Items** (Phase 2):
- Notebook
- DataPipeline
- Environment
- Report
- All other item types

**Workflow**:
```bash
# Option 1: Deploy to existing blank workspace
fabdep --bootstrap

# Option 2: Create workspace first, then bootstrap deploy
fabdep --bootstrap --create-workspace

# Option 3: Auto-detect + bootstrap (recommended for new workspaces)
fabdep -d --bootstrap
```

**Workspace Creation**:
- Optional `capacity_id` in fabdep.yml:
  ```yaml
  workspace_base_name: "my_workspace"
  capacity_id: "12345-67890-abcd-..."  # Optional, uses first active capacity if omitted
  ```
- If `capacity_id` is not specified, automatically uses the first active capacity the user has access to
- Workspace name format: `{workspace_base_name} [{branch}]`
- Checks if workspace exists before creating
- Skips creation with info message if workspace already exists

**Connection GUIDs**:
- Bootstrap mode detects external connection GUIDs in DataPipelines/Dataflows
- Warns user that connection GUIDs are tenant-wide and must be configured manually
- Connection GUIDs are NOT deployed by fabric-cicd (they're tenant-wide references)
- User must configure connections in target workspace manually

**User Experience**:
```
[info] Bootstrap mode enabled - two-phase deployment
[info] Phase 1: Deploying foundational items
[info] Foundational item types: Lakehouse, SemanticModel
[info] ✓ Phase 1 complete
[info] Phase 2: Deploying all items
[info] Item types: Notebook, DataPipeline, Environment
[info] ✓ Phase 2 complete
[info] Deployment complete!

[warn] Found 3 external connection GUID(s) in DataPipelines/Dataflows
[warn] Connection GUIDs are tenant-wide and must be configured manually in the target workspace
[warn] Run 'fabdep detect --save' to see detected connection GUIDs
```

### Automatic Retry Logic

Deployment failures from connection issues are automatically retried to handle transient network errors.

**Behavior**:
- Retries up to 3 times for connection errors (`Connection aborted`, `RemoteDisconnected`)
- 2-second delay between retry attempts
- Logs warning message with attempt count: `Connection error on attempt 1/3: ...`
- After 3 failed attempts, raises the error normally
- Other error types (auth, validation, etc.) fail immediately without retry

**Example Output**:
```
[warn] Connection error on attempt 1/3: ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))
[info] Retrying in 2 seconds...
```

### Smart Item Type Display

When using item type filters (`--item-types` or `--exclude-types`), only displays item types that are both in scope AND present in the repository.

**Before**:
```
Item types in scope: Notebook, DataPipeline, Report, SemanticModel, Lakehouse, Environment
```

**After** (when only Notebook and DataPipeline exist in repo):
```
Item types in scope: DataPipeline, Notebook
```

## Dependencies

```toml
[project]
dependencies = [
    "fabric-cicd>=0.1.29",    # Microsoft's official package for deployment
    "pyyaml",                 # For fabdep.yml parsing
]
```
</text>

<old_text line=370>
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
- `auto_detect`: Enable dynamic parameter generation
- `save_detected_params`: Save generated parameters to file

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
- `parse_args()`: Configure argument parsers (auth, rename, detect, deploy)
- `handle_deploy_command()`: Execute deployment with auto-detect support
- `handle_auth_command()`: Execute auth subcommands
- `handle_rename_command()`: Execute rename utility
- `handle_detect_command()`: Execute parameter detection
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

## CLI Commands

### Deployment (default command)

```
fabdep [deploy] [DIRECTORY] [OPTIONS]

Arguments:
  DIRECTORY              Path to workspace directory (default: current directory)

Options:
  --no-cleanup, -n       Skip unpublishing orphaned items
  --item-types, -t       Deploy specific types (e.g., Notebook DataPipeline)
  --exclude-types, -x    Exclude types (e.g., Report SemanticModel)
  --include-items, -i    Include specific items by name (experimental)
  --auto-detect, -d      Auto-detect source IDs and generate parameters on-the-fly
  --save-params          Save auto-detected parameters to parameter.yml.detected
  --branch, -b           Override git branch name for workspace resolution
  --bootstrap            Enable two-phase deployment (foundational items first, then all items)
  --create-workspace     Create workspace if it doesn't exist (uses default capacity if not specified)
  --help                 Show help message
```

**Examples**:
```bash
fabdep                              # Deploy using git branch
fabdep --branch prod                # Deploy to "workspace_base_name [prod]"
fabdep -d                           # Auto-detect parameters
fabdep -t Notebook DataPipeline     # Deploy specific types
fabdep -x Report SemanticModel      # Exclude types
fabdep --no-cleanup                 # Skip orphan cleanup

# Bootstrap mode (for blank workspaces)
fabdep --bootstrap                  # Two-phase deployment
fabdep --bootstrap --create-workspace  # Create workspace first, then deploy
fabdep -d --bootstrap               # Auto-detect + bootstrap (recommended for new workspaces)
```

### Detect Parameters

```
fabdep detect [DIRECTORY] [OPTIONS]

Arguments:
  DIRECTORY              Path to workspace directory (default: current directory)

Options:
  --save, -s             Save detected parameters to parameter.yml.detected
  --help                 Show help message
```

### Rename Items

```
fabdep rename [DIRECTORY] [OPTIONS]

Arguments:
  DIRECTORY              Path to workspace directory (default: current directory)

Options:
  --execute, -e          Actually perform renames (default is dry-run)
  --clean, -c            Apply .lower().replace(' ', '_') to directory names
  --help                 Show help message
```

### Authentication Management

```
fabdep auth <command>

Commands:
  list                   List current and all available Azure accounts
  switch <sub-id>        Switch to different subscription
  rename <sub-id> <name> Rename a subscription
```

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

## Development

```bash
# Install dependencies (requires uv)
uv sync

# Build distribution
uv build

# Install locally
pip install -e .

# Test CLI
cd /path/to/workspace
fabdep --no-cleanup
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

# With auto-detect mode and branch override
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

## Troubleshooting

**"fabdep.yml not found"**
- Create `fabdep.yml` in workspace root with `workspace_base_name: "your_workspace"`

**"No workspace_base_name found in fabdep.yml"**
- Ensure fabdep.yml contains the `workspace_base_name` field

**"Workspace not found: 'workspace_name'"**
- Verify workspace exists in Fabric with name: `{base_name} [{branch}]`
- Run `fabdep auth list` to verify Azure account
- Check workspace access permissions

**"Failed to get git branch"**
- Ensure running from git repository (not detached HEAD state)
- Use `--branch` flag to override git branch detection

**"No lakehouse found in repository directory"**
- Ensure at least one `*.Lakehouse` directory exists with `.platform` file

**"User does not have access to the connection used in the Pipeline"**
- Connection GUIDs are tenant-wide (not workspace-specific)
- Edit pipeline files directly to update connection GUIDs
- Run `fabdep detect` to discover current connection GUIDs
- DO NOT use parameter.yml for connection GUIDs

**Authentication/Authorization Errors**
- Run `fabdep auth list` to see accounts
- Switch with `fabdep auth switch <subscription-id>`

## Related

- **[microsoft/fabric-cicd](https://github.com/microsoft/fabric-cicd)**: The underlying library
- **Workspace repos**: Use this CLI tool for deployments
- **example-fabric-etl**: Core Python ETL framework (separate package)
