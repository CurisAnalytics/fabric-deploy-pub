# fabric-deploy (`fabdep`)

Deploy Microsoft Fabric workspaces from git without hand-writing `parameter.yml`.

fabdep wraps Microsoft's [fabric-cicd](https://microsoft.github.io/fabric-cicd/). It finds
the workspace, lakehouse, SQL endpoint, warehouse, SQL database and User Data Function IDs
your items reference, rewrites them for the target workspace (picked from the git branch),
and blocks deploys that would overwrite live row-level security or ship report buttons
still wired to another workspace. `fabdep plan` shows what a deploy would do first.

> [!IMPORTANT]
> **Built for internal use, shared as-is.** fabdep was written for one team's Fabric
> workflow and is published in case it's useful to others. It is not supported, and it
> encodes that team's conventions. If you want to use it, have a coding agent review it and
> adapt it to your setup before pointing it at a workspace you care about.
> Not affiliated with or endorsed by Microsoft.

## Install

Requires Python 3.11+, `git`, and a way to sign in (the Azure CLI by default).

```sh
git clone <this repository> && cd fabric-deploy
uv tool install .        # or: pipx install .
```

## Use

```sh
cd path/to/workspace     # the folder holding the Fabric Git item folders
fabdep init              # writes a starter fabdep.yml
fabdep detect            # offline: the IDs found and what they'd become
fabdep plan              # read-only: what a deploy would create, update and unpublish
fabdep                   # deploy the checked-out branch
```

By default branch `dev` deploys to the workspace `<workspace_base_name> [DEV]`; an
`environments:` map in `fabdep.yml` overrides that. `fabdep schema` prints the config's
JSON Schema, and `fabdep <command> -h` documents every option.

> [!WARNING]
> **A full deploy deletes items.** After publishing, fabdep unpublishes the workspace items
> the repo doesn't contain (orphan cleanup). `-n`, `-i` and `--pr` turn that off. Run
> `fabdep plan` first: it lists everything a deploy would unpublish.

## More

- [ARCHITECTURE.md](ARCHITECTURE.md): how detection, the guards and the deploy flow work,
  and the [known limitations](ARCHITECTURE.md#known-limitations).
- [examples/](examples/): GitHub Actions and Azure Pipelines workflows.
- [AGENTS.md](AGENTS.md): setup, tests and conventions, for agents and people.
- [CHANGELOG.md](CHANGELOG.md)

MIT licensed; see [LICENSE](LICENSE).
