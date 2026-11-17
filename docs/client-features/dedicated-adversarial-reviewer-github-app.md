# Dedicated adversarial reviewer GitHub App

Adversarial validation verdicts are published as native pull-request reviews using
the separately configured `auto-coder-reviewer` GitHub App. Configure its `app_id`
and optional `client_id` under `[github-app-auto-coder-reviewer]` in
`~/.auto-coder/config.toml`. The default private key remains
`~/.auto-coder/auto-coder-reviewer.pem`. Optional `private_key_path` selects
a separate external PEM using an absolute path or `~/` relative to the
worker's HOME. Empty, non-string, and other relative paths are rejected;
an unreadable selected key fails without falling back to the default key.

The same table in `~/.auto-coder/<owner>/<repo>/config.toml` overrides
these settings for only that repository. For example:

```toml
[github-app-auto-coder-reviewer]
app_id = "222222"
private_key_path = "~/.auto-coder/outliner-reviewer.pem"
```

This selects an existing external key; configuration loading never reads,
creates, copies, or replaces PEM contents. It does not install or transfer an
App. Keep a personal App's key and configuration intact when selecting a
different organization App for a repository. `PASS` maps to `APPROVE`, `NEEDS_FIX`
and `NEEDS_TESTS` map to `REQUEST_CHANGES`, and other outcomes map
deterministically to `COMMENT`.
Publication is pinned to the validated head SHA and fails closed without falling
back to the normal user credential.

Two-tier strong audits use the same App and diff-anchor helper to attach one
native root review thread per accepted finding in the review submission. Their
summary remains a distinct COMMENT review, not an ordinary validation verdict.
Exact publication reconciliation verifies the complete expected thread set as
well as the author, reviewed head, and summary body before confirming delivery.

For a `NEEDS_FIX` verdict, every actionable finding is submitted atomically as
its own independently resolvable review thread. Findings name a changed-file
anchor and include their complete requirement, counterexample, evidence, test
gap, and regression scenario. Valid diff line or range anchors are preserved;
missing or invalid line coordinates safely become file-level comments rather
than fabricated line locations. An invalid changed-file anchor or any review API
failure leaves publication unsuccessful. GitHub's existing review-thread
resolution state remains the only acknowledgement state used by merge gating.

Malformed canonical correction bindings are rejected before publication as
`ERROR` diagnostics. Their model-reported findings and closure claims are not
authorized effects: the native `COMMENT` review publishes the invalid-reference
reason without inline findings, independent closure, or repair dispatch. The raw
answer is retained for diagnosis and existing unresolved threads keep blocking
merge until a new valid independent attempt completes.

This native review is the sole and authoritative publication path: Auto-Coder
does not additionally post the validation result as a regular user-authenticated
PR comment. The review body carries the same versioned machine-readable marker
(validator format/version plus the exact validated head SHA) used elsewhere, so
persisted validation state for a PR head can be recovered from the review after
a processing-loop restart. A review only counts as authoritative when it was
authored by the reviewer App's own resolved bot identity (fetched via `GET /app`
using the App's own credentials, never a configurable string) and carries the
marker for the exact head SHA in question; a lookalike review or comment from
anyone else is ignored. Reading this persisted state (`GitHubClient.get_pr_reviews_strict`
plus identity resolution) fails closed on any API or configuration error rather
than treating a lookup failure as "no prior result". Legacy validation comments
created before this change remain readable as a same-SHA fallback when no native
review exists yet for that head, but the comment-publication path itself is
never used for new results.


## Issue Review Boundary and Token Capabilities

The reviewer App also provides a dedicated publication boundary for Issue review comments (`publish_issue_review`).
To preserve the principle of least privilege, the client explicitly requests separate scoped installation tokens:
- Pull Request verdicts request tokens restricted to `pull_requests: write`.
- Issue findings request tokens restricted to `issues: write`.

This strict capability separation ensures that operations crossing contexts do not inadvertently leak permission scopes.
Like PR publication, Issue comment publication operates securely with zero fallbacks. If the App is unconfigured, or an identity/token lookup fails, the publisher does not fall back to the ordinary user credential or global/environment variables (e.g., `$GITHUB_TOKEN`).

### Issue specification/decomposition validation findings routing

`SpecificationValidationLifecycle.apply_blocked`/`apply_inherited_blocked` and
`DecompositionValidationLifecycle.apply_blocked` publish their BLOCKED
findings comments exclusively through `GitHubClient.publish_issue_review_comment`
(a thin wrapper over `publish_issue_review`), never through the ordinary
`add_comment_to_issue` credential path. A pre-existing comment only counts as
already-published when its body is an exact match for the decision's marker
**and** its author is the reviewer App's own resolved identity
(`GitHubClient.reviewer_app_identity`) -- a body-substring match alone is
never sufficient, and a marker-bearing comment from another actor is reported
as an unconfirmed conflict rather than silently accepted or reposted.

