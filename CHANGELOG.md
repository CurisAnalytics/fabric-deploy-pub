# Changelog

All notable changes to fabric-deploy (`fabdep`) are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Releases before the first public one are summarized in a few lines each.

## Unreleased

## 2.0.0 - 2026-10-29

### Breaking

- `tenant:` must be a tenant ID (GUID) and every token must match it, relayed ones included;
  no fallback to another signed-in identity. `require_spn:` is a top-level key.
- `fabdep.yml` is required (no workspace name guessed from the folder). `capacity_id` must be
  a GUID, and is needed to create a workspace when several capacities are active.
- fabric-cicd's environment is the branch name (or its `environments:` entry's), not `fabdep`.
  The repo's `parameter.yml` and the templates it `extend`s now apply: their entry for a
  `find_value` replaces the detected rule if it has a value for the environment and no item
  or file filter, else both apply, theirs first. A missing template, or one that extends
  others, is an error.
- `detect --save` keys replacements by `_ALL_` instead of `prod`.
- Malformed `manual_parameters` entries fail the deploy. A `replace_value` mapping is read by
  environment, and `_ALL_` must be its only key, as fabric-cicd requires.
- An unnamed reference with several candidate items, of any type that could own it, and a
  Direct Lake source or notebook lakehouse in another workspace (even named like a repo
  lakehouse, when fabdep can see that workspace and fabdep.yml doesn't name it) deploy
  unchanged with a warning, not rebound.
- Models on a `pbidedicated.windows.net` SQL host get the endpoint's `fabric.microsoft.com` host.
- `--fail-on-stale` covers only warehouse attachments that can't be remapped.
- The RLS guard checks every in-scope model, also one without roles in the repo, and blocks
  a changed role `PBI_Id` (the service would drop the role's members).
- A report button bound to a User Data Function no repo item declares, or in a workspace
  fabdep can see and fabdep.yml doesn't name, blocks the deploy (unless
  `--allow-unbound-udf`), also in a repo with no User Data Function.
- Bare `--pr` takes the newest commit worded as a merged pull request (not a plain merge) in
  HEAD's first-parent history, and fails when HEAD is past it (a rebase merge, a direct
  push): pass `--pr <commit>`. `--pr HEAD` means HEAD's own changes.
- An unknown `-t`/`-x` item type is an error naming the closest type.
- `fabdep rls pull` without `--write` exits 3 when the roles differ (was 1), like `plan` when
  a guard would block; 1 means an error.
- Errors go to stderr; colour only on a terminal without `NO_COLOR`. A directory argument
  that doesn't exist is a usage error (exit 2).
- Python API: `WorkspaceDeployer` drops `max_retries`, `repo_item_types` and
  `deploy(banner=)` and gains `environment`; `TenantPin` is `AuthSettings`.

### Added

- MIT license.
- `environments:` maps branches (names or globs) to a `workspace`, an `environment` and
  `create: true`; `workspace_name_template` sets the default name (warned about when it is
  the branch alone).
- `auth:`, `--auth` and `FABDEP_AUTH` choose the credential: `cli`, `env`, `workload`,
  `managed` or `default`. `fabdep auth status` shows who fabdep signs in as.
- `fabdep plan` (or `deploy --dry-run`) shows what a deploy would change, without changing
  anything; `--output json` for tools.
- `fabdep init` writes a starter `fabdep.yml`; `fabdep schema` prints its JSON Schema.
- `examples/` for GitHub Actions and Azure Pipelines.
- A `manual_parameters` `find_value` can be a list.
- API calls follow fabric-cicd's `FABRIC_API_ROOT_URL` / `DEFAULT_API_ROOT_URL`.

### Changed

- `refresh` and `--frame` sync every SQL endpoint the Direct Lake models read, and none for
  other models.
- Tokens are cached and shared with fabric-cicd; a relayed token's use is logged (never its
  value).
- "Workspace not found" suggests similar names; `fabdep -h` shows the deploy options.
- A mistyped command (`fabdep refesh`) suggests the command, and a removed command or option
  says it was removed, instead of being read as the workspace directory.
- `fabdep detect` prints its results to stdout, so they can be redirected; warnings stay on
  stderr.
- `-N` creates the workspace only once the deploy's options, guards and parameter file pass.
- A pull request that changed no item deploys nothing with `--pr` (was: an error), creating
  no workspace; its plan says so.
