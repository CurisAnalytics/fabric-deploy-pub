# Architecture

How fabdep is put together, for people changing it. Usage is in [README.md](README.md);
setup, tests and conventions are in [CONTRIBUTING.md](CONTRIBUTING.md).

## What fabdep adds to fabric-cicd

[fabric-cicd](https://microsoft.github.io/fabric-cicd/) publishes a folder of item definitions
into a workspace given by id, applying the find/replace rules of a `parameter.yml` someone
wrote by hand. fabdep wraps it and adds:

- **Target resolution**: the workspace comes from `fabdep.yml` and the git branch (a name
  template or an `environments:` map), found by name, created on request.
- **Generated parameter rules**: detection reads the repo's item files and emits the
  find/replace rules that point ids at the target, merged with the user's own `parameter.yml`.
- **Cross-workspace awareness**: ids of the repo's other environments are rewritten; ids of
  unrelated workspaces deploy unchanged.
- **Literals where fabric-cicd has no dynamic variable**: SQL database hosts and SQL database
  endpoint ids are looked up in the target.
- **Guards before publishing**: live row-level security, report buttons bound to User Data
  Functions, notebook warehouse attachments that can't be remapped.
- **Scope**: item types (`-t`, `-x`), named items (`-i`), the items a pull request changed (`--pr`).
- **Identity**: a credential mode, a tenant pin and `require_spn`, checked on every token; one
  cached credential shared with fabric-cicd.
- **After a deploy**: Direct Lake framing (`refresh`, `deploy --frame`).
- **Tooling**: `init`, `schema`, `detect`, `rls pull`, `auth status`, and plain-language hints
  for opaque Fabric errors.

## Modules

All in `src/fabric_deploy/`.

| Module | Responsibility |
| --- | --- |
| `cli` | Argument parsing, command handlers, user-facing messages and exit codes; `deploy` is the default command. |
| `config` | Reads and validates `fabdep.yml` (`FabdepConfig`, `AuthSettings`): target workspace name, fabric-cicd environment, `create`, the pattern of the repo's workspace names. No network. |
| `identity` | Credential mode, the shared per-scope token cache, the tenant and `require_spn` check on every token, relayed tokens. |
| `transport` | HTTP: timeout, retries on 429/5xx honouring `Retry-After`, pagination, long-running-operation polling; API roots from fabric-cicd's constants. |
| `client` | Fabric and Power BI endpoints: workspaces, capacities, items, SQL endpoint sync, model refresh, model definitions. |
| `scope` | The repo's items (from `.platform` files), literal search in item files, type and item scope, `-i` validation, `--pr` path-to-item mapping. No network. |
| `detect` | `ParameterDetector`: finds ids in item files, classifies them, maps each to a fabric-cicd dynamic variable. No network. |
| `params` | `manual_parameters`, literals looked up in the target, and the effective parameter file (`ParameterFile`). |
| `workspace` | `WorkspaceDeployer`, the deploy orchestration and the public Python API. |
| `rls` | Row-level-security role diff: `rls pull` and the deploy guard. |
| `refresh` | Direct Lake framing: SQL endpoint sync, model refresh, poll, first error. |
| `git` | Current branch, the latest pull-request commit, the paths a commit changed. |
| `log` | Console output (stderr; colour only on a terminal without `NO_COLOR`), fabric-cicd log formatting, error classification and hints. |
| `scaffold` | The starter `fabdep.yml` (`fabdep init`) and the packaged `fabdep.schema.json` (`fabdep schema`). |
| `plan` | `fabdep plan` and `deploy --dry-run`: runs a dry-run `WorkspaceDeployer` through scope, guard verdicts and parameter rules, lists the target's items to tell creates, updates and orphan unpublishes apart, and renders the result as text or JSON. Read calls only. |

Imports point one way: `cli` → orchestration (`plan`, `workspace`, `refresh`, `rls`) → `params`,
`detect`, `scope`, `client` → `transport` → `identity` → `config` → `git`. `log` is used
throughout; `scope`, `git` and `scaffold` import nothing from the package. Only `cli` prints
results and exits; the other modules log and raise.

## Deploy flow

`fabdep deploy` builds a `WorkspaceDeployer` (steps 1-5: reads and lookups only, except that
`-N` or `create: true` may create the workspace) and calls `deploy()` (steps 6-11).

1. **Config.** `FabdepConfig.from_path` reads `fabdep.yml` (required) and takes the branch
   from `--branch` or git. The branch picks an `environments:` entry (its exact key, else the
   first glob that matches). The entry's `workspace`, else `workspace_name_template` (default
   `{base} [{BRANCH}]`), names the target. The fabric-cicd environment is the entry's
   `environment`, else the branch name.
