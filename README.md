# fabric-deploy

CLI wrapper around Microsoft's [`fabric-cicd`](https://github.com/microsoft/fabric-cicd) that deploys git-synced Fabric workspaces. Resolves workspace IDs from `<base_name> [<git_branch>]`, auto-generates `parameter.yml` from your repo, and delegates auth to the active `az` session.

## Install

```bash
git clone <repo> && cd fabric-deploy
uv sync   # or: pip install -e .
```

Requires `fabric-cicd >= 1.0`. Auth uses `azure.identity.AzureCliCredential`, so `az login` (or `fabdep auth switch`) is the only auth surface.

## Quick start

In your Fabric workspace repo:

```yaml
# fabdep.yml
workspace_base_name: "my_workspace"
```

Then:

```bash
fabdep                 # deploys everything to "my_workspace [<current branch>]"
fabdep -t Notebook     # only one item type
fabdep -x Report       # all but one item type
fabdep -b prod         # override the branch
fabdep -N              # create the workspace if it doesn't exist
fabdep -L              # feature branch: skip lakehouse-ID rewrites
```

Auto-detection scans `*.Notebook/notebook-content.py`, `*.SemanticModel/definition/*.tmdl`, `*.DataPipeline/pipeline-content.json`, and `*.CopyJob/copyjob-content.json` for source IDs and emits a fresh `parameter.yml` per deploy.

## Auth

```bash
fabdep auth list                              # show all accounts (User and SPN)
fabdep auth switch <sub-id|name>              # switch active subscription
fabdep auth rename <sub-id> "<friendly name>" # local alias, persists across az logins
```

SPNs and aliases live in `~/.azure/azureProfile.fabdep.json` so they survive `az account list --refresh`. Sign in as a service principal with:

```bash
az login --service-principal -u <CLIENT_ID> -p <CLIENT_SECRET> \
  --tenant <TENANT_ID> --allow-no-subscriptions
```

## Config

`fabdep.yml` keys:

| key | required | default |
|---|---|---|
| `workspace_base_name` | yes | — |
| `capacity_id` | only with `-N` if you want a specific one | first active capacity |
| `manual_parameters` | no | — |

Use `manual_parameters` to override auto-detected `replace_value`s (e.g. to *keep* an external reference unchanged). `find_value` must match what auto-detect found; optional `item_type` scopes the substitution.

```yaml
manual_parameters:
  - find_value: "00000000-0000-0000-0000-000000000000"
    replace_value: "00000000-0000-0000-0000-000000000000"  # keep as-is
    item_type: ["SemanticModel"]
    description: "External SQL endpoint we don't deploy"
```

## Other commands

```bash
fabdep detect [--save]    # show or write detected parameters to parameter.yml.detected
fabdep rename [--execute] # rename item directories to match .platform displayName
```

## Links

- [CLAUDE.md](./CLAUDE.md) — internal architecture notes
- [fabric-cicd](https://github.com/microsoft/fabric-cicd) / [docs](https://microsoft.github.io/fabric-cicd/)
