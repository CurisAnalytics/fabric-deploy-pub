# fabric-deploy

CLI wrapper around Microsoft's [fabric-cicd](https://github.com/microsoft/fabric-cicd) that deploys git-synced Fabric workspaces. Resolves the target workspace from `workspace_base_name` + git branch, auto-detects parameter replacements, and deploys via the Fabric REST API. Auth is the active `az` session (`AzureCliCredential`).

See [CLAUDE.md](./CLAUDE.md) for architecture and internals.

## Install

```bash
uv sync          # or: pip install -e .
```

## Quick start

```bash
echo 'workspace_base_name: "my_workspace"' > fabdep.yml
cd path/to/workspace
fabdep            # deploys to "my_workspace [<current-branch>]"
```

## Commands

### Deploy (default)

```
fabdep [DIRECTORY] [OPTIONS]
  -n, --no-cleanup       Skip unpublishing orphaned items
  -t, --item-types       Item types to deploy (e.g. Notebook DataPipeline)
  -x, --exclude-types    Item types to exclude
  -i, --include-items    Specific items only, "Name.Type" (matches .platform displayName)
      --pr [COMMIT]      Only the items the latest pull request touched
  -b, --branch           Override git branch for workspace resolution
  -N, --new-workspace    Create the workspace if it doesn't exist
  -r, --max-retries      Retries for transient errors (default: 1)
  -L, --skip-lakehouse   Skip lakehouse param replacement (feature branches share dev lakehouse)
  -F, --fail-on-stale    Exit non-zero if a notebook has multiple lakehouses attached
      --frame            After deploy, frame the Direct Lake model(s)
```

`-i` matches the `.platform` **displayName**, not the folder name; a name that
matches nothing fails loudly (instead of silently deploying nothing) and a
one-line `Deployed: ...` confirmation prints what landed.

`--pr` automates reading the PR diff before a promotion: it finds the most
recent merge / `Merged PR N:` (Azure DevOps) / `Merge pull request #N` or
`(#N)` (GitHub) commit reachable from HEAD, maps the files it changed onto the
items that own them, and deploys only those. Pass a commit-ish (`--pr abc1234`)
to pick a specific PR. It resolves into `-i`, so orphan cleanup is off and the
two flags can't be combined. Changed paths that belong to no item in the
working tree — repo config, docs, or an item the PR deleted — are listed and
skipped; `--pr` never unpublishes.

### Refresh (Direct Lake framing)

Re-frames a Direct Lake model: **sync SQL endpoint → refresh → poll → surface
the first framing error.** The SQL-endpoint sync first is required, or the
refresh fails with *"cannot access the source Delta table."*

```
fabdep refresh [DIRECTORY] [OPTIONS]
  -i, --include-items    Model display name(s) to frame (default: all in repo;
                         a trailing ".SemanticModel" is tolerated)
  -b, --branch           Override git branch for workspace resolution
      --no-sync          Skip the SQL endpoint metadata sync
      --timeout          Per-model poll timeout, seconds (default: 900)
```

```bash
fabdep refresh                    # frame every model in the repo
fabdep refresh -i "Sales Model"   # frame one model
fabdep deploy --frame             # deploy, then frame in one shot
```

> A `Completed` refresh proves the model re-framed, **not** that measures
> evaluate — verify a visual or DAX query.

Refresh uses the Power BI API (different token audience than Fabric);
`AzureCliCredential` mints the right scope. In token-relay mode set
`POWERBI_BEARER_TOKEN` (falls back to `FABRIC_BEARER_TOKEN`, which the Power BI
host may reject).

### Auth

```
fabdep auth list                       # accounts (marks User vs SPN)
fabdep auth switch <sub-id|name>       # switch active account
fabdep auth rename <sub-id> <name>     # alias an account (survives az refresh)
fabdep auth login <alias> [--app … --tenant … --cert …] [--user]   # multi-tenant
```

Authenticate as an SPN with `az login --service-principal -u <id> -p <secret> --tenant <tid> --allow-no-subscriptions`. SPNs and renames persist to `~/.azure/azureProfile.fabdep.json`, surviving `az account list --refresh`.

**Multi-tenant:** `fabdep auth login <alias>` logs a tenant's SPN into an isolated `AZURE_CONFIG_DIR` (default `~/.azure-<alias>`) and registers the alias, so tenants never share `~/.azure` state. Pass `--app/--tenant/--cert` on first login (reused after); `--user` for an interactive non-SPN login. A workspace opts in with `tenant: <alias>` in `fabdep.yml` — fabdep then selects that login and asserts the active identity is in the pinned tenant before any Fabric call.

### Rename items

```
fabdep rename [DIRECTORY] [-e/--execute] [-c/--clean]
```

Renames item directories to match their `.platform` displayName (dry-run by default; `-c` lowercases + underscores).

## fabdep.yml

```yaml
workspace_base_name: "my_workspace"  # + " [<branch>]" → resolved via Fabric API
capacity_id: "…"                        # optional; first active capacity used with -N
tenant: contoso                           # optional tenant-pin alias (see Auth)

manual_parameters:                      # optional: override auto-detected params
  - find_value: "ffffffff-…"            #   auto-detected from source files
    replace_value: "ffffffff-…"         #   keep this external reference unchanged
    item_type: ["SemanticModel"]
    description: "External lakehouse SQL endpoint"
```

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `fabdep.yml not found` | Add `workspace_base_name:` to a `fabdep.yml` in the workspace root |
| `Workspace not found` | Confirm `{base_name} [{branch}]` exists; check `fabdep auth list` and access |
| `Failed to get git branch` | Run from a git repo, or pass `--branch` |
| `does not have access to the connection` | Connection GUIDs are tenant-wide; fix them in the pipeline files (`fabdep detect` lists them) |
| Auth errors | `fabdep auth list`, then `fabdep auth switch <sub-id|name>` |

Cryptic Fabric failures (RequiredOptionsMissing, compatibility-level parse errors, Direct-Lake hybrid-mode / storage-mode-change rejections) print a plain-language hint alongside the raw error.

## Links

- [CLAUDE.md](./CLAUDE.md) — architecture & development guide
- [fabric-cicd docs](https://microsoft.github.io/fabric-cicd/) · [GitHub](https://github.com/microsoft/fabric-cicd)