2. **Identity.** `apply_auth_settings` installs the shared credential: `--auth`, else
   `FABDEP_AUTH`, else `auth:`, else `cli`. Every token it mints or reads from
   `FABRIC_BEARER_TOKEN` / `POWERBI_BEARER_TOKEN` is checked against `tenant:` and
   `require_spn:` before use.
3. **Target.** The workspace is looked up by name. `-N` creates it and fails if it exists;
   `create: true` creates it only when missing, on `capacity_id` or the only active capacity.
4. **Anchors.** Every visible workspace whose name `fabdep.yml` could produce is a sibling.
   For each sibling an item file mentions, its lakehouses are listed.
5. **Detection.** `ParameterDetector.detect_all()` (see below). Then `-L` drops the lakehouse
   rules, `manual_parameters` override or add rules for the environment, and SQL database
   hosts and endpoint ids are looked up in the target.
6. **Scope.** `--pr` becomes the items its pull-request commit changed; `-i` names are checked
   against `.platform` display names (no match is an error); `-t`/`-x` become fabric-cicd's
   `item_type_in_scope`.
7. **Guards**, before anything is published: `--fail-on-stale` (unremappable warehouse
   attachments), report buttons whose User Data Function can't be rebound
   (`--allow-unbound-udf`), and every in-scope semantic model's live RLS roles
   (`--allow-rls-change`).
8. **Parameter file.** The repo's `parameter.yml`, if any, plus the detected rules whose
   `find_value` it doesn't already list, written to a temporary directory and passed as
   `parameter_file_path`. With nothing to add, fabric-cicd reads the repo's file itself. When
   every detected workspace id already is the target's, only `manual_parameters` rules are added.
9. **Publish.** `FabricWorkspace(...)` with the target id, environment, type scope,
   parameter file and the shared credential, then `publish_all_items` (`items_to_include` for
   `-i` and `--pr`).
10. **Orphan cleanup.** `unpublish_all_orphan_items` removes workspace items the repo doesn't
    contain, unless `-n`, `-i` or `--pr` is in effect. The temporary file is removed either way.
11. **Framing** (`--frame`). `refresh.frame_models` for the semantic models the deploy
    published: sync the SQL analytics endpoints their Direct Lake tables read, refresh each
    model, poll, raise the first error.

`refresh` and `rls pull` run steps 1-3 (without creating anything), then their own work.

## Detection

`ParameterDetector` reads item files only. Each id it finds becomes a `DetectedParameter`:
the value, what kind of reference it is, the files it appears in, and its replacement (a
fabric-cicd dynamic variable, a literal, or none).

| Item | Files read | References |
| --- | --- | --- |
| Notebook | `notebook-content.py`, `notebook-content.sql` (`META` blocks), `notebook-settings.json` | default lakehouse and its workspace; warehouse attachments; lakehouse auto-binding |
| SemanticModel | `definition/**/*.tmdl` | Direct Lake OneLake sources (`<workspace>/<lakehouse>`); `Sql.Database(s)("<host>", "<db>")` hosts and item ids |
| Report | `definition/**/*.json`, only when the repo has a UserDataFunction | User Data Function button bindings |
| DataPipeline | `pipeline-content.json` | connection ids; `workspaceId` in activities |
| CopyJob | `copyjob-content.json` | source and destination `workspaceId` and `artifactId` |

`model.bim` models and `.ipynb` notebooks are not read; a warning names them.

### Workspace ids

