# AGENTS.md

Guidance for AI coding agents working on fabdep (`src/fabric_deploy/`, CLI `fabdep`), a wrapper
around Microsoft's fabric-cicd that deploys Fabric workspaces from git.

## Read first

- [ARCHITECTURE.md](ARCHITECTURE.md): modules, the deploy flow, detection, invariants.
- [CONTRIBUTING.md](CONTRIBUTING.md): setup, tests, commit and changelog conventions.

## Invariants

- A deploy never modifies the user's repo; only `init`, `detect --save`, `rls pull --write`
  and `schema -o` write files there.
- Read-only commands (`detect`, `auth status`, `rls pull` without `--write`, `plan`) write
  nothing to Fabric or the repo.
- Fail closed on ambiguity: never pick the first of several candidates; leave the id unchanged
  with a warning, or raise.
- Only the repo's own workspaces' ids are rewritten; unrelated workspaces deploy unchanged.
- Tests never touch the network or a real sign-in.

## Before committing

```sh
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
uv run basedpyright            # 0 errors
```

Never run tests under `git rebase --exec` or a git hook. Never call Azure or Fabric for real.

## Fixtures and examples

No real tenant, workspace, capacity or item ids, tokens, hostnames or company names anywhere:
use pattern GUIDs (`00000000-0000-0000-0000-0000000000xx`) and the names Contoso, Fabrikam,
Northwind or Woodgrove. Build test items with `make_item` from `tests/conftest.py`.

Conventional commits; user-visible changes get a line under `Unreleased` in CHANGELOG.md.
