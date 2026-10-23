# CI examples

Pipelines that run fabdep from a Fabric workspace repository. A pull request into `main` or
`dev` gets a plan of deploying the items it changes to the workspace of the branch it merges
into (`fabdep plan --pr <merge commit>`); once it is merged, the push to `main` or `dev`
deploys those items (`fabdep deploy --pr`). Preview and deploy cover the same items, and
neither unpublishes anything.

| File | For |
| --- | --- |
| [`github-actions/deploy.yml`](github-actions/deploy.yml) | GitHub Actions, signing in with OpenID Connect (no secret) |
| [`azure-devops/azure-pipelines.yml`](azure-devops/azure-pipelines.yml) | Azure Pipelines, signing in through a service connection |

Both sign in with the Azure CLI (`azure/login`, or the `AzureCLI@3` task) and run
fabdep with `--auth cli`, so fabdep uses that sign-in. The install line points at
`github.com/OWNER/fabric-deploy`, a placeholder for the repository's location; pin a
release tag with `@<tag>`, from the first public release on (older tags reference a test
submodule that isn't public).

## Prerequisites

1. **A service principal for the pipeline** (a Microsoft Entra app registration).
   - GitHub Actions: add federated credentials to the app registration for the
     repository, with subject `repo:<owner>/<repo>:pull_request` for the plan job and
     `repo:<owner>/<repo>:ref:refs/heads/main` (and `.../dev`) for the deploy job. Save
     the app's client ID and the tenant ID as the repository variables
     `AZURE_CLIENT_ID` and `AZURE_TENANT_ID`.
   - Azure DevOps: an Azure Resource Manager service connection for the service
     principal (workload identity federation needs no secret), named in the pipeline's
     `serviceConnection` variable. The connection asks for a subscription (or management
     group) scope, though fabdep calls no Azure Resource Manager API. With
     `allowNoSubscriptions: true`, `AzureCLI@3` signs in without selecting that
     subscription, so the service principal needs no Azure role. `AzureCLI@2` (the only
     version on Azure DevOps Server) has no such input and selects the connection's
     subscription after signing in, so with it the service principal needs a role on that
     subscription (Reader, for example).
2. **The Fabric tenant setting** *Service principals can call Fabric public APIs*
   (admin portal, Tenant settings, Developer settings), enabled for the service
   principal or a security group it belongs to. To let fabdep create workspaces
   (`-N`, or `create: true` in fabdep.yml), also *Service principals can create
   workspaces, connections, and deployment pipelines*.
3. **The Contributor role (or higher) on each workspace** the pipeline deploys to,
   and access to the connections the repository's items use (`fabdep detect` lists
   them). The service principal needn't see the workspace the content was exported from
   (dev, for a deploy to `main`): fabdep then recognizes its IDs by the repo's lakehouse
   and function names. Any role on it lets fabdep rewrite its IDs wherever they occur.
4. **A `tenant:` pin in fabdep.yml**: the tenant ID (`az account show --query tenantId
   --output tsv`). fabdep then refuses a token from any other tenant, so a pipeline
   signed in to the wrong tenant fails before it calls an API. Add
   `require_spn: true` to refuse user sign-ins too.

## Without the Azure CLI

fabdep can sign in as the service principal itself: `--auth env` reads `AZURE_TENANT_ID`,
`AZURE_CLIENT_ID` and `AZURE_CLIENT_SECRET`, or `AZURE_CLIENT_CERTIFICATE_PATH` (a PEM or
PKCS12 file with the private key) instead of the secret. The app registration then needs
that secret or certificate rather than federated credentials. In GitHub Actions drop the
`azure/login` step; in Azure Pipelines run fabdep in a plain `script` step instead of
`AzureCLI@3`. Pass the values from your secret store:

```yaml
# GitHub Actions
- run: fabdep deploy "$FABDEP_DIR" --branch "$GITHUB_REF_NAME" --auth env --pr
  env:
    AZURE_TENANT_ID: ${{ vars.AZURE_TENANT_ID }}
    AZURE_CLIENT_ID: ${{ vars.AZURE_CLIENT_ID }}
    AZURE_CLIENT_SECRET: ${{ secrets.AZURE_CLIENT_SECRET }}
```

```yaml
# Azure Pipelines: secret variables reach a script only through env:
- script: >-
    fabdep deploy "$(fabdepDir)" --auth env --pr
    --branch "${BUILD_SOURCEBRANCH#refs/heads/}"
  env:
    AZURE_TENANT_ID: $(tenantId)
    AZURE_CLIENT_ID: $(clientId)
    AZURE_CLIENT_SECRET: $(clientSecret)
```

A client secret is a long-lived credential: prefer a certificate, or the federated sign-in
of the examples above, which stores no secret at all.

## Notes

- `--pr` with no commit (the push jobs) deploys what the newest merged pull request
  changed, found by its commit message on the branch's first-parent history: use merge
  commits or squash merges, and protect `main` and `dev` so changes arrive through pull
  requests. After a rebase merge or a direct push, HEAD is past the newest such commit, and
  `--pr` fails rather than deploy an older pull request. The plan jobs name the merge commit
  the pull-request build checks out instead. Both need the history, hence
  `fetch-depth: 0` / `fetchDepth: 0`.
- `--pr` never unpublishes, and a pull request that changes no item (only
  documentation, say) plans and deploys nothing. A full `fabdep deploy`, previewed with
  `fabdep plan`, removes the items the repository no longer has.
- Add `--output json` to `fabdep plan` for a plan other tools can read.