Label withdrawal (`implementation-ready` removal) always stays on the
ordinary controller credential: only the findings comment itself is
App-authored.

Each `ValidationDecision`/`DecompositionDecision` carries a
`publication_schema_version` and an optional `publication_receipt`
(confirmed comment id, publisher login, publisher App id). A decision
persisted before this routing existed keeps `publication_schema_version == 0`
forever and is never reinterpreted as App-authored, reposted, or edited. A
decision produced after this change is stamped `publication_schema_version =
1` before its first send attempt; `findings_published` is only trusted
without re-verification once a receipt is durably attached, so a crash
between an accepted POST and receipt persistence cannot masquerade as
complete -- it is re-checked against the live comment list on the next pass.

Both the specification decomposition-child (inherited) and decomposition
(parent-set) publication routes participate in the durable pending-work
store (`github_pending_work.py`), matching the standalone specification
route: a `GitHubRequestError` while posting the comment or removing the
label is deferred as a named effect (`diagnostic`/`readiness-withdrawal`)
and resumed by a registered stage handler
(`VALIDATION_PUBLICATION_STAGE` for specification,
`DECOMPOSITION_PUBLICATION_STAGE` for decomposition) rather than only being
recorded in a returned failure string.

A confirmed reviewer-App findings comment delivered back as its own
`issue_comment` webhook is recognized (marker + resolved reviewer identity)
and does not enqueue a redundant re-evaluation; this check is scoped to that
exact combination and is not a general bot/comment filter, so an unrelated
comment, human reply, or genuine Issue edit is unaffected.

Pending accepted publications are reconciled before starting another ordinary
adversarial validation. Review roots moved by later commits are verified using
their original coordinates and original reviewed commit. Unconfirmed recovery
remains merge-blocking and is reported as a processing failure with its reason;
it does not consume another reviewer invocation.

### Safe PR publication failure diagnostics

A failed ordinary PR verdict logs a structured `reviewer_publication` diagnostic
with the last publication stage, exception/cause type names, HTTP status when
available, and an allowlisted endpoint class. Status 403 is recorded as
`forbidden`, without assuming whether permissions or a rate limit caused it.
Exception messages, response bodies, credentials, headers, raw URLs and query
strings are excluded. The existing unsuccessful result and review safety gates
remain unchanged; diagnostics do not retry a request, change credentials, fall
back to the user identity, or establish provider acceptance. The dashboard trace
schema is unchanged: this adds safe details to an existing publication failure
log rather than a new processing origin, admission decision, or effect.

Regression: `bash scripts/test.sh tests/test_github_app_reviewer.py`.

### Changing publisher identity

Selecting a new App does not re-author or transfer old reviews or receipts.
Existing author, exact-head, payload and root-association checks still apply.
A retained unconfirmed PR publication is reconciled before another verdict
can be sent. If its old-App review is absent, inaccessible, or does not match
the newly authenticated identity, reconciliation remains unsuccessful and
the original intent stays retained; the new App must not resend that review.
Resolve the retained publication through its original identity before switching,
or hold the transition while that evidence is unknown. This option does not add
cross-App recovery or reinterpret historical authors as the new App.

This configuration change does not add a dashboard event, processing origin,
admission outcome or durable-work schema; existing publication diagnostics and
pending reconciliation remain the observable results.

### Ordinary retained publisher continuity

New ordinary decisions carry publication identity version 1. Before the first
send, the controller atomically binds the resolved App ID to the exact producing
repository, PR, head/base and ordinary attempt. Same-producer retention and stale
bookkeeping writes preserve this binding. A selected transport must resolve the
same publisher before it can send. A matching App can resume ordinary recovery;
a changed, unavailable, malformed or legacy-unknown publisher leaves the saved
decision PENDING without a new review, repair handoff, reviewer invocation or
merge. Missing canonical publication intents do not waive this check. A changed head
also cannot discard an unknown or attempted pending publication; only a new
version-1 decision that has not reached its pre-send binding is known unsent.

Existing records without this binding remain version 0; a configuration change
cannot retroactively assign them to a new App. They require independently
established original-publication evidence and a supported migration decision;
this change neither discards them nor provides a migration command.

Regression: `bash scripts/test.sh tests/test_ordinary_publisher_identity.py
tests/test_ordinary_correction_retention.py tests/test_effective_decision_pr_flow.py
tests/test_github_app_reviewer.py`.

Discussion changes also cannot discard an uncertain ordinary publication. The
pre-invalidation check holds it before ledger changes, and the locked store
refuses a stale clear after another writer binds the publisher. Confirmed and
provably-unsent ordinary decisions remain eligible for discussion revalidation.
