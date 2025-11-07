# CLAUDE.md

## Project Overview

`fabric-deploy` is a **thin CLI wrapper** around Microsoft's official [`fabric-cicd`](https://github.com/microsoft/fabric-cicd) package. It auto-detects workspace configuration from `parameter.yml` files, eliminating per-workspace `cicd.py` scripts.

## Purpose

- **Wrapper**: Thin CLI layer over Microsoft's `fabric-cicd` package
- **Auto-detection**: Reads `parameter.yml` to extract workspace ID and environment
- **Dynamic Parameter Generation**: Auto-detect source IDs from workspace files and generate parameter.yml with dynamic variables
- **CLI convenience**: Replace inline Python scripts with a simple command
- **Smart Replace Support**: Handles both legacy and modern parameterization formats

## Architecture

```
fabric-deploy/
├── src/fabric_deploy/         # Package directory
│   ├── __init__.py            # Package initialization, exports all public classes
│   ├── workspace.py           # WorkspaceDeployer class - deployment business logic
│   ├── param_detector.py      # ParameterDetector class - auto-detect source IDs
│   ├── cli.py                 # ALL CLI logic - parsers, handlers, user-facing text
│   ├── auth.py                # Azure auth utilities - business logic only
│   ├── logging.py             # Custom logging & console formatting utilities
│   ├── rename.py              # Rename utility - business logic only
│   └── types.py               # Type definitions
├── pyproject.toml             # Package configuration, depends on fabric-cicd
├── example_parameter.yml      # Example configuration file
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
fabdep  # Auto-detects workspace_id and environment from parameter.yml
```

## Deployment Flow

### CLI Flow
1. `cli.py`: `parse_args()` parses command-line arguments
2. `cli.py`: `main()` creates `WorkspaceDeployer` dataclass instance with custom logger name
3. `workspace.py`: `WorkspaceDeployer.__post_init__()` loads configuration
4. `workspace.py`: `WorkspaceDeployer.deploy()` orchestrates deployment

### WorkspaceDeployer Initialization Flow (`__post_init__`)
1. If `auto_detect=True`: calls `_generate_dynamic_params()` to create parameter file on-the-fly
2. If `workspace_id` is None: calls `_load_config()` which:
   - Calls `_validate_and_handle_parameter_file()` to check for invalid parameter files
   - Automatically renames invalid parameter files to `.bak` extension
   - Extracts **workspace ID** from first entry's `replace_value[environment]`
   - Extracts **environment** from keys in `replace_value` (must be consistent across all entries)
   - Sets `self.workspace_id` and `self.environment`
3. If `workspace_id` is provided (via CLI) but `environment` is None: defaults `environment` to "prod"
4. If `lakehouse_name` is not set: calls `_detect_lakehouse()` which:
   - Searches for `*.Lakehouse` directories
   - Reads `.platform` metadata file to extract `displayName`
   - Sets `self.lakehouse_name`

