# Changelog

One-line status per release. Newest first. Dates are release/commit dates.

## [1.7.0] — Unreleased
`fabdep rls` (pull/diff live RLS role definitions) + deploy guard that blocks a deploy from silently changing a target model's live RLS. Deploy header prints an `Options:` line with every non-default flag in effect. Guard UX: announce the slow live-definition read before it starts, and keep a clean check quiet (definitions-vs-memberships note only prints when a change is actually permitted/blocked). Auth UX: definitively-expired credentials (AADSTS7000222 secret expiry, refresh-token expiry codes) print a rotate/re-login hint instead of the account list. Detection: `Sql.Databases("host")` (server-level navigation form) now yields the SQL-endpoint connection-string parameter — previously only two-arg `Sql.Database("host","db")` matched, so import-mode models built with the navigation form (Woodgrove) deployed to prod still pointing at the dev endpoint.

## [1.6.2] — 2026-07-21
Null-UUID guard, `.platform`-typed item discovery, domain-rule fixes.

## [1.6.1] — Unreleased
Remap notebook `dependencies.warehouse` bindings (T-SQL `Lakewarehouse` → lakehouse `$sqlendpointid`, `MountedWarehouse` → target-workspace SQLEndpoint literal, `Datawarehouse` → Warehouse `$id`); recursive item-name detection so nested `.SQLDatabase`/`.Lakehouse` folders resolve; warn (and `--fail-on-stale`) when a warehouse attachment would deploy with a source-workspace GUID.

## [1.6.0] — Unreleased
Internal layered-architecture refactor — **no public API or CLI change** (the only declared export, `WorkspaceDeployer`, keeps its exact interface). The two god-modules split into focused layers: `identity` (credential/token/tenant-pin), `transport` (HTTP + LRO polling), `client` (Fabric/Power BI domain calls, was `api.py`), `profile` (fabdep-profile I/O), `scope` (repo item introspection + deploy-scope), `params` (parameter.yml lifecycle). `WorkspaceDeployer` slimmed 781→380 lines into an orchestrator. The relay auth is now a labeled fallback inside `identity` rather than smeared through the REST module.

## [1.5.1] — 2026-06-25
Test-suite consolidation onto the parametrized house style (~110 fewer LOC); no runtime change.

## [1.5.0] — 2026-06-24
Direct Lake framing: `fabdep refresh` / `fabdep deploy --frame` (sync SQL endpoint → refresh → poll → surface first error); cryptic-error hints; `--include-items` fails loudly on a name mismatch and confirms what deployed.

## [1.4.0] — 2026-06-22
Multi-tenant `tenant:` pin (registry alias resolved per-tenant) and `FABRIC_BEARER_TOKEN` relay auth for CA-blocked tenants.

## [1.3.1] — 2026-05-31
Bump `fabric-cicd` to 1.1.0.

## [1.3.0] — 2026-05-13
`--fail-on-stale` deploy flag (exit non-zero when a notebook has multiple lakehouses attached).

## [1.2.0] — 2026-05-13
Scan T-SQL notebooks for source IDs and warn on stale lakehouse attachments.

## [1.1.0] — 2026-05-08
Drop auto-detected `item_type` scoping from `parameter.yml`; scope-filter cleanups.

## [1.0.1] — 2026-05-07
Detect OneLake DFS URLs in Direct Lake models; allow `auth switch` by local alias; auto-resolve SQL Database connection strings.

## [1.0.0] — 2026-05-04
First stable release: `FabdepConfig` extracted, git/scope helpers modularized, unit-test layer added behind an integration marker.

## [0.3.0] — 2026-05-04
Move to `fabric-cicd` 1.0 (explicit `token_credential`); persist SPN logins and aliases in the fabdep profile; slimmer error output.

## [0.2.4] — 2026-04-03
Auth rename is local-only; case-insensitive item types; uppercase branch suffix; label accounts as User vs SPN.

## [0.2.3] — 2026-02-23
Bump `fabric-cicd`; new logging display.

## [0.2.2] — 2026-02-12
`--skip-lakehouse` (feature-branch mode), per-item filtering, and `rename` bugfixes.

## [0.2.1] — 2026-01-25
Recursive `.platform` scan with type filtering; remove bootstrap mode; parameter-cleanup fixes.

## [0.2.0] — 2025-12-15
SQL Database parameter detection, configurable retries, `-N` workspace validation, and a pytest harness.

## [0.1.3] — 2025-11-10
Branch-based workspace resolution and workspace auto-detection.

## [0.1.2] — 2025-11-07
Dynamic source-ID generation and bad-connection-ID detection.

## [0.1.1] — 2025-11-05
Auth error handling and documentation.

## [0.1.0] — 2025-11-04
Initial release: `fabdep` CLI with deploy/rename/detect and the workspace/CLI module split.
