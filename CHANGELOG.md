# Changelog

All notable changes to fabric-deploy (`fabdep`) are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Releases before the first public one are summarized in a few lines each.

## [Unreleased]

### Breaking

### Added

- MIT license; public release preparation.

### Changed

### Fixed

### Removed

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
