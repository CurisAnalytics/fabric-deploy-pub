# Changelog

Brief status per release. Newest first. Dates are release/commit dates.

## [Unreleased]
RLS guard: a role whose `annotation PBI_Id` would change blocks the deploy (the service recreates it without its members; `rls pull --write` adopts the target's id), models with no roles in the repo are checked too, and an unreadable model definition is an error instead of "no roles".
`--pr` only counts commits worded as a merged pull request on HEAD's first-parent line (a plain local merge no longer does), and stops when HEAD has commits on top of that PR commit instead of deploying an older PR. An unknown `-t`/`-x` item type is an error.
`refresh` / `--frame` wait out a queued refresh and stop when a SQL endpoint table fails to sync; API requests time out after 60 s.

## [1.11.1] — 2026-10-21
The `fabdep vX` header (renamed from "Fabric CI/CD") is printed before anything else; the run summary names the source and target workspaces and drops the parameter-file timestamp. The RLS guard reads the target's models in parallel.

## [1.11.0] — 2026-10-21
Lakehouse auto-binding in git: notebooks whose metadata carries the lakehouse logicalId and the null-UUID workspace no longer anchor the source workspace, so the deployer anchors every `{base} [...]` sibling workspace it can list and maps the referenced siblings' lakehouse ids where code still carries them; `fabdep detect --source-workspace-id`. Anchoring is order-independent (a bound UDF button now anchors pipeline references too). `-L` warns that auto-bound notebooks rebind anyway.

## [1.10.0] — 2026-10-16
`fabdep auth bind <alias> <tenant|account>`: register a tenant alias against an identity already in the shared `az` login — the pin then selects it per deploy without an isolated config dir or `auth switch`.
Pin warnings/errors list the tenants `az` can see; `fabdep.yml` warns on unknown keys (`tenan:`).
`fabric-cicd` 1.3 → 1.4 (Graph Model / Cosmos DB items, Activator workspace-id fix); dependency upgrade clears the open `pyjwt`/`urllib3` advisories.

## [1.9.0] — 2026-08-14
`manual_parameters` entries fabdep didn't auto-detect are now injected as find/replace rules when the value occurs in an item file — covers references detection has no model for (e.g. a PowerApp link in a report).

## [1.8.2] — 2026-08-14
SQL Database hostnames resolve to a literal bare host: the `$sqlendpoint` dynamic returns a `,1433` suffix that broke notebook JDBC URLs.
Fixes: token falls back to another identity in the same tenant; no more double-printed log lines; null-UUID CopyJob workspaceIds treated as local.

## [1.8.0] — 2026-08-10
Cross-workspace pipeline references deploy verbatim instead of being rewritten to the target workspace — override with `manual_parameters`.
MirroredDatabase SQL analytics endpoints auto-resolve. `fabric-cicd` 1.2 → 1.3.

## [1.7.0] — 2026-07-24
`--pr`: deploy only the items the latest pull request touched.
Report User Data Function buttons rebind to the target workspace; `--allow-unbound-udf` ships them as-is.
`fabdep rls` pull/diff, plus a deploy guard against silently changing a model's live RLS.
Detection: server-level `Sql.Databases("host")` now yields the SQL-endpoint parameter. Dependency upgrades clear the open advisories.

## [1.6.2] — 2026-07-21
Null-UUID guard, `.platform`-typed item discovery, domain-rule fixes.

## [1.6.1] — 2026-07-08
Remap notebook `dependencies.warehouse` bindings to the target workspace; recursive item-name detection for nested folders; `--fail-on-stale` when an attachment would deploy with a source-workspace GUID.

## [1.6.0] — 2026-06-29
Internal layered-architecture refactor (`identity`/`transport`/`client`/`profile`/`scope`/`params`) — no public API or CLI change.

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
