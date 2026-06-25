# fabric-deploy

CLI wrapper around Microsoft's [fabric-cicd](https://github.com/microsoft/fabric-cicd) that deploys git-synced Fabric Workspaces.

### Features

- **Workspace Resolution**: Base name + git branch → workspace ID via Fabric API
- **Auto-Detect Parameters**: Scans workspace files, generates dynamic parameter.yml
- **Workspace Creation**: Create new workspaces with `-N`
- **Item Filtering**: Deploy/exclude specific item types
- **Feature Branch Support**: Skip lakehouse replacement with `-L` (use dev lakehouse)
- **Direct Lake Framing**: `fabdep refresh` / `deploy --frame` — sync SQL endpoint → refresh → poll → surface the first framing error
- **Auth Management**: Switch accounts, persistent SPN logins & aliases
- **Configurable Retries**: Auto-retry transient errors (customizable with `-r`)
- **Friendlier Errors**: Cryptic Fabric failures (RequiredOptionsMissing, compatibilityLevel parse errors, Direct-Lake hybrid-mode and storage-mode-change rejections) get a plain-language hint; `-i` fails loudly instead of silently deploying nothing on a name mismatch


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
uv sync          # or: pip install -e .
```

Requires `fabric-cicd >= 1.0` (declared in `pyproject.toml`). Authentication is delegated to the active `az` session via `azure.identity.AzureCliCredential`, so `fabdep auth switch ...` is the only auth surface you need.

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
  -F, --fail-on-stale    Exit non-zero if a notebook has multiple lakehouses attached
      --frame            After deploy, frame the Direct Lake model(s)
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
fabdep --frame            # Deploy, then frame the Direct Lake model(s)
```

When `-i / --include-items` names an item that isn't in the repo (it matches
the `.platform` **displayName**, not the folder name), fabdep now fails loudly
instead of silently deploying nothing, and prints a one-line `Deployed: ...`
confirmation of what landed.

---


### Refresh (Direct Lake framing)

After a Delta write or a semantic-model deploy, a Direct Lake model must be
re-framed — and its SQL analytics endpoint synced first, or the refresh fails
with *"cannot access the source Delta table."* `fabdep refresh` runs the whole
sequence: **sync SQL endpoint → refresh → poll → surface the first framing
error.**

```bash
fabdep refresh [DIRECTORY] [OPTIONS]

Options:
  -i, --include-items    Semantic model display name(s) to frame (default: all in repo)
  -b, --branch           Override git branch for workspace resolution
      --no-sync          Skip the SQL analytics endpoint metadata sync
      --timeout          Per-model refresh poll timeout in seconds (default: 900)
```

```bash
fabdep refresh                      # Frame every model in the repo (sync first)
fabdep refresh -i "Sales Model"     # Frame one model by display name
fabdep deploy --frame               # Deploy, then frame in one shot
```

> A `Completed` refresh proves the model **re-framed**, not that measures
> evaluate. Type drift / broken relationships surface only at query time —
> still verify a visual or a DAX query.

Semantic-model refresh uses the Power BI API (a different token audience than
the Fabric API); `AzureCliCredential` mints the right scope automatically. In
token-relay mode set `POWERBI_BEARER_TOKEN` for a Power BI-scoped token (it
falls back to `FABRIC_BEARER_TOKEN`, which the Power BI host may reject).

---


### Authentication

```bash
fabdep auth <command>

Commands:
  list                   List current and all available Azure accounts
  switch <sub-id>        Switch to different subscription
  rename <sub-id> <name> Rename a subscription
  login <alias>          Log a tenant in to an isolated config dir + register it
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

**Multi-tenant: `fabdep auth login` + the tenant pin**

For work across multiple Entra tenants, `fabdep auth login <alias>` logs a tenant's SPN into an **isolated** `AZURE_CONFIG_DIR` (default `~/.azure-<alias>`) and records it in a machine-local registry (the `tenants` key in `~/.azure/azureProfile.fabdep.json`), so two tenants never share the global `~/.azure` state — letting you deploy to different tenants from concurrent processes without identity bleed.

```bash
# First login (or after a cert swap) — pass the SPN details once:
fabdep auth login contoso --app <CLIENT_ID> --tenant <TENANT_ID> --cert ~/contoso.pem

# Re-login later — reuses the stored app id / cert / config dir:
fabdep auth login contoso

# Interactive user (non-SPN) login into the isolated dir:
fabdep auth login contoso --user
```

Options: `--app`, `--tenant`, `--cert` (required on first SPN login; reused after), `--config-dir` (override the default `~/.azure-<alias>`), `--user` (interactive non-SPN login). The tenant id stored in the registry is read live from the fresh login, so it can't drift.

A workspace then opts in with a `tenant:` line in its `fabdep.yml` (see [Configuration](#fabdepyml-required)). Before any Fabric call, fabdep resolves the alias, points `AZURE_CONFIG_DIR` at that tenant's login, and asserts the active identity lives in the pinned tenant — failing fast with a clear error on a mismatch instead of a confusing "workspace not found". Deploys with no `tenant:` line are unaffected.


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
tenant: contoso                         # Optional: pin to a tenant alias (see Authentication)

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
- `tenant` is an optional registry alias (created by `fabdep auth login`) — it routes the deploy to that tenant's isolated login and asserts the active identity is in that tenant. Omit it for single-tenant work; behavior is then unchanged. It's portable (no machine paths/GUIDs), so it can be committed and travels to every worktree.


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
