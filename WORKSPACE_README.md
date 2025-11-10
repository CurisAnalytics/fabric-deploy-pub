# Contoso 𐧾 Fabric Workspace

This repo is synced to a Microsoft Fabric Workspace (comprised of Notebooks, Pipelines, Lakehouses, Reports, &c).

It includes script-based CI/CD deployment using [fabric-deploy](https://github.com/path/to/fabric-deploy), which wraps Microsoft's [fabric-cicd](https://github.com/microsoft/fabric-cicd).

Commits are most often made through the Fabric UI.

---

## Local Development

- Export sample data to local Parquet/CSV/Excel files

- Update notebook paths to point to local files during development

- Use `dummy-notebookutils-reborn` from pip for Fabric library stubs (includes `mssparkutils`)

- Instantiate a local Spark cluster with the following:

```python
from pyspark.sql import SparkSession

spark = SparkSession.builder.getOrCreate()
```

- IDEs with VSCode extensions can alternatively use [Fabric Data Engineering](https://learn.microsoft.com/en-us/fabric/data-engineering/setup-vs-code-extension)

---

## Structure

- **`*.Lakehouse/`**: Lakehouse definitions and schemas

- **`Notebooks/` or `ETL Pipelines/`**: Notebook ETL logic (`.Notebook/` subdirectories)

- **`Pipelines/` or `*.DataPipeline/`**: Data pipeline orchestrations

- **`*.Report/`**: Power BI reports

- **`*.SemanticModel/`**: Power BI semantic models

- **`Environment.Environment/`**: Python package configurations (generally blank)

- **`*.CopyJob/`**: Data copy operations

- **`fabdep.yml`**: Deployment configuration (workspace base name, capacity ID, manual parameters)

- **`parameter.yml`**: Environment-specific parameter replacements (generated or manual)

Most item subfolders include a `.platform` file indicating metadata (with fields *type*, *displayName*, and *logicalId*).

### Notebooks

Each `.Notebook/` directory contains `notebook-content.{py/sql}` comprised of Python/PySpark/SQL code.

Edit `notebook-content` directly, use your local copy as a reference and for LLM usage, or use the VSCode Fabric connector for interactive development.

---

## Deployment (CI/CD)

Deploy this Workspace to Microsoft Fabric using the `fabdep` CLI:

```bash
# Install the CLI tool
pip install fabric-deploy
# Or from within the fabric-deploy git repo
pip install -e .

# Deploy from workspace directory (auto-detects configuration from fabdep.yml)
fabdep

# Deploy with options
fabdep --no-cleanup                           # Skip orphan cleanup
fabdep --item-types Notebook DataPipeline     # Deploy specific types only
fabdep --exclude-types Report SemanticModel   # Exclude certain types
fabdep --auto-detect                          # Auto-detect parameters (no parameter.yml needed)
fabdep --branch prod                          # Deploy to specific branch
```

### Configuration

Create a `fabdep.yml` file in the workspace root:

```yaml
workspace_base_name: "my_workspace"
capacity_id: "12345-67890-abcd-..."  # Optional, auto-detected if not specified

# Optional: Manual parameter overrides for external resources
manual_parameters:
  - find_value: "ffffffff-ffff-ffff-ffff-ffffffffffff"  # Auto-detected from source files
    replace_value: "ffffffff-ffff-ffff-ffff-ffffffffffff"  # Keep same (external reference)
    item_type: ["SemanticModel"]
```

**What each field does**:
- `workspace_base_name`: Base name of the Fabric workspace. Combined with git branch to resolve full workspace name (e.g., `my_workspace [main]`)
- `capacity_id`: Fabric capacity ID. Optional—if omitted, `--create-workspace` uses the first active capacity. Required if manually specifying capacity.
- `manual_parameters`: Override `replace_value` for auto-detected parameters that reference external resources (e.g., SQL endpoint IDs from other workspaces). Specify `item_type` to limit which items the replacement applies to.

### Standard Deployment

```bash
# Deploy using current git branch
# Resolves workspace as: {workspace_base_name} [{current_branch}]
fabdep

# Deploy to specific branch
fabdep --branch prod        # Deploys to "my_workspace [prod]"

# Deploy with parameter overrides
fabdep -t Notebook DataPipeline     # Deploy specific item types
fabdep -x Report SemanticModel      # Exclude specific types
fabdep --no-cleanup                 # Skip orphan cleanup
```

### Auto-Detect Mode

Automatically detect source IDs and generate `parameter.yml` on-the-fly:

```bash
# Auto-detect parameters (recommended for first-time setup)
fabdep --auto-detect

# Auto-detect and save detected parameters to file
fabdep --auto-detect --save-params

# Auto-detect with filtering
fabdep --auto-detect -t Notebook DataPipeline
```

This mode scans your workspace files (Notebooks, Pipelines, SemanticModels) and automatically detects:
- Workspace IDs
- Lakehouse IDs
- SQL connection strings and endpoint IDs
- Generates dynamic variables like `$workspace.$id` and `$items.Lakehouse.<name>.$id`

### Bootstrap Mode (New Workspace)

Deploy to blank workspaces or create workspaces from scratch:

```bash
# Deploy to existing blank workspace
fabdep --bootstrap

# Create workspace and deploy (recommended)
fabdep --bootstrap --create-workspace

# Auto-detect + bootstrap (best for new workspaces)
fabdep --auto-detect --bootstrap
```

**Two-phase deployment**:
1. **Phase 1**: Deploy foundational items (Lakehouse, SemanticModel, SQLDatabase, Warehouse, etc.)
2. **Phase 2**: Deploy dependent items (Notebook, DataPipeline, Report, etc.)

This prevents chicken-and-egg problems where notebooks/pipelines reference lakehouse IDs that don't exist yet.

**Workspace Creation**:
- Automatically creates the workspace if it doesn't exist
- Uses `capacity_id` from `fabdep.yml`, or defaults to first active capacity
- Workspace name format: `{workspace_base_name} [{branch}]`
- Checks if workspace already exists before creating

### Authentication

Manage Azure accounts and subscriptions:

```bash
fabdep auth list                # Show current and available accounts
fabdep auth switch <sub-id>     # Switch to different subscription
fabdep auth rename <sub-id> <name>  # Rename subscription (auto-switches if needed)
```

If you receive authentication errors, the CLI will display available accounts and suggest relevant commands. You may need to switch to a subscription in the correct tenant for your Workspace.

### Parameter Detection

Inspect and save detected parameters:

```bash
fabdep detect                   # Show detected parameters (dry-run)
fabdep detect --save            # Save to parameter.yml.detected
```

---

## What Happens During Deployment

1. **Load Configuration**: Reads `fabdep.yml` to get workspace base name and capacity ID
2. **Resolve Workspace**: Combines base name + git branch (or `--branch` override) and looks up workspace ID via Fabric API
3. **Create Workspace** (if `--create-workspace`): Creates workspace if it doesn't exist
4. **Generate Parameters** (if `--auto-detect`): Auto-detects source IDs and generates `parameter.yml` dynamically
5. **Apply Overrides** (if `manual_parameters` in `fabdep.yml`): Applies manual parameter overrides for external resources
6. **Apply Find/Replace**: Executes parameter replacements to update environment-specific IDs
7. **Detect Lakehouse**: Reads lakehouse name from `.Lakehouse/.platform` metadata
8. **Publish Items**: Uses Microsoft's `fabric-cicd` library to deploy items to target workspace
9. **Clean Up** (unless `--no-cleanup`): Removes orphaned items from previous deployments

---

## External Resources & Connection GUIDs

If your workspace references external resources (e.g., SQL endpoints from other workspaces, external lakehouses), use manual parameter overrides in `fabdep.yml`:

```yaml
manual_parameters:
  # Lakehouse SQL Analytics Endpoint Item ID
  - find_value: "ffffffff-ffff-ffff-ffff-ffffffffffff"  # Auto-detected from SemanticModel
    replace_value: "ffffffff-ffff-ffff-ffff-ffffffffffff"  # Keep same (external reference)
    item_type: ["SemanticModel"]

  # SQL Server connection string
  - find_value: "old-server.database.windows.net"  # Auto-detected from DataPipeline
    replace_value: "new-server.database.windows.net"  # Custom replacement
    item_type: ["DataPipeline"]
```

**Important**: Connection GUIDs in DataPipelines/Dataflows are tenant-wide and must be configured manually in the target workspace. They are not deployed by fabric-cicd and their IDs don't change between workspaces.

---

## Troubleshooting

**"fabdep.yml not found"**
- Create `fabdep.yml` in workspace root with `workspace_base_name: "your_workspace"`

**"Workspace not found: 'workspace_name'"**
- Verify workspace exists in Fabric UI with name: `{base_name} [{branch}]`
- Run `fabdep auth list` to verify Azure account is correct
- Check you have access permissions to the workspace

**"Failed to get git branch"**
- Ensure running from git repository (not detached HEAD state)
- Use `--branch` flag to override: `fabdep --branch prod`

**"No lakehouse found in repository directory"**
- Ensure at least one `*.Lakehouse/` directory exists with `.platform` file

**Authentication/Authorization Errors**
- Run `fabdep auth list` to see available accounts
- Switch with `fabdep auth switch <subscription-id>`
- May need to switch to subscription in correct tenant for your workspace

---

## Related

- **[fabric-deploy](https://github.com/path/to/fabric-deploy)**: CLI wrapper for Fabric deployments
- **[microsoft/fabric-cicd](https://github.com/microsoft/fabric-cicd)**: Underlying deployment library
- **[Microsoft Fabric Documentation](https://learn.microsoft.com/en-us/fabric/)**: Fabric platform docs