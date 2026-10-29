# fabric-deploy (`fabdep`)

Deploy Microsoft Fabric Workspaces from git without hand-writing `parameter.yml`.

fabdep wraps Microsoft's [fabric-cicd](https://microsoft.github.io/fabric-cicd/). It finds
the Workspace, Lakehouse, SQL endpoint, Warehouse, SQL Database and User Data Function IDs
your items reference, and rewrites them for the target Workspace (picked from the git branch).

This is intended to support Workspace repos making use of multiple branches. The Workspaces
themselves need not be synced to git in the Fabric UI. `fabdep` blocks deploys that would
overwrite live row-level security or ship report buttons still wired to another Workspace.
`fabdep plan` shows what a deploy would do first.

> [!IMPORTANT]
> **Built for internal use, shared as-is.** fabdep was written for one team's Fabric
> workflow and is published in case it's useful to others. This repo will not be meaningfully
> supported by the author. If you want to use it, have a coding agent review it and
> adapt it to your setup before pointing it at a Workspace you care about.
>
> **1.11.1 is the last version the author used in production**, so it reflects the particular
> Fabric Workspace setup and workflow fabdep was designed around. The many commits after it
> are pre-emptive cleanup to make the tool viable in other Fabric setups; they are covered by
> the offline test suite but have not been run against a live tenant.
>
> Not affiliated with or endorsed by Microsoft.

## Install

Requires Python 3.11+ and a way to sign in (the Azure CLI by default).

```sh
cd fabric-deploy
uv tool install .        # or: pipx install .
```

Try `uv run fabdep --help`. This prints the available commands and the options of the
default command `deploy`. `fabdep <command> -h` covers the others.

## Use

```sh
cd path/to/Workspace     # the folder holding the Fabric Git item folders
fabdep init              # writes a starter fabdep.yml
fabdep detect            # offline: the IDs found and what they'd become
fabdep plan              # read-only: what a deploy would create, update and unpublish
fabdep                   # deploy the checked-out branch
```

By default branch `dev` deploys to the Workspace `<Workspace_base_name> [DEV]`; an
`environments:` map in `fabdep.yml` overrides that.

> [!WARNING]
> **A full deploy deletes items.** After publishing, fabdep unpublishes the Workspace items
> the repo doesn't contain (orphan cleanup). `-n`, `-i` and `--pr` turn that off. Run
> `fabdep plan` first: it lists everything a deploy would unpublish.

## More

- [ARCHITECTURE.md](ARCHITECTURE.md): how detection, the guards and the deploy flow work,
  and the [known limitations](ARCHITECTURE.md#known-limitations).
- [examples/](examples/): GitHub Actions and Azure Pipelines workflows.
- [AGENTS.md](AGENTS.md): setup, tests and conventions, for agents and people.
- [CHANGELOG.md](CHANGELOG.md)

MIT licensed; see [LICENSE](LICENSE).
