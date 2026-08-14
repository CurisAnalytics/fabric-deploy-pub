# Changelog

One-line status per release. Newest first. Dates are release/commit dates.

## [1.8.2] — Unreleased
Identity: token acquisition falls back to another logged-in identity in the same tenant instead of failing the run (empty tenant registry no longer hard-fails a committed `tenant:` alias). Logging: fabdep messages no longer double-print (console handler attaches only to the top-level logger of a tree). **SQL database hostnames no longer gain a `,1433` suffix.** The `$items.SQLDatabase.<name>.$sqlendpoint` dynamic resolves to the raw `serverFqdn`, which the Fabric API always returns ADO.NET-style with a port suffix (`<host>.database.fabric.microsoft.com,1433` — verified across three live databases; not transient). The detected find_value is a bare host, so the dynamic injected the port into every occurrence: tolerable in M expressions (`Sql.Database("host,1433", …)` connects), but fatal in notebook JDBC URLs composed as `f"{hostname}:{port}"` → `host,1433:1433` (surfaced by a client migration). SQL-database connection-string parameters now resolve at deploy time to a literal bare host read from the target workspace's typed `/sqlDatabases` endpoint (same pattern as the 1.6.1 MountedWarehouse literal); on lookup failure fabdep warns and falls back to the old dynamic.

## [1.8.0] — Unreleased
**cross-workspace-reference fix: cross-workspace pipeline references no longer rewritten.** Detection used to map *every* non-null `workspaceId` in DataPipeline/CopyJob JSON to `$workspace.$id`, so a pipeline invoking an item in another workspace (one workspace's `merge_shared_facts` → a notebook in another workspace; also every client-workspace invoke in other workspaces' orchestrators) had that reference silently rewritten to the deploy target. Now only workspace ids anchored as the repo's own (from notebook attachments and semantic-model OneLake URLs) are remapped; anything else is recorded as a `Cross-workspace reference` with no replacement — it deploys verbatim, warns, and remains overridable via a `manual_parameters` entry. A foreign CopyJob section's `artifactId` is likewise no longer mapped onto the repo's Lakehouse. `fabric-cicd` 1.2.0 → 1.3.0 (floor raised to `>=1.3`). Detection: **MirroredDatabase SQL analytics endpoints now auto-resolve** — a `datawarehouse.*` connection string / endpoint item id in a repo whose SQL-endpoint item is a `.MirroredDatabase` maps to fabric-cicd 1.3's new `$items.MirroredDatabase.<name>.$sqlendpoint` / `$sqlendpointid` dynamic variables (Lakehouse still wins first-match when both item types are present; this also resolves T-SQL notebook `lakewarehouse` attachments in mirrored-only repos). Upstream fixes picked up with the bump: deploys no longer fail when item attributes are still provisioning asynchronously (fabric-cicd #887 — the fresh `-N` SQLDatabase caveat), and the `spark_pool` `item_name` list filter works again (#1071). New test pins fabdep's appended feature flags (`enable_experimental_features`, `enable_items_to_include`) against fabric-cicd 1.3's `get_supported_feature_flags()` so an upstream rename fails in CI, not mid-deploy. fabric-cicd 1.3 also honors `FABRIC_CICD_RETRY_API_MAX_DURATION_SECONDS` (default 300s) for slow LRO polls; fabdep passes the environment through untouched, so it works with no fabdep change.

## [1.7.0] — Unreleased
`--pr` deploy filter: resolve the latest pull-request commit (ADO `Merged PR N:`, GitHub merge/squash, or any merge commit), map its changed files onto the items that own them, and deploy only those — the by-hand diff read before a promotion, automated. PBIR **User Data Function buttons are rebound to the target workspace**: report definitions are now scanned, and a button's source-workspace `itemId`/`workspaceId` are parameterized when the structure matches and the named function resolves to exactly one repo `.UserDataFunction` item; ambiguous references warn and deploy untouched, and every rebind is announced. `--allow-rls-change` now skips the RLS check outright instead of paying ~30s/model to warn about a settled decision. `fabdep rls` (pull/diff live RLS role definitions) + deploy guard that blocks a deploy from silently changing a target model's live RLS. Deploy header prints an `Options:` line with every non-default flag in effect. Guard UX: announce the slow live-definition read before it starts, and keep a clean check quiet (definitions-vs-memberships note only prints when a change is actually permitted/blocked). Auth UX: definitively-expired credentials (AADSTS7000222 secret expiry, refresh-token expiry codes) print a rotate/re-login hint instead of the account list. Detection: `Sql.Databases("host")` (server-level navigation form) now yields the SQL-endpoint connection-string parameter — previously only two-arg `Sql.Database("host","db")` matched, so import-mode models built with the navigation form (Woodgrove) deployed to prod still pointing at the dev endpoint. Dependencies fully upgraded to clear the open Dependabot advisories (urllib3, cryptography, PyJWT, requests, idna, Pygments, azure-core, pytest); `fabric-cicd` 1.1.0 → 1.2.0, direct floors raised to the patched versions.

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
