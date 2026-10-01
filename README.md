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
      --allow-unbound-udf  Deploy report buttons whose User Data Function
                         reference can't be rebound to the target workspace
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

PBIR report buttons bound to a User Data Function are rebound to the target
workspace automatically, matched on the function name declared in a repo
`.UserDataFunction` item (the itemId a button carries is source-workspace
state). A bound button whose function no repo item declares — a stale name,
or a UDF that was never committed — would deploy still calling the *source*
workspace's function, so it blocks the deploy when its report is in scope;
fix the button's binding or pass `--allow-unbound-udf` to ship it as-is.

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

Two separate things live in `~/.azure/azureProfile.fabdep.json`:

**Account labels** — cosmetic names for subscriptions in the active `az` login. They
only affect `fabdep auth list` / `switch` and never select an identity.

```
fabdep auth list                       # accounts (marks User vs SPN)
fabdep auth switch <sub-id|name>       # set the active az subscription
fabdep auth rename <sub-id> <name>     # label a subscription (survives az refresh)
```

**Tenant aliases** — what `tenant: <alias>` in `fabdep.yml` resolves against. With a
pin, fabdep picks the right identity for every deploy, so you never `auth switch`
first, and a wrong-tenant login fails early with a clear error instead of
"workspace not found". Two ways to register an alias:

```
fabdep auth bind <alias> <tenant-id|sub-id|name>   # developer machine: reuse your existing az login
fabdep auth login <alias> [--app … --tenant … --cert …] [--user]   # isolated SPN/user login
```

`bind` writes no login and no config dir: at deploy time fabdep mints tokens with
`--subscription` for an enabled account in that tenant (your own user login before an
SPN, or exactly the account you named), leaving the active `az` account alone. Log in
to the tenant first (`az login --tenant <id>`); the pin's error text lists the tenants
`az` can see. `login` instead logs the tenant into an isolated `AZURE_CONFIG_DIR`
(default `~/.azure-<alias>`) — the headless SPN-cert path. Pass `--app/--tenant/--cert` on
first login (reused after); `--user` for an interactive non-SPN login. Set
`require_spn: true` on a registry entry to reject user credentials.

Authenticate as an SPN with `az login --service-principal -u <id> -p <secret> --tenant <tid> --allow-no-subscriptions`. SPNs and labels persist to the profile, surviving `az account list --refresh`.

### Rename items

```
fabdep rename [DIRECTORY] [-e/--execute] [-c/--clean]
```

Renames item directories to match their `.platform` displayName (dry-run by default; `-c` lowercases + underscores).

## fabdep.yml

```yaml
workspace_base_name: "my_workspace"  # + " [<branch>]" → resolved via Fabric API
capacity_id: "…"                        # optional; first active capacity used with -N
tenant: contoso                           # optional tenant-pin alias (see Auth); unknown keys warn

manual_parameters:                      # optional: override or inject find/replace params
  - find_value: "ffffffff-…"            #   auto-detected from source files → override
    replace_value: "ffffffff-…"         #   keep this external reference unchanged
    item_type: ["SemanticModel"]
    description: "External lakehouse SQL endpoint"
  - find_value: "44444444-…"            #   not auto-detected but present in item files → injected
    replace_value: "55555555-…"         #   e.g. rewrite a PowerApp app id in report JSON
    item_type: ["Report"]
    description: "PowerApp link"
```

A `find_value` fabdep auto-detected gets its replacement overridden. One it didn't detect is
injected as a new find/replace entry — but only if the value literally occurs in an item file
(anything under a `.platform` directory); otherwise it warns and is skipped, so a typo'd or
stale entry can't silently no-op.

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
