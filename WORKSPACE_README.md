# Contoso 𐧾 Fabric Workspace

This repo is synced to a Microsoft Fabric Workspace (comprised of Notebooks, Pipelines, Lakehouses, Reports, &c).

Commits are most often made through the Fabric UI.

CI/CD is available using [fabric-deploy](https://github.com/example-org/fabric-deploy), which wraps Microsoft's [fabric-cicd](https://github.com/microsoft/fabric-cicd).

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

Fabric items are stored in the repo as a folder whose "extension" denotes the item type.

Most item subfolders include a `.platform` file indicating metadata (with fields *type*, *displayName*, and *logicalId*).

### Notebooks

Each `.Notebook/` directory contains `notebook-content.{py/sql}` comprised of Python/PySpark/SQL code.

Edit `notebook-content` directly, use your local copy as a reference and for LLM usage, or use the VSCode Fabric connector for interactive development.

---

## Deployment (CI/CD)

Deploy this Workspace to Microsoft Fabric using the Contoso `fabdep` CLI:

```bash
# Install fabric-deploy
pip install -e path/to/fabric-deploy

# Deploy workspace (auto-detects workspace from fabdep.yml + git branch)
fabdep

# Deploy specific item types only
fabdep -t Notebook DataPipeline

# Create new workspace
fabdep -N
```

**Configuration**: `fabdep.yml` in workspace root defines `workspace_base_name`. Target workspace is resolved as `{workspace_base_name} [{branch}]` via Fabric API.

**Auto-Detection**: Source IDs are auto-detected and dynamically replaced. Use `-A` for manual `parameter.yml`.
