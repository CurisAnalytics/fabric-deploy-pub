# fabric-deploy (`fabdep`)

Deploy Microsoft Fabric workspaces from git without hand-writing `parameter.yml`.

fabdep is a command-line wrapper around Microsoft's
[fabric-cicd](https://microsoft.github.io/fabric-cicd/) that generates fabric-cicd's
`parameter.yml` from your repo. It finds the workspace, lakehouse, SQL endpoint, warehouse,
SQL database and User Data Function IDs your items reference and rewrites them for the
target workspace; it picks that workspace from the git branch; and it blocks deploys that
would overwrite live row-level security or ship report buttons still wired to another
workspace. **`fabdep plan`** shows what a deploy would create, update, unpublish and
rewrite, without changing anything.

fabdep is an independent project, not affiliated with or endorsed by Microsoft.

**Without fabdep**, you keep a parameter file in step with every ID in the repo, and look up
the target workspace's ID for your deploy script:

```yaml
# parameter.yml
find_replace:
  - find_value: "00000000-0000-0000-0000-0000000000d1"   # the dev workspace
    replace_value:
      PROD: "$workspace.$id"
  - find_value: "00000000-0000-0000-0000-0000000000a1"   # Sales_Lakehouse in dev
    replace_value:
      PROD: "$items.Lakehouse.Sales_Lakehouse.$id"
  # ...and one more for every warehouse, SQL endpoint, SQL database, function...
```

**With fabdep**, a one-line `fabdep.yml` (`workspace_base_name: Contoso Sales`) and:

```console
$ git switch main
$ fabdep plan
Workspace    Contoso Sales [MAIN] (00000000-0000-0000-0000-0000000000f1)
Branch       main
Environment  main
Credential   cli (default)
Tenant       not pinned

Items: 1 to create, 3 to update, 1 to unpublish
  update     Lakehouse      Sales_Lakehouse
  create     Notebook       Clean Orders
  update     Notebook       Load Orders
  update     SemanticModel  Sales
  unpublish  Notebook       Scratch

Parameters for environment main: 2 rule(s) apply
  00000000-0000-0000-0000-0000000000a1 -> $items.Lakehouse.Sales_Lakehouse.$id
      detected: Lakehouse ID; in Clean Orders.Notebook, Load Orders.Notebook
  00000000-0000-0000-0000-0000000000d1 -> $workspace.$id
      detected: Workspace ID; in Clean Orders.Notebook, Load Orders.Notebook

Guards
  fail-on-stale  pass
  unbound UDF    pass
  RLS            pass
      Sales: unchanged

The deploy would proceed.
$ fabdep
```

## Requirements

- Python 3.11 or later (tested on 3.11–3.13) and `git` on `PATH`, on Linux, macOS or
  Windows.
- A Fabric workspace in git, in the folder layout of Fabric Git integration: one folder per
  item, each with a `.platform` file.
- A way to sign in: the Azure CLI (`az login`) for the default `cli` mode, or another
  [authentication mode](#authentication).
- The Contributor role (or higher) on each target workspace, and access to the connections
  the items use. The identity needn't see the repo's other workspaces (dev's, when
  deploying `main`): fabdep then recognizes their IDs by a notebook lakehouse or a button's
  function named like the repo's. Seeing them (any role will do) lets fabdep rewrite their
  IDs wherever they occur.
- For a service principal, the Fabric tenant setting *Service principals can call Fabric
  public APIs*; for fabdep to create workspaces, also *Service principals can create
  workspaces, connections, and deployment pipelines*.

## Install

fabdep is not on PyPI. Install it from a clone, or straight from git; `OWNER` is a
placeholder, as the repository's public location isn't settled yet. Pin a release by
appending `@<tag>`, from the first public release on: older tags reference a test submodule
that isn't public.

```sh
# from a clone
git clone https://github.com/OWNER/fabric-deploy.git && cd fabric-deploy
uv tool install .            # or: pipx install .

# or straight from git
uv tool install git+https://github.com/OWNER/fabric-deploy
```

## Quickstart

```sh
cd path/to/repo/workspace     # the folder that holds the item folders
fabdep init                   # writes fabdep.yml; workspace_base_name = the folder's name
$EDITOR fabdep.yml            # set workspace_base_name, see Configuration
fabdep detect                 # offline: the IDs in the repo and their replacements
fabdep plan                   # signs in and shows what a deploy would do
fabdep                        # deploys the checked-out branch
```

By default branch `dev` deploys to the workspace `<workspace_base_name> [DEV]`, which must
exist (`-N`, or `create: true` in fabdep.yml, creates it).

> [!WARNING]
> **A full deploy removes items.** After publishing, `fabdep` unpublishes the workspace's
> items that the repo doesn't contain (orphan cleanup), of every item type fabric-cicd
> deploys or, with `-t`/`-x`, of the types in scope. `-n` (`--no-cleanup`), `-i` and `--pr`
> turn it off, and **`fabdep plan` lists every item it would unpublish.** Lakehouses,
> warehouses, SQL databases, eventhouses and a few other stateful types are left alone:
> fabric-cicd unpublishes them only behind feature flags, which fabdep doesn't set.

## Configuration

`fabdep.yml` sits in the workspace folder; `deploy`, `plan`, `refresh` and `rls pull` need
it. Only `workspace_base_name` is needed for the default naming; the rest is optional.

```yaml
workspace_base_name: Contoso Sales
workspace_name_template: "{base} [{BRANCH}]"   # the default
environments:                    # per branch, by exact name or glob (* any characters, ? one)
  main:
    workspace: "{base}"          # main deploys to "Contoso Sales"
    environment: PROD            # fabric-cicd environment (default: the branch name)
  dev:                           # "Contoso Sales [DEV]", from the template
  "feature/*":
    create: true                 # create the workspace if it doesn't exist
capacity_id: 00000000-0000-0000-0000-000000000000  # for workspaces fabdep creates
tenant: 00000000-0000-0000-0000-000000000000       # Entra tenant ID every token must match
auth: cli                        # cli | env | workload | managed | default
require_spn: true                # refuse user sign-ins
manual_parameters:
  - find_value: 00000000-0000-0000-0000-0000000000aa
    replace_value:
      PROD: 00000000-0000-0000-0000-0000000000bb
      dev: 00000000-0000-0000-0000-0000000000cc
    item_type: SemanticModel
    description: Shared lakehouse SQL endpoint
```

- **Target workspace.** A branch uses the `environments` entry of its exact name, else the
  first glob that matches it, in file order. The entry's `workspace`, else
  `workspace_name_template`, names the target; templates take `{base}`, `{branch}` and
  `{BRANCH}` (the branch upper-cased).
- **The repo's own workspaces.** IDs of the workspaces fabdep.yml can name, among those the
  identity can see, are rewritten to the target's. IDs of the other workspaces it can see
  deploy unchanged, with a warning, even where a notebook's lakehouse or a button's
  function there is named like one of the repo's. A workspace it can't see (any workspace,
  if fabdep can't list them) counts as the repo's where a notebook attaches a lakehouse
  there named like a repo lakehouse, or a report button calls a function there that a repo
  User Data Function declares. With `environments`, only the map's workspaces count, so
  give an entry to every branch the content may come from (an empty one will do, like
  `dev:` above). See [ARCHITECTURE.md](ARCHITECTURE.md#detection).
- **Environment.** fabric-cicd's environment is the entry's `environment`, else the branch
  name. It selects the `replace_value`s of `manual_parameters` and of your `parameter.yml`,
  and Variable Library value sets.
- **Creating workspaces.** `-N` creates the target and fails if it exists; `create: true`
  creates it when missing. Both use `capacity_id`, else the only active capacity (with
  several active, set `capacity_id`). The workspace is created only once the deploy's
  options, guards and parameter file have passed, so a typo or a blocked guard leaves no
  empty workspace behind.
- **`manual_parameters`** adds to detection. A detected `find_value` gets your
  `replace_value` (the `find_value` itself keeps a reference unchanged); any other becomes a
  new rule if it occurs in an item file, else it is skipped with a warning. `find_value` may
  be a list. `replace_value` is one value, or one per environment, as above (an environment
  it doesn't list keeps the detected replacement, if any); `_ALL_` covers every environment
  and must then be the only key.
- **Your `parameter.yml`.** A `parameter.yml` in the workspace folder, with the templates
  it `extend`s, is merged with the detected rules; its other sections pass through. Its
  `find_replace` entry for a `find_value` (or a template's) replaces the detected rule only
  if it has a value for the environment and no `item_type`, `item_name` or `file_path`
  filter. Otherwise both apply, the file's and its templates' entries first. A key given
  twice (ignoring case), `_ALL_` beside other keys, a missing template and a template that
  extends others are errors. The merged file is written to a temporary directory; fabdep
  never modifies the repo.
- **Validation.** Unknown top-level keys warn; unknown keys in an `environments` or
  `manual_parameters` entry fail. For editor checks, run
  `fabdep schema -o fabdep.schema.json` and start fabdep.yml with
  `# yaml-language-server: $schema=./fabdep.schema.json`.

## Commands

Commands that work on a workspace take its folder as an optional argument (default: the
current directory); put it before the options. Commands that sign in take `--auth MODE`,
and `deploy`, `plan`, `refresh` and `rls pull` take `-b BRANCH` to target another branch's
workspace. `fabdep -h` lists the commands and the deploy options, `fabdep <command> -h` the
rest.

### `fabdep [deploy]`

Publishes the folder's items to the branch's workspace with the generated rules, then runs
orphan cleanup. `deploy` is the default command.

| Purpose | Options |
| --- | --- |
| Scope | `-i NAME.TYPE ...` only these items, by `.platform` display name (not folder name); `-t TYPE ...` / `-x TYPE ...` only / all but these item types; `--pr [COMMIT]` the items the newest merged pull request (or `COMMIT`) changed |
| Cleanup | `-n` no orphan cleanup (`-i` and `--pr` imply it; with `-t`/`-x` cleanup covers only the types in scope) |
| Target | `-N` create the workspace (fails if it exists) |
| Rewriting | `-L` leave lakehouse references unchanged (a feature-branch workspace reading dev's lakehouse) |
| Guards | `-F` fail on unremappable warehouse attachments; `--allow-rls-change`; `--allow-unbound-udf` (see [Safety guards](#safety-guards)) |
| Afterwards | `--frame` refresh the semantic models the deploy published (see `refresh`) |
| Dry run | `--dry-run [--output json]`, the same as `fabdep plan` |

`--pr` looks back up to 50 commits along `HEAD`'s first-parent history for one worded as a
merged pull request by Azure DevOps, GitHub (merge or squash), GitLab or Bitbucket; a plain
`git merge` doesn't count, and a rebase merge leaves nothing to find. If `HEAD` has commits
on top of that one (a direct push, a rebase merge), `--pr` fails rather than redeploy an
older pull request: name the commit with `--pr <commit>` (`--pr HEAD` is `HEAD`'s own
changes). Changed files outside any item are ignored, a pull request that changed no item
deploys nothing (nor creates the workspace), and `--pr` never unpublishes an item the pull
request deleted. `-i` and `--pr` rely on fabric-cicd's experimental `items_to_include`,
which publishes only the items named: name what they depend on too.

### `fabdep plan`

Takes the deploy's options and makes read calls only: the target (or the workspace it would
create), the items to create, update and unpublish, each find/replace rule with its origin
and the items it hits, each guard's verdict, and the deploy's warnings. `--output json`
prints one JSON document on stdout (shape in `fabdep plan -h`). Exit status: **0** the
deploy would proceed, **3** a guard would block it, **1** error, **2** usage.

### Other commands

| Command | What it does |
| --- | --- |
| `fabdep detect [-s] [--source-workspace-id GUID]` | Lists the IDs in the item files and each one's replacement. Offline: no sign-in, and fabdep.yml isn't read. Deploy and plan also list the repo's other workspaces to recognise their IDs; give those here with `--source-workspace-id` (repeatable). `-s` writes them to `parameter.yml.detected`, a fabric-cicd parameter file. |
| `fabdep refresh [-i MODEL ...] [--no-sync] [--timeout S]` | Direct Lake framing: syncs the SQL analytics endpoints the models' Direct Lake tables read, refreshes each model (default: every one in the repo), waits up to 900 s each and fails on the first error. Other models refresh without a sync. A completed refresh doesn't prove the measures evaluate. |
| `fabdep rls pull [-i MODEL ...] [--write]` | Diffs the target's row-level security role definitions against the repo; without `--write`, exits 3 when they differ (a deploy's RLS guard would block). `--write` rewrites the repo's role files (and `ref role` lines) to match the target, for review and commit. |
| `fabdep auth status` | Signs in and prints the credential, identity, token type (user or app-only), tenant and token expiry. |
| `fabdep init [--base-name NAME] [--force]` | Writes a starter fabdep.yml, with every other key as a commented-out example. |
| `fabdep schema [-o FILE]` | Prints the JSON Schema of fabdep.yml, or writes it to `FILE`. |

For `refresh` and `rls pull`, `-i` takes a model's display name, with or without
`.SemanticModel`.

## Authentication

fabdep signs in through [azure-identity](https://pypi.org/project/azure-identity/) and hands
the same credential to fabric-cicd. The mode comes from `--auth`, else the `FABDEP_AUTH`
environment variable, else `auth:` in fabdep.yml, else `cli`:

| Mode | Signs in as | Set up |
| --- | --- | --- |
| `cli` | whoever `az login` signed in | `az login --tenant <tenant-id>` |
| `env` | a service principal | `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, and `AZURE_CLIENT_SECRET` or `AZURE_CLIENT_CERTIFICATE_PATH` |
| `workload` | a federated (OIDC) identity | `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_FEDERATED_TOKEN_FILE` |
| `managed` | the Azure host's managed identity | `AZURE_CLIENT_ID` for a user-assigned one |
| `default` | azure-identity's `DefaultAzureCredential` chain | any of the above |

- **Tenant pin.** With `tenant:` set, each token's tenant is checked before it is used and a
  token from another tenant is refused; `cli` and `workload` also ask for tokens from that
  tenant. `require_spn: true` refuses user tokens, so only a service principal or managed
  identity can deploy. These checks read the token's claims; the Fabric API verifies it.
- **Relayed tokens.** Where fabdep can't sign in itself, put an access token in
  `FABRIC_BEARER_TOKEN`, and one for the Power BI API (used by `refresh` and `--frame`) in
  `POWERBI_BEARER_TOKEN`. A relayed token takes precedence over the mode; its use is
  logged (never its value), it is checked against `tenant:` and `require_spn:`, and it
  can't be renewed, so it has to last the run.
- **Secrets.** fabdep takes no secret on its command line. Prefer workload identity
  federation or a certificate; keep a client secret in environment variables from your
  secret store, never in a command line that ends up in shell history and logs.

## Safety guards

All guards run before anything is created or published, and `fabdep plan` reports each
one's verdict.

- **Row-level security.** For every semantic model in scope, fabdep compares the target's
  live role definitions with the repo's `definition/roles/`: the roles, their rules and each
  role's `annotation PBI_Id`. Any difference blocks the deploy, including a role only one
  side has, and roles in the target for a model the repo has none for. A deploy would
  revert roles edited in the target, and the service recreates a role deployed under a
  different `PBI_Id` without its members (memberships aren't in the model definition, so
  git can't restore them). Adopt the target's roles with `fabdep rls pull --write`, or
  overwrite them on purpose with `--allow-rls-change`. A model not yet in the target passes.
- **Report buttons bound to User Data Functions** are rebound to the target's function,
  matched on the function name a repo `.UserDataFunction` item declares. If no repo item
  declares it (the repo may have none), more than one does, or the button calls a workspace
  the identity can see that isn't one of the repo's, the button would still call another
  workspace's function, so a deploy that publishes that report is blocked;
  `--allow-unbound-udf` ships it as is.
- **Notebook warehouse attachments** that can't be remapped deploy with their source IDs and
  a warning; `-F` (`--fail-on-stale`) makes that an error, for CI.
- **`-i` names** are matched against `.platform` display names: unmatched names are listed
  with the available items, and if none matches the deploy fails instead of publishing
  nothing.
- **Ambiguous and foreign references deploy unchanged, with a warning.** fabdep never picks
  the first of several candidates: a reference that doesn't name its item, when the repo has
  several items that could be it (say, two lakehouses, or a lakehouse and a mirrored
  database for a SQL endpoint ID), is left alone, as is any ID of a workspace that isn't one
  of the repo's own. Map either with `manual_parameters`.

## CI/CD

[examples/](examples/) has complete pipelines for GitHub Actions and Azure Pipelines, and the
service principal, federated credential and tenant settings they need. The core of the
GitHub one (the job needs `permissions: id-token: write`):

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0             # --pr reads the merge commit and its parent
- uses: astral-sh/setup-uv@v6
- run: uv tool install git+https://github.com/OWNER/fabric-deploy
- uses: azure/login@v2         # OpenID Connect: no stored secret
  with:
    client-id: ${{ vars.AZURE_CLIENT_ID }}
    tenant-id: ${{ vars.AZURE_TENANT_ID }}
    allow-no-subscriptions: true
- if: github.event_name == 'pull_request'   # $GITHUB_SHA: the PR's merge commit
  run: fabdep plan . --branch "$GITHUB_BASE_REF" --auth cli --pr "$GITHUB_SHA"
- if: github.event_name == 'push'
  run: fabdep deploy . --branch "$GITHUB_REF_NAME" --auth cli --pr
```

The plan previews what the push's `--pr` deploys once the pull request is merged: the
items it changes, nothing unpublished. `--pr` needs the history: in a shallow clone it
fails, asking you to fetch it, rather than deploying everything. As it never unpublishes,
run a full `fabdep` from time to time to remove items deleted from the repo.

## Output and troubleshooting

Log lines, warnings and errors go to stderr; results (`plan`, `detect`, `auth status`,
`schema`) go to stdout, so `fabdep plan --output json > plan.json` stays clean. `rls pull`
reports in log lines and its exit status. Exit status: **0** success, **1** error, **2**
usage, and **3** when `plan` finds a guard would block the deploy or `rls pull` finds the
target's roles differ from the repo's. Colour is used only on a terminal, and never when
`NO_COLOR` is set. Known cryptic Fabric errors get a plain-language hint next to the raw
message.

| Symptom | What to do |
| --- | --- |
| `No fabdep.yml in ...` | Run `fabdep init` in the workspace folder, or pass that folder as the first argument. |
| `Workspace not found: '...'` | fabdep suggests up to five similar names. Check `workspace_base_name`, the template and the branch (`-b`), and that the identity can see the workspace (`fabdep auth status`); `-N` or `create: true` creates it. |
| `HEAD is detached`, `not in a git repository` | Name the branch with `-b`; CI checkouts are often detached. |
| `fabdep.yml pins tenant ...` | The credential signed in to another tenant: `az login --tenant <tenant-id>`, or fix the mode's environment variables. |
| HTTP 401 or 403 | Run `fabdep auth status`. The identity needs Contributor on the workspace and, as a service principal, the tenant setting under [Requirements](#requirements). |
| `does not have access to the connection` | Connection IDs are tenant-wide and never rewritten; give the identity access to the connection (`fabdep detect` lists them). |
| `--pr found no pull-request commit`, `HEAD is N commit(s) past the newest pull-request commit` | Merge pull requests with a merge or squash commit, or name the commit: `--pr <commit>`. |
| An option swallowed the folder | `-i`, `-t` and `-x` take several values and `--pr` an optional one: write `fabdep DIR -t Notebook`, not `fabdep -t Notebook DIR`. |

Limitations:

- Detection reads TMDL semantic models and `.py`/`.sql` notebooks. `model.bim` models and
  `.ipynb` notebooks get a warning, and their IDs deploy unchanged unless
  `manual_parameters` maps them.
- fabdep relies on fabric-cicd internals, so it pins fabric-cicd below its next minor
  version (currently `>=1.4,<1.5`).
- `fabdep plan` approximates fabric-cicd in places: it ignores `parameter.yml` `file_path`
  filters and doesn't resolve `$` variables in a `find_value`.

## Contributing, security and license

- [CONTRIBUTING.md](CONTRIBUTING.md): setup, tests and conventions;
  [ARCHITECTURE.md](ARCHITECTURE.md): how fabdep works inside.
- [CHANGELOG.md](CHANGELOG.md): what changed in each release.
- [SECURITY.md](SECURITY.md): report vulnerabilities privately, not in an issue.
- [MIT License](LICENSE), copyright Logan Teder.
