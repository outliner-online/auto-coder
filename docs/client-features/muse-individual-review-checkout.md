# Muse Issue review checkout

Operators using Muse for individual Issue specification or parent/child
decomposition review must supply an
absolute target checkout path in the existing repository-scoped application
configuration. For example, in `~/.auto-coder/OWNER/REPO/config.toml`:

```toml
[issue_review]
checkout = "/absolute/path/to/the/target-checkout"
```

The equivalent inline override in the base `config.toml` is supported:

```toml
[repository."OWNER/REPO".issue_review]
checkout = "/absolute/path/to/the/target-checkout"
```

The path must resolve to a non-bare Git worktree root with a readable committed
HEAD and exactly one origin URL identifying that exact GitHub repository. HTTPS,
GitHub SSH, and `ssh://git@github.com/` origin formats are accepted. Missing,
relative, invalid, ambiguous, and cross-repository selections fail locally before
Muse dispatch. Origin evidence comes only from the checkout's local Git config
and, when enabled, its worktree-specific config, without following includes.
Inherited system/global defaults cannot supply missing checkout metadata.
Additional inherited or included origin values are rejected, even when direct
checkout metadata matches, so they cannot redirect private-workspace cloning.
Git directory/index overrides, alternate config-file selection, and injected
command-configuration environment variables fail closed before Muse dispatch.
No checkout is inferred from the controller directory, neighboring repositories,
or the independent auto-coder update-source workspace.

Ordinary Review-lane intake already carries its exact repository and Issue
identity into the scheduled specification lifecycle. At Muse entry, that identity
selects the configured checkout and binds the invocation's command directory
before the existing private-workspace preparation and real Muse no-edit Git
snapshot. The binding is restored on return and exception without changing the
process directory. Private copies and retained sessions keep their existing
ownership and cleanup rules. Decomposition review uses the same binding with the
scheduled parent identity and complete direct-child set. PR review, editable
execution, and other providers retain their existing directory behavior.

Setup errors produce an explicit retryable `ERROR`, never a saved `READY` or
`BLOCKED` authorization or an implementation handoff. Correcting configuration
allows the same Review arrival to retry. This source change does not configure
an active worker or provision a checkout; operators must supply the value before
using this feature.

`tests/test_issue_review_checkout.py` crosses ordinary engine intake, the real
Review scheduler, default specification analysis, private-workspace preparation,
and Muse Git preflight with deterministic local MSP hosts. Its launch oracle
reads the actual Git worktree/HEAD, verifies overlapping repository and Issue
identities, and checks invalid inputs, retry, exception cleanup and thread reuse.
An explicit old-behavior control removes only the new binding and reproduces
the non-Git controller's Muse HEAD failure before host launch.

`tests/test_decomposition_review_checkout.py` crosses the engine's authoritative
family scheduling, real validation scheduler, default decomposition analyzer,
private-workspace preparation and real Muse no-edit Git preflight. It stops at
`Popen` before any MSP host or external provider starts. An independent Git oracle
verifies the selected source, private worktree, HEAD and parent identity from
non-Git, unrelated-Git and matching controller directories. Invalid selections,
retry, concurrent repositories and an old-binding control are covered. The
deliberate stop returns `ERROR` and grants no terminal review authorization.
