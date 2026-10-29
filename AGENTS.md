# AGENTS.md

How to work on fabdep (`src/fabric_deploy/`, CLI `fabdep`), a wrapper around Microsoft's
fabric-cicd that deploys Fabric workspaces from git. Written for AI coding agents; it serves
people just as well. Read [ARCHITECTURE.md](ARCHITECTURE.md) first: the modules, the deploy
flow, detection, the invariants and the known limitations.

## Setup and checks

fabdep uses [uv](https://docs.astral.sh/uv/) and Python 3.11 or later (CI: 3.11–3.13 on Linux).

```sh
uv sync                                # .venv with fabdep and the dev tools
uv run pytest                          # unit tests, offline
uv run ruff check src tests            # lint
uv run ruff format --check src tests   # format
uv run basedpyright                    # type check: keep it at 0 errors
```

Run all four before every commit; CI runs the same.

## Invariants

- A deploy never modifies the user's repo; only `init`, `detect --save`, `rls pull --write`
  and `schema -o` write files there.
- Read-only commands (`detect`, `auth status`, `rls pull` without `--write`, `plan`) write
  nothing to Fabric or the repo.
- Fail closed on ambiguity: never pick the first of several candidates; leave the id unchanged
  with a warning, or raise.
- Only the repo's own workspaces' ids are rewritten; unrelated workspaces deploy unchanged.
- Library modules log and raise; user-facing text, printing and exit codes belong in `cli`.

## Tests

- One file per module in `tests/` (`test_detect.py` for `detect`, ...). Bug fixes start with
  a failing test. Warnings are errors.
- **Offline.** An autouse fixture in `tests/conftest.py` fails any non-loopback connection,
  refuses real sign-ins, clears `FABRIC_BEARER_TOKEN`, `POWERBI_BEARER_TOKEN` and
  `FABDEP_AUTH`, and points the Azure CLI at an empty config directory. Stub Fabric calls with
  `monkeypatch` (`mock_fabric_api` covers building a `WorkspaceDeployer`); use the `credential`
  fixture when code needs tokens.
- Build items with `make_item` from `tests/conftest.py`; `sample_ws` gives each test a copy of
  `tests/fixtures/sample-workspace`.
- **Placeholders only**: no real tenant, workspace, capacity or item ids, tokens, hostnames or
  company names. Use pattern GUIDs (`00000000-0000-0000-0000-0000000000xx`) and the names
  Contoso, Fabrikam, Northwind or Woodgrove.
- **Never run the tests under `git rebase --exec` or from a git hook**: git exports `GIT_DIR`
  there, and the tests that build scratch repositories would commit into yours.
- **Live integration tests** (marked `integration`) deploy to real workspaces and are skipped
  unless `FABDEP_INTEGRATION_REPO` names a workspace repo of your own whose workspaces for the
  branch named `MAIN` (the tests run with `-b MAIN`) and for the checked-out branch are
  throwaway ones: the deploys unpublish every item those workspaces have that the repo
  doesn't. Sign in first, then `FABDEP_INTEGRATION_REPO=/path/to/repo uv run pytest -m integration`.

## Changes

- Keep a change to one concern and match the style around it; comments say why, not what
  changed when.
- A user-visible change gets a line in `CHANGELOG.md` under `Unreleased` (Breaking, Added,
  Changed, Fixed or Removed), written from the user's side.
- Changing what `fabdep.yml` accepts means updating `src/fabric_deploy/fabdep.schema.json` and
  the `fabdep init` starter in `scaffold.py` too.
- fabdep relies on some fabric-cicd internals: raise the version cap in `pyproject.toml` only
  after the suite passes against the new release.
- [Conventional Commits](https://www.conventionalcommits.org/) (`fix(detect): ...`, `feat!:`
  for a breaking change, explained in the body), one logical change per commit with its tests
  and changelog line.