### WorkspaceDeployer.deploy() Flow
1. Applies item type filtering based on arguments (`item_types`, `exclude_types`, `include_items`)
2. Creates `FabricWorkspace` instance (from Microsoft's package)
3. Calls `publish_all_items()` with optional item filtering (from Microsoft's package)
4. Optionally calls `unpublish_all_orphan_items()` (from Microsoft's package) unless `no_cleanup=True`

## What This Package Provides

### Core Features
- **Dynamic Parameter Generation**: Auto-detect source IDs and generate parameter.yml with dynamic variables
- **Deployment CLI**: `fabdep` command with auto-detection from `parameter.yml`
- **Lakehouse Detection**: Auto-detects lakehouse names from `.platform` files
- **Parameter Validation**: Validates and handles both legacy and smart replace formats
- **Item Filtering**: Deploy specific item types or exclude types
- **Rename Utility**: Rename workspace item directories to match display names
- **User-friendly Output**: Formatted console output and error messages

### ParameterDetector Class

The `ParameterDetector` class (in `param_detector.py`) auto-detects source IDs from workspace repository files and generates parameter.yml with dynamic variables.

**Detection Sources**:
- **Notebook Metadata**: Extracts `default_lakehouse_workspace_id` and `default_lakehouse` from notebook-content.py files
- **SemanticModel Files**: Parses `expressions.tmdl` to extract SQL connection strings and endpoint IDs
- **Pipeline JSON**: Scans pipeline-content.json for external connection references
- **Lakehouse Metadata**: Reads `.platform` files to get lakehouse display names for variable generation

**Dynamic Variable Generation**:
```python
# Workspace ID → $workspace.$id
"fb61a71c-b2af-49dc-9150-9040010c79bb" → "$workspace.$id"

# Lakehouse ID → $items.Lakehouse.<name>.$id
"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" → "$items.Lakehouse.my_lakehouse.$id"

# SQL Connection → $items.Lakehouse.<name>.$sqlendpoint
"EXAMPLEABC123XYZ" → "$items.Lakehouse.my_lakehouse.$sqlendpoint"
```

**Key Methods**:
- `detect_all()`: Scans all workspace files and returns detected parameters
- `generate_parameter_yml()`: Creates parameter.yml structure with dynamic variables
- `save_parameter_yml()`: Writes parameter.yml to file with YAML formatting
- `print_detection_summary()`: Displays detected parameters with dynamic variables

**Auto-Skip Logic**: 
1. **Same Workspace Detection**: When auto-detect mode is used and the provided `--workspace-id` matches the source workspace ID detected in notebooks, no parameter file is generated. The deployment proceeds directly without any replacements since source and destination are identical.
2. **External Connections**: Parameters without dynamic variables (e.g., external connection IDs) are automatically skipped because external connections are not deployed by fabric-cicd (manual setup required), and connection IDs don't change between source and destination.

**Auto-Detect Mode**: Use `-d -w <workspace-id>` to deploy without a parameter.yml file. This will:
1. Scan workspace files to detect source IDs (workspace, lakehouse, SQL endpoints)
2. Generate parameter.yml with dynamic variables (`$workspace.$id`, `$items.Lakehouse.<name>.$id`)
3. Deploy to target workspace (fabric-cicd resolves IDs automatically)
4. Clean up generated files after deployment



## Dependencies

```toml
[project]
dependencies = [
    "fabric-cicd>=0.1.29",    # Microsoft's official package for deployment
    "pyyaml",                 # For parameter.yml parsing
]
```

## Configuration

**parameter.yml** (in workspace repo):
```yaml
find_replace:
  # First entry's replace_value is used to detect workspace ID and environment
  - find_value: "364abdbe-c061-4f74-b34a-9631bc7e3e4d"
    replace_value:
      prod: "6c5304f1-2e84-4add-bd46-25a1e943cdec"  # CLI uses this as workspace_id
    item_type: ["Notebook", "CopyJob"]

  # Additional find/replace entries for parameter replacement
  - find_value: "dev-lakehouse-id"
    replace_value:
      prod: "prod-lakehouse-id"  # All entries must use the same environment key
    item_type: ["Notebook", "SemanticModel"]
```

**Important**: All `replace_value` entries must use the same environment key (e.g., all use `prod` or all use `dev`). The CLI validates this consistency and extracts the destination `workspace_id` from the first entry.

### Parameter File Validation

The CLI automatically validates parameter files and detects invalid configurations:

**Invalid scenarios** (triggers automatic `.bak` renaming):
- File already has `.bak` extension
- Duplicate `find_value` entries (same value appears multiple times)
- Blank or empty destination values in `replace_value` (e.g., `prod: ""` or `prod: null`)

**Behavior when invalid**:
1. CLI logs warning messages explaining why the file is invalid
2. Renames `parameter.yml` to `parameter.yml.bak`
3. Deployment continues using workspace ID from first entry only
4. Microsoft's `fabric-cicd` package ignores the `.bak` file (preventing parameter replacement)

**Example invalid parameter.yml**:
```yaml
find_replace:
  - find_value: "source-workspace-id"
    replace_value:
      prod: "dest-workspace-id"  # Valid workspace ID
    item_type: ["Notebook"]
  
  - find_value: "source-lakehouse-id"
    replace_value:
      prod: ""  # INVALID: blank destination value
    item_type: ["Notebook"]
  
  - find_value: "source-workspace-id"  # INVALID: duplicate find_value
    replace_value:
      prod: "another-id"
    item_type: ["DataPipeline"]
```

This file would be automatically renamed to `parameter.yml.bak`, and deployment would proceed with just the workspace ID from the first entry.

## Implementation Details

### WorkspaceDeployer Dataclass
The main deployment logic is encapsulated in the `WorkspaceDeployer` dataclass:

```python
# cli.py
from dataclasses import dataclass, field
from fabric_cicd import FabricWorkspace, publish_all_items, unpublish_all_orphan_items

@dataclass
class WorkspaceDeployer:
    """Main deployer class for Fabric workspace items."""
    
    repo_dir: Path | str
    logger_name: str | None = None
    
    # Configuration populated by _load_config()
    workspace_id: str | None = field(default=None, init=False)
    environment: str | None = field(default=None, init=False)
    lakehouse_name: str | None = field(default=None, init=False)
    
    # Private fields (initialized in __post_init__)
    _repo_dir_resolved: Path = field(init=False, repr=False)
    logger: logging.Logger = field(init=False, repr=False)
    
    def __post_init__(self):
        """Initialize derived fields after dataclass initialization."""
        self._repo_dir_resolved = Path(self.repo_dir).resolve()
        self.logger = get_logger(self.logger_name or __name__)
    
    def _load_config(self) -> None:
        """Load and validate workspace configuration from parameter.yml."""
        # Validate parameter file and rename to .bak if invalid
        # Extract workspace_id and environment
    
    def deploy(
        self,
        no_cleanup: bool = False,
        item_types: list[str] | None = None,
        exclude_types: list[str] | None = None,
        include_items: list[str] | None = None,
    ) -> None:
        """Deploy Fabric workspace items."""
        # Load config if not already loaded
        if not self.workspace_id:
            self._load_config()
        
        # Apply item type filtering
        # Create FabricWorkspace
        # Call publish_all_items and unpublish_all_orphan_items

def parse_args():
    """Parse command-line arguments."""
    # Configure argument parser with subparsers for 'auth' and 'deploy'
    # Automatically injects 'deploy' as default when deploy-specific flags are used
    return parser.parse_args()

def main():
    """CLI entry point."""
    args = parse_args()
    deployer = WorkspaceDeployer(
        repo_dir=args.directory,
        logger_name="fabric_deploy.cli"
    )
    deployer.deploy(
        no_cleanup=args.no_cleanup,
        item_types=args.item_types,
        exclude_types=args.exclude_types,
        include_items=args.include_items,
    )
```

### Key Components

**`workspace.py` - Workspace abstraction module**
Contains the `WorkspaceDeployer` dataclass:
- Main dataclass encapsulating all deployment logic
- Dataclass fields: `repo_dir` (required), `logger_name` (optional)
- Configuration attributes: `workspace_id`, `environment`, `lakehouse_name` (populated by `_load_config()`)
- Private fields: `_repo_dir_resolved`, `logger` (initialized in `__post_init__()`)
- Provides automatic `__repr__()`, `__eq__()`, and field introspection

**`WorkspaceDeployer._load_config() -> None`**
- Private method to load and validate workspace configuration
- Calls `_validate_and_handle_parameter_file()` to check for invalid parameter files
- Calls `_extract_config_from_parameters()` to extract workspace ID and environment
- Sets `self.workspace_id` and `self.environment`
- Automatically called by `deploy()` if `workspace_id` is not set

**`WorkspaceDeployer._validate_and_handle_parameter_file(param_file: Path) -> None`**
- Validates parameter file for common invalid scenarios
- Detects duplicate `find_value` entries
- Detects blank/empty destination values in `replace_value`
- Automatically renames invalid files to `.bak` extension
- Logs warning messages explaining validation failures

**`WorkspaceDeployer._extract_config_from_parameters(param_file: Path) -> tuple[str, str]`**
- Reads `parameter.yml` and extracts workspace ID and environment
- Validates that all `replace_value` entries use the same environment key
- Returns tuple of `(workspace_id, environment)`

**`WorkspaceDeployer._detect_lakehouse() -> None`**
- Searches for `*.Lakehouse` directories
- Reads `.platform` metadata file to extract `displayName`
- Sets `self.lakehouse_name` for orphan cleanup exclusions
- Automatically called by `deploy()` if `lakehouse_name` is not set

**`WorkspaceDeployer.deploy(...) -> None`**
- Main public method for deployment
- Accepts filtering arguments: `no_cleanup`, `item_types`, `exclude_types`, `include_items`
- Orchestrates the entire deployment workflow

**`cli.py` - CLI interface module**

Contains ALL CLI logic - argument parsing, command handling, error messages, and user-facing text.

**`parse_args() -> argparse.Namespace`**
- Configures all argument parsers inline (auth, rename, deploy)
- Handles deploy command injection for backward compatibility
- Returns parsed arguments

**`handle_auth_command(args) -> None`**
- Executes auth subcommands (list, switch, rename)
- Calls business logic from `auth.py` (list_accounts, switch_account, etc.)
- Formats and displays output to user
- Handles errors with user-friendly messages

**`handle_rename_command(args) -> None`**
- Executes rename command
- Calls `rename_items_by_display_name()` from `rename.py`
- Handles errors with user-friendly messages

**`handle_detect_command(args) -> None`**
- Executes detect command
- Creates `ParameterDetector` instance and calls `detect_all()`
- Prints detection summary with dynamic variables
- Optionally saves parameter.yml with `--save` flag
- Handles errors with user-friendly messages

**`handle_deploy_command(args) -> None`**
- Executes deploy command
- If `--auto-detect` flag is set:
  - Validates `--workspace-id` is provided
  - Creates `ParameterDetector` and detects all source IDs
  - **Same-Workspace Check**: Compares provided workspace ID with detected source workspace ID
    - If match found: Disables auto-detect and deploys directly without parameter replacements
    - Notifies user that source and target are identical
  - If different workspaces: Generates parameter.yml on-the-fly with dynamic variables
  - Optionally saves with `--save-params` flag (only when different workspaces)
- Creates `WorkspaceDeployer` instance and calls `deploy()`
- Handles errors with user-friendly messages

**`main() -> None`**
- CLI entry point
- Routes to appropriate command handler based on args.command (auth, rename, detect, deploy)



## Microsoft's fabric-cicd Package

**Documentation**: https://microsoft.github.io/fabric-cicd/

**Key Classes/Functions**:
- `FabricWorkspace(workspace_id, repository_directory, environment, item_type_in_scope)` - Main configuration class
- `publish_all_items(workspace)` - Deploys items to Fabric
- `unpublish_all_orphan_items(workspace)` - Cleans up orphaned items

**Supported Item Types** (handled by Microsoft's package):
- DataPipeline, Environment, Notebook, Report, SemanticModel
- Lakehouse, MirroredDatabase, VariableLibrary, CopyJob
- Eventhouse, KQLDatabase, KQLQueryset, Reflex, Eventstream
- Warehouse, SQLDatabase, KQLDashboard, Dataflow, GraphQLApi
- ApacheAirflowJob, MountedDataFactory

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
  --workspace-id, -w     Destination workspace ID (replaces parameter.yml)
  --auto-detect, -d      Auto-detect source IDs and generate parameters on-the-fly
  --save-params          Save auto-detected parameters to parameter.yml.detected
  --help                 Show help message
```

**Note**: The `deploy` subcommand is optional. When deploy-specific flags are used without specifying a subcommand, the CLI automatically defaults to `deploy`.

**Auto-Detect Mode**: Use `-d -w <workspace-id>` to deploy without a parameter.yml file. This will:
1. Scan workspace files to detect source IDs (workspace, lakehouse, SQL endpoints)
2. **Check if target workspace matches source workspace**:
   - If same: Skip parameter generation and deploy directly without replacements
   - If different: Generate parameter.yml with dynamic variables
3. Generate parameter.yml with dynamic variables (`$workspace.$id`, `$items.Lakehouse.<name>.$id`)
4. Deploy to target workspace (fabric-cicd resolves IDs automatically)
5. Clean up generated files after deployment

**Same-Workspace Deployment**: When deploying to the same workspace as the source (e.g., updating items in place), the CLI automatically detects this and skips parameter generation, avoiding unnecessary replacements.

### Detect Parameters

```
fabdep detect [DIRECTORY] [OPTIONS]

Arguments:
  DIRECTORY              Path to workspace directory (default: current directory)

Options:
  --save, -s             Save detected parameters to parameter.yml.detected
  --help                 Show help message
```

**Purpose**: Auto-detect source IDs from workspace files and optionally generate parameter.yml with dynamic variables.

**Detection Sources**:
- Notebook metadata (`default_lakehouse_workspace_id`, `default_lakehouse`)
- SemanticModel `expressions.tmdl` files (SQL connection strings and endpoint IDs)
- Pipeline JSON files (external connection IDs)
- Lakehouse `.platform` files (display names for variable generation)

**Output**: Displays detected parameters with their dynamic variable mappings. Parameters without dynamic variables (e.g., external connections) are shown but marked as skipped.

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

Renames workspace item directories to match their display names from `.platform` metadata. By default, uses the exact display name. With `--clean`, converts display names to lowercase with underscores (e.g., "My Notebook" → "my_notebook"). Runs in dry-run mode by default to preview changes before applying them. Shows which directories are skipped when they already match the target name.

### Authentication Management

```
fabdep auth <command>

Commands:
  list                   List current and all available Azure accounts
  switch <sub-id>        Switch to different subscription
  rename <sub-id> <name> Rename a subscription
```

**Example usage**:
```bash
# Deploy from current directory (deploy subcommand is implicit)
fabdep

# Deploy from specific directory
fabdep /path/to/workspace

# Deploy without cleaning up orphaned items
fabdep --no-cleanup

# Deploy only notebooks and pipelines (deploy is inferred from -t flag)
fabdep -t Notebook DataPipeline

# Exclude reports and semantic models
fabdep --exclude-types Report SemanticModel

# Deploy specific items by name (experimental)
fabdep --include-items "MyNotebook.Notebook" "MyPipeline.DataPipeline"

# Explicit deploy subcommand also works
fabdep deploy -t Notebook DataPipeline

# Rename items to match display names (dry-run)
fabdep rename

# Actually perform renames
fabdep rename --execute

# Rename with cleaning (lowercase + underscores)
fabdep rename --clean --execute

# Rename items in specific directory
fabdep rename /path/to/workspace -e -c

# Authentication commands
fabdep auth list    # Shows current + all available accounts
fabdep auth switch xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
fabdep auth rename xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx "Production Subscription"  # Auto-switches if needed
```



## Authentication Module

**auth.py** provides Azure CLI wrappers for account management:

```python
def list_accounts(refresh: bool = True) -> list[AzureAccount]:
    """List all Azure accounts."""

def get_current_account() -> AzureAccount | None:
    """Get currently active account."""

def switch_account(subscription_id: str) -> None:
    """Switch to a different account."""

def rename_subscription(subscription_id: str, new_name: str) -> bool:
    """Rename an Azure subscription. Auto-switches if needed. Returns True if switched."""

def is_connection_error(error_msg: str) -> bool:
    """Check if error is connection reference related (DataPipeline, Dataflow)."""

def is_auth_error(error_msg: str) -> bool:
    """Check if error is authentication/authorization related."""
```

**Error Detection**: The CLI automatically detects different error types and provides specific troubleshooting guidance:
- **Connection errors**: Detects when DataPipelines/Dataflows reference missing connections, explains that connection GUIDs are tenant-wide and must be edited in source repository files (not parameter.yml)
- **Auth errors**: Detects authentication/authorization failures (401, 403, 404, "unauthorized", "forbidden"), displays current account information and suggests auth commands
- **Priority**: Connection errors are checked first to avoid false positives (connection errors can contain auth-related keywords)

**Auto-switching**: The `rename` command automatically switches to the target subscription if it's not accessible in the current tenant context, eliminating the need for manual switching before renaming.

**Design**: Auth module contains ONLY business logic. All CLI handling (parser setup, command execution, output formatting) is in `cli.py`.

## Logging Module

**logging.py** provides custom logging and console formatting utilities:

```python
def get_logger(name: str) -> logging.Logger:
    """Configure and return logger with CustomFormatter."""

def print_header(message: str) -> None:
    """Print styled header (re-exported from fabric-cicd)."""

def print_error(message: str) -> None:
    """Print error message without log prefix."""

def format_account_list(accounts: list[dict], current_id: str | None = None) -> str:
    """Format list of Azure accounts with current marker."""

def format_auth_error_message(
    current_account: dict | None = None,
    all_accounts: list[dict] | None = None
) -> str:
    """Format helpful authentication error message with troubleshooting tips."""

def format_connection_error_message() -> str:
    """Format helpful message for connection reference errors in DataPipelines/Dataflows."""
```

**Design**: Separates presentation/formatting logic from business logic. Auth module handles Azure CLI interactions and error detection, logging module handles display formatting.

## Rename Module

**rename.py** provides utility for renaming workspace item directories:

```python
def rename_items_by_display_name(
    repo_dir: Path | str,
    dry_run: bool = True,
    clean: bool = False,
) -> None:
    """Rename workspace item directories to match their display names."""
```

**Flow**:
1. Search for all `.platform` files in workspace (`**/*.*/.platform` glob pattern)
2. Read each `.platform` metadata file to extract `displayName`
3. Optionally convert display name to directory name format (lowercase, underscore-separated) when `clean=True`
4. Rename parent directory to match formatted display name
5. Skip items that already match target name (logs as INFO level, visible by default)
6. By default runs in dry-run mode (preview only)

**Error Handling**: Gracefully handles invalid `.platform` files (JSON parsing errors, missing keys) and continues processing other items.

**Design**: Rename module contains ONLY business logic. All CLI handling (parser setup, command execution, error handling) is in `cli.py`.

## Error Handling

The CLI validates:
- Workspace directory exists
- `parameter.yml` exists in the workspace directory
- At least one `find_replace` entry exists
- All `replace_value` entries use the same environment key
- At least one `*.Lakehouse` directory exists
- Lakehouse `.platform` metadata file is readable

**Intelligent error handling**: The CLI automatically detects different error types and provides specific troubleshooting guidance:
- **Connection errors**: Detects when DataPipelines/Dataflows reference missing tenant-wide connections, explains that connection GUIDs must be edited directly in repository source files (not via parameter.yml)
- **Auth/authorization errors**: Detects authentication failures, displays account information and suggests relevant auth commands
- **Validation errors**: Parameter file validation issues trigger automatic .bak renaming with clear explanation

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

The `WorkspaceDeployer` class is defined in `workspace.py` and can be imported directly for programmatic use.

## Programmatic Usage

All public classes can be imported and used programmatically:

```python
from fabric_deploy import (
    WorkspaceDeployer,
    ParameterDetector,
    rename_items_by_display_name,
)
from pathlib import Path

# --- Parameter Detection ---

# Detect and display parameters
detector = ParameterDetector(repo_dir="/path/to/workspace")
detector.detect_all()
detector.print_detection_summary()

# Generate parameter.yml with dynamic variables
detector.save_parameter_yml(
    output_path="parameter.yml",
    use_dynamic=True,
    environment="prod",
    skip_empty=True  # Skip parameters without dynamic variables
)

# --- Deployment ---

# Basic deployment
deployer = WorkspaceDeployer(
    repo_dir="/path/to/workspace",
    logger_name="my_app.fabric_deployment",
)
deployer.deploy()

# With auto-detect mode
deployer = WorkspaceDeployer(
    repo_dir="/path/to/workspace",
    workspace_id="target-workspace-id",
    auto_detect=True,  # Enable dynamic parameter generation
)
deployer.deploy()

# With custom filters
deployer.deploy(
    item_types=["Notebook", "DataPipeline"],
    no_cleanup=False,
)

# Inspect configuration before deploying
deployer._load_config()
print(f"Workspace: {deployer.workspace_id}")
print(f"Environment: {deployer.environment}")

deployer._detect_lakehouse()
print(f"Lakehouse: {deployer.lakehouse_name}")

# --- Rename Utility ---

# Rename with cleaning enabled
rename_items_by_display_name(
    repo_dir="/path/to/workspace",
    dry_run=False,
    clean=True,  # Apply .lower().replace(" ", "_")
)
```

**Benefits:**
- **Deployment Control**: Programmatic control over deployment logic
- **Dataclass Design**: Automatic `__repr__()`, `__eq__()`, and field introspection
- **Lazy Loading**: No I/O during initialization
- **Custom Logging**: Namespace logging for better observability
- **Integration**: Compose these classes in larger automation workflows

## Related

- **[microsoft/fabric-cicd](https://github.com/microsoft/fabric-cicd)**: The underlying library
- **Workspace repos**: Use this CLI tool for deployments
- **example-fabric-etl**: Core Python ETL framework (separate package)

## Troubleshooting

**"parameter.yml not found"**
- Ensure you're running the command from a Fabric workspace directory
- Check that `parameter.yml` exists in the workspace root

**"No environments found in parameter.yml"**
- Verify `find_replace` entries have `replace_value` dictionaries
- Check that at least one environment key exists (e.g., `prod`, `dev`, `env`)

**"Multiple different environments found"**
- All `replace_value` entries must use the same environment key
- Fix: Update all entries to use consistent environment naming

**"No lakehouse found in repository directory"**
- Ensure at least one `*.Lakehouse` directory exists
- Check that the directory contains a `.platform` metadata file

**"Invalid parameter file detected"**
- Check logs for specific validation failures (duplicate find_values, blank destinations)
- Parameter file will be automatically renamed to `.bak`
- Deployment continues using workspace ID from first entry only
- Fix the parameter file and rename back to `.yml` to restore parameter replacement

**"User does not have access to the connection used in the Pipeline"**
- This error occurs when a DataPipeline references a connection that doesn't exist in the target tenant
- **Root cause**: Connection GUIDs are TENANT-WIDE (not workspace-specific), and your workspace repo contains GUIDs from a different tenant
- **Important**: This indicates the workspace repository is misconfigured for the target tenant
- **Solution**:
  1. Identify the correct connection GUIDs in the target tenant
  2. Edit the pipeline files (e.g., `pipeline-content.json`) directly in your workspace repository
  3. Update connection GUIDs to match the target tenant's connections
  4. Commit the updated files to your repository
  5. Re-run deployment
- **DO NOT use parameter.yml for connection GUIDs** - they should be edited in source files
- **Detection help**: Run `fabdep detect` to discover connection GUIDs currently in your workspace repo

**Authentication/Authorization Errors**
- Run `fabdep auth list` to see current and all available accounts
- Switch accounts with `fabdep auth switch <subscription-id>`
- Workspace may be in a different tenant than your current authentication