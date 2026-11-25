# Deferred PR validation results

An ordinary PR reviewer can finish while GitHub temporarily prevents the
controller from confirming the current review target. Auto-Coder retains that
unaccepted result and its deferred reviewer-session checkpoint before waiting.
Once current evidence is available, processing resumes the original attempt's
acceptance work without invoking the reviewer again.

## Retention and authority

The pending result is stored by `DeferredPRValidationStore`, separately from the
best-effort, non-authorizing review audit. It retains the normalized result,
checkpoint, producing review and attempt identities, evaluated input bindings,
and retry reason and deadline. Reading an audit report is never a replacement
for this pending-state record.

Retention grants no approval, thread closure, corrective delivery or merge
permission. The original attempt registry and the existing acceptance,
publication and final merge boundaries keep their authority. The checkpoint is
restored as a proposal; accepted-finding projections and process-local registry
objects are reacquired from their current owners.

## Waiting and resumption

A post-review GitHub observation deferral preserves the result and exposes the
original reason, including `rate_limit_cooldown`, and the retry-not-before time.
Processing before that time waits without another reviewer invocation. The
pending state survives controller restart.

When retry is due, the controller verifies the registered producing attempt and
freshly observes the PR head and base, specification inputs, PR prose, claimed
review discussion, review policy and applicable review-cycle state. A matching
current context resumes the same attempt ID and sequence through the normal
acceptance and effect boundaries. It does not allocate another review attempt
or treat the saved verdict as already accepted.

An unavailable current observation leaves the result pending. This includes
parent Issue descriptions supplied as background to the original review: an
unavailable parent lookup is not treated as an absent or changed parent. Parent
context does not expand the child Issue's requirements, and reading legacy
`Parent-Issue` metadata does not modify the GitHub hierarchy. Complete prose
comparison is required when consuming a deferred result; the existing child
Requirement and diff snapshot checks keep their established availability rules.
A confirmed change to the evaluated target or input, or a newer applicable
attempt, supersedes the pending proposal. Normal current-context admission then
decides whether a new review can run. Corrupt or unreadable pending state cannot
be interpreted as permission to publish or merge. Successful resumption retires
only the consumed attempt's pending record, preserving independently newer work.

If the controller stops after GitHub accepts the native review but before local
acknowledgment, resumption reconciles that exact publication before sending
again. The authenticated publisher, PR head, attempt marker, verdict and native
review state must match, and any pending review-root associations must be
recovered. A confirmed publication is acknowledged without another POST;
unavailable or conflicting publication evidence continues to wait. An absent
publication still goes through the existing fresh-input and publication guards.
The diagnostic audit never supplies publication or merge authority.

## Diagnostics

The existing `pr.adversarial-validation` stage distinguishes:

- `DEFERRED`, `effect=validation-result-retained`: the result is retained while
  its adoption waits. Facts include `phase=gap-state-acceptance`, `reason`,
  `retry_not_before`, `attempt_id`, `attempt_sequence` and `examined_head`.
- `DEFERRED`, `effect=validation-result-resumed`: the original result is being
  consumed without another reviewer invocation. Subsequent acceptance,
  publication, repair or merge events describe their actual outcomes.
- `SUPERSEDED`, `effect=validation-result-superseded`: a confirmed input or
  attempt change prevents reuse of the pending proposal.

These events are visible in the existing Decision Log. The native reviewer
report remains historical evidence; a resumed event is not review approval or
PR completion. See [dashboard review history](dashboard-llm-review-history.md).

## Verification

Run `bash scripts/test.sh tests/test_deferred_pr_validation.py
 tests/test_deferred_pr_validation_store.py tests/test_issue_context.py
 tests/test_issue_context_strict.py
 tests/test_adversarial_validator.py::TestBuildAdversarialValidationContext` for
the controller flow, durable store and complete current-context boundaries.
The joined regression observes a real normalized reviewer result, refuses its
first HEAD confirmation, reconstructs state, and verifies same-attempt
consumption with one model call and no effects during the wait.
Changed-input and unavailable-input cases check rejection and continued waiting;
the Decision Log regression checks the facts emitted by production processing.
Strict parent-context regressions distinguish unavailable retrieval from
confirmed absence, preserve typed retry reasons and deadlines, and verify that
metadata fallback only reads the existing relationship.