- A top-level `fabdep.yml` key that looks like a misspelled `tenant`, `auth` or `require_spn`
  (`tenan:`) is an error; other unknown keys still only warn.

### Fixed

- A deploy or plan whose scope has no repo item (a folder without items, a `-t` type the repo
  lacks) is an error while orphan cleanup is on, instead of unpublishing the target's items.
- A CopyJob's `artifactId` maps to a lakehouse or a warehouse by its connection's `type`; any
  other type deploys unchanged with a warning (it was always taken for a lakehouse).
- With an `environments:` map, the workspaces `workspace_name_template` names (when it uses
  `{base}`) stay the repo's own, so a map naming only `main` no longer leaves dev's IDs
  unchanged.
- `plan` (and `--output json`) lists `fabdep.yml`'s warnings, such as an unknown key, with
  its other warnings.
- The RLS guard warns about a `model.bim` semantic model instead of blocking the deploy (its
  roles aren't read), and `rls pull` leaves such a model alone instead of writing TMDL role
  files into it.
- `refresh` and `--frame` fail when a SQL endpoint table fails to sync, before refreshing any
  model (the failure was only a warning).
- With `--pr`, the item-filter and orphan-cleanup warnings name `--pr`, not `--include-items`.
- The CI examples say that GitHub drops a pending `--pr` deploy when another merge lands, and
  that a periodic full deploy catches what it missed.
- Repos without a lakehouse deploy; orphan cleanup no longer spares items named like it.
- Requests time out and retry 429, and 5xx on reads, honouring `Retry-After`.
- Lists follow pagination (stopping at a repeated token or 1000 pages). An unreadable model
  definition is an error (`rls pull --write` could delete every role file).
- `--frame` refreshes only the models the deploy published; `-L` holds when it drops every rule.
- `--pr` works from a git subfolder, and fails in a shallow clone instead of deploying all.
- Outside a git repository, or on a detached HEAD, the error says to pass `--branch`.
- Files with a UTF-8 BOM are read; a 404 is no longer reported as a sign-in problem.
- `manual_parameters` apply when deploying back to the workspace the IDs came from.
- Windows: sign-in works, and consoles that can't print `✓` no longer crash.

### Removed

- `fabdep rename`; `--max-retries` (`-r`), as requests now retry on their own.
- `fabdep auth list`, `switch`, `rename`, `login` and `bind`, the fabdep profile and tenant
  aliases: set `tenant:` to the tenant ID and choose the credential with `auth:`.

## 1.11.1 - 2026-10-21

The `fabdep vX` header prints before anything else; the run summary names the source and
target workspaces. The RLS guard reads the target's models in parallel.

## 1.11.0 - 2026-10-21

Support for Fabric's Lakehouse Auto-Binding in Git: when notebook metadata no longer names
the source workspace, fabdep lists the `{base} [...]` sibling workspaces and remaps their ids
(and their lakehouses' ids) to the target. New `fabdep detect --source-workspace-id`.
`-L` warns that auto-bound notebooks rebind anyway.

## 1.10.0 - 2026-10-16

`fabdep auth bind <alias> <tenant|account>` registers a tenant alias against an identity
already signed in with `az`, so the `tenant:` pin selects it without a separate config directory.
Tenant-pin messages list the tenants `az` can see; unknown `fabdep.yml` keys warn.
`fabric-cicd` 1.3 → 1.4 (Graph Model and Cosmos DB items); patched `pyjwt` and `urllib3`.

## 1.9.0 - 2026-09-03

`manual_parameters` entries that detection didn't produce are injected as find/replace rules
when their value occurs in an item file, covering references detection has no model for
(e.g. a PowerApp link in a report).

## 1.8.2 - 2026-08-14

SQL Database hostnames resolve to a bare host, without the `,1433` suffix that broke notebook
JDBC URLs. Token acquisition falls back to another identity in the same tenant; log lines no
longer print twice; null-UUID CopyJob workspace ids are treated as local.

## 1.8.1 - 2026-08-13

A deploy blocks when an in-scope report has a User Data Function button that can't be rebound
to the target workspace; `--allow-unbound-udf` ships it as-is.
The deploy summary no longer lists excluded items as deployed.

## 1.8.0 - 2026-08-10

`--pr`: deploy only the items the latest pull request touched.
Report User Data Function buttons rebind to the target workspace; cross-workspace pipeline
references deploy verbatim instead of being rewritten to the target (override with
`manual_parameters`).
Detection resolves MirroredDatabase SQL analytics endpoints and the server-level
`Sql.Databases("host")` form. `fabric-cicd` 1.1 → 1.3; dependencies upgraded to patched versions.

## 1.7.0 - 2026-07-24

`fabdep rls` pulls and diffs live row-level-security role definitions, and a deploy guard
blocks silent changes to a target model's live RLS (`--allow-rls-change` to proceed).
Expired credentials print a re-login hint instead of the account list.

## 1.6.2 - 2026-07-17

Items are discovered by their `.platform` type rather than folder suffix; no find/replace rule
is emitted for the null UUID; domain-rewrite rule fixes.

## 1.6.1 - 2026-07-08

Notebook `dependencies.warehouse` bindings are remapped to the target workspace; item names
are detected in nested folders; `--fail-on-stale` also covers warehouse attachments that would
deploy with a source-workspace id.

## 1.6.0 - 2026-06-29

Internal restructuring into layered modules (`identity`, `transport`, `client`, `profile`,
`scope`, `params`); no CLI or public API change.

## 1.5.1 - 2026-06-25

More plain-language hints for Direct Lake errors (hybrid tables, storage mode, compatibility
level); `refresh -i` accepts a trailing `.SemanticModel` like `deploy -i`; workspace item
listing follows pagination; refresh failures show the underlying error.

## 1.5.0 - 2026-06-24

Direct Lake framing: `fabdep refresh` and `fabdep deploy --frame` sync the SQL endpoint,
refresh the model, poll, and surface the first error. Plain-language hints for cryptic Fabric
errors; `--include-items` fails loudly on a name mismatch and confirms what deployed.
`FABRIC_BEARER_TOKEN`: use a token relayed from another machine when Conditional Access
blocks signing in locally.

## 1.4.0 - 2026-06-22

`tenant:` pin in `fabdep.yml`: a registered alias selects the identity for each tenant, and a
deploy refuses to run in the wrong tenant.

## 1.3.1 - 2026-06-01

`fabric-cicd` 1.0 → 1.1.

## 1.3.0 - 2026-05-13

`--fail-on-stale`: exit non-zero when a notebook has several lakehouses attached.

## 1.2.0 - 2026-05-13

T-SQL notebooks are scanned for source ids; stale lakehouse attachments warn.

## 1.1.0 - 2026-05-08

Detected parameters are no longer scoped by `item_type` in `parameter.yml`; scope-filter fixes.

## 1.0.1 - 2026-05-07

OneLake DFS URLs in Direct Lake models are detected; `auth switch` accepts a local alias;
SQL Database connection strings auto-resolve.

## 1.0.0 - 2026-05-04

First stable release. Configuration loading is separated from deployment; unit tests run
apart from the live integration tests.

## 0.3.0 - 2026-05-04

`fabric-cicd` 1.0 (explicit `token_credential`); service-principal logins and aliases persist
in the fabdep profile; shorter error output.

## 0.2.4 - 2026-04-03

`auth rename` is local-only; item types are case-insensitive; the branch suffix in workspace
names is upper-cased; accounts are labelled User or SPN.

## 0.2.3 - 2026-02-23

`fabric-cicd` 0.1 → 0.2; new log display.

## 0.2.2 - 2026-02-12

`--skip-lakehouse` for feature branches that share the dev lakehouse; per-item filtering;
`rename` fixes. `deploy -s` and `-A` removed.

## 0.2.1 - 2026-01-25

Recursive `.platform` scan with type filtering; bootstrap mode removed; parameter-cleanup fixes.

## 0.2.0 - 2025-12-15

SQL Database parameter detection, configurable retries, `-N` workspace validation, and a
pytest suite.

## 0.1.3 - 2025-11-10

Target workspace resolved from the git branch; workspace auto-detection.

## 0.1.2 - 2025-11-07

`fabdep detect`: generate source-id parameters from repository files; bad connection ids are
reported.

## 0.1.1 - 2025-11-05

Auth error handling and documentation.

## 0.1.0 - 2025-11-04

Initial release: the `fabdep` CLI with `deploy`, `rename` and `auth`.
