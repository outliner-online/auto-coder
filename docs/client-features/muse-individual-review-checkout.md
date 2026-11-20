# Muse individual Issue review checkout

Operators using Muse for individual Issue specification review must supply an
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
Muse dispatch. Git directory/index environment overrides also fail closed.
No checkout is inferred from the controller directory, neighboring repositories,
or the independent auto-coder update-source workspace.

Ordinary Review-lane intake already carries its exact repository and Issue
identity into the scheduled specification lifecycle. At Muse entry, that identity
selects the configured checkout and binds the invocation's command directory
before the existing private-workspace preparation and real Muse no-edit Git
snapshot. The binding is restored on return and exception without changing the
process directory. Private copies and retained sessions keep their existing
ownership and cleanup rules. PR review, decomposition review, editable execution,
and other providers retain their existing directory behavior.

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
