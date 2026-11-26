# Controller GitHub App authentication

Controller authentication is selected per repository with `select_controller_auth`. When no
`github-app-auto-coder-controller` table is present, an explicitly supplied token retains
precedence over the existing `GITHUB_TOKEN`, `gh auth token`, and GitHub CLI configuration
discovery. A present table always selects App mode; invalid configuration or GitHub evidence
fails closed and never falls back to another credential role.

The global `~/.auto-coder/config.toml` is deep-merged with
`~/.auto-coder/<owner>/<repo>/config.toml`:

```toml
[github-app-auto-coder-controller]
app_id = 12345
private_key_path = "~/.auto-coder/auto-coder-controller.pem"
```

`app_id` must be a positive integer. The optional key path must be absolute or start with
`~/`; its default is shown above. Installing the App, storing its PEM, granting it access to
the repository, and approving the required repository permissions are deployment
prerequisites and are not performed by Auto-Coder.

Consumers retain the selected provider and call `credential_for(repository, permissions)`
immediately before each outbound request. App credentials are renewed when no more than 60
seconds remain. `invalidate` affects only the exact returned snapshot, so it cannot discard a
newer concurrent credential and never replays a consumer operation. Authentication failures
contain only a nonsecret stage and category.

The provider verifies App identity, installation, permissions, expiry, and an exact singleton
repository grant before publishing a credential. Its stable authority key contains no token.
App identity is obtained with the App JWT; personal identity uses authenticated `GET /user`.
Credentials and identity evidence are memory-only and are reconstructed after restart.

`create_controller_client(repository)` composes that provider with the shared management
adapter. Injected Issue and PR management callers use `GitHubClient.api_for`, which resolves a
credential at every wire send (including later pages on a retained GhApi object) rather than
copying an installation token into the process environment. The legacy singleton remains the
startup default until the remaining CI and terminal merge senders adopt repository-scoped
clients; Git, native reviewer-App publication, provider APIs and human-operator actions remain separate credential roles.

Controller-App PRs keep the local implementation marker and ordinary review/repair route. A
`[bot]` suffix does not turn such a PR into a dependency update. Deployments with restrictive
Issue/PR author allowlists must add the App's bot **account ID** (the identity observed on the
created PR), not the GitHub App ID. Recording-transport fixtures demonstrate routing and token
renewal only; they do not prove that a live installation has deployment permissions.

This adapter change is observability-neutral: it retains the shared request outcome boundary
and existing trace event schema, while changing only authorization selection immediately before
the request is sent. Dashboard event names, outcomes, and fields are therefore unchanged.
