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

This feature supplies the renewable provider but does not migrate existing Issue, PR, CI,
merge, Git, or background-operation callers. Native GitHub review approvals continue to use
the separately configured reviewer App; Actions secrets and human-operator credentials remain
separate roles.