- **The repo's own**: the target and the *anchors*, the workspaces the content ties to the
  repo's own items (a notebook attached to a repo lakehouse, a Direct Lake source on one, a
  button bound to a repo User Data Function) plus the siblings the deployer listed (for
  `detect`, the `--source-workspace-id` values). They map to `$workspace.$id`. A sibling's id
  gets a rule only where an item file contains it; its lakehouses' ids map to the repo
  lakehouse of the same name.
- **The null UUID**: Fabric's "this workspace" placeholder, also written by lakehouse
  auto-binding. No rule; fabric-cicd resolves auto-bound lakehouses by logicalId.
- **Foreign**: any other workspace, e.g. a pipeline invoking a notebook elsewhere, a Direct
  Lake model on a shared lakehouse, a CopyJob writing to another workspace. Recorded as a
  cross-workspace reference with no replacement, so it and the item id that comes with it
  deploy unchanged, with a warning. A `manual_parameters` entry can remap it.

A sibling is a workspace whose display name `fabdep.yml` could give some branch: a name the
`environments:` map produces (a glob key stands for its branches) or, without a map,
`workspace_name_template` with any branch. `{branch}` matches no `]`, so `{base} [DEV] [OLD]`
is not a sibling of `{base} [DEV]`.

### Item ids

| Reference | Replacement |
| --- | --- |
| Workspace id | `$workspace.$id` |
| Lakehouse id | `$items.Lakehouse.<name>.$id` |
| Lakehouse or mirrored database SQL endpoint host / id | `$items.<Type>.<name>.$sqlendpoint` / `$sqlendpointid` |
| SQL database id | `$items.SQLDatabase.<name>.$id` |
| SQL database host | the target database's bare host, looked up (fabric-cicd's `$sqlendpoint` adds `,1433`) |
| SQL database endpoint id (notebook `MountedWarehouse`) | looked up in the target (no dynamic variable exists) |
| Warehouse id | `$items.Warehouse.<name>.$id` |
| User Data Function id (report button) | `$items.UserDataFunction.<name>.$id` |
| Connection id | none: connections are tenant-wide |

A reference that names its item (`default_lakehouse_name`, a database's `<name>-<id>`, the
function a button calls) maps to the repo item of that name, or declaring that function, or
to nothing. An unnamed one maps to the repo's only candidate; with several, it deploys
unchanged with a warning. Hosts are per workspace, so any matching item resolves them.

## Invariants

Changes must keep these true.

- **The repo is never modified by a deploy.** The effective parameter file lives in a
  temporary directory. Only explicit requests write into the repo: `init`, `detect --save`,
  `rls pull --write`, `schema -o`.
- **Read-only commands make no writes**, to Fabric or to the repo: `detect`, `auth status`,
  `rls pull` without `--write`, `plan`.
- **Fail closed on ambiguity.** Never pick the first of several candidates: an id fabdep can't
  place deploys unchanged, with a warning. Whatever could make a deploy ship more, less or
  elsewhere than intended is an error: `-i` matching nothing, `--pr` without a pull-request
  commit or in a shallow clone, several active capacities without `capacity_id`, an unbound
  User Data Function button, any RLS difference, an unknown key in an `environments:` or
  `manual_parameters` entry.
- **Only the repo's own workspaces are rewritten.** An id of an unrelated workspace is never
  pointed at the target.
- **Guards run before anything is published or unpublished.**
- **One identity.** fabdep's REST calls and fabric-cicd use the same checked credential.
- **Tests never touch the network** or the developer's sign-in (see CONTRIBUTING.md).

## fabric-cicd coupling

fabdep relies on `FabricWorkspace`, `publish_all_items`, `unpublish_all_orphan_items`,
`append_feature_flag`, `constants.ACCEPTED_ITEM_TYPES`, the API root constants, the constants
`plan` uses to order items and to tell which orphans cleanup leaves
(`SERIAL_ITEM_PUBLISH_ORDER`, `UNPUBLISH_FLAG_MAPPING`, `FEATURE_FLAG`), the semantics of
`parameter.yml` (dynamic variables, `_ALL_`, `extend`) and fabric-cicd's logger. That is
why `pyproject.toml` caps fabric-cicd below its next minor version: raise the cap only after
the test suite passes against the new release.
