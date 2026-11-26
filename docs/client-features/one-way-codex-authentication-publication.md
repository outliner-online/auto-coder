# Retired Codex authentication publication

Auto-Coder no longer publishes local Codex login state to repository Actions
secrets during `process-issues` startup. The `[github_secrets]` configuration
table, dedicated publisher, and `--skip-codex-auth-secret-sync` option have
been removed. Existing configuration tables no longer trigger publication.

Before upgrading or restarting an externally configured launcher with this
version, remove `--skip-codex-auth-secret-sync` from its command line. The
option is no longer accepted. Remove the unused `[github_secrets]` table and
its dedicated publication credential from configuration when updating the
deployment. Updating source does not alter a running process or its launch
configuration.

Selective prompt evaluations use an independently configured OpenAI API key;
see [Selective Promptfoo prompt-regression CI](selective-promptfoo-prompt-regression-ci.md).
They no longer consume the `CODEX_AUTH_JSON` Actions secret. Operators may
delete that secret after confirming no other repository workflows use it.
The retirement does not delete or modify a developer's Codex login file;
ordinary Codex clients retain ownership of their local authentication.

This retirement is observability-neutral for the dashboard: startup publication
had no dashboard trace producer or view contract. The shared GitHub request
observation boundary remains in place for its active consumers.
