# Provider-aware unresolved review repair

Actionable unresolved review threads keep merge blocked and are routed through the
provider-aware durable `cloud.csv` association to the existing originating cloud
session. Jules and Codex Cloud therefore receive repair feedback through the same
`CloudTaskClientBase.send_followup` contract, targeting the existing PR and its
current head branch, fetched from a cache-bypassing authoritative GitHub lookup
immediately before transport. Per-finding local and PR-side receipts prevent
redelivery, while new findings remain eligible. A durable pending reservation is
written before transport; if an accepted request cannot be confirmed in either
receipt store, later runs suppress speculative duplicate delivery and report the
indeterminate state explicitly. Provenance-clarification threads remain excluded.
Missing or ambiguous ownership, unsupported providers, rejected requests, and
transport failures produce an explicit failed processing outcome rather than a
successful defer.

Saved adversarial reports can identify existing actionable roots by an exact
native thread ID with a `STILL_VALID` disposition, including strong-audit roots.
This preserves repair routing when the latest report rewords the original
finding rather than reproducing its paragraphs. Other explicit dispositions
do not authorize corrective delivery.

External review comments also receive controller-owned canonical blocker IDs at
repair preparation. Their original scope joins managed findings in the repair
bundle, including mixed reviews; native comment delivery receipts continue to
prevent duplicate follow-ups. See [External review feedback tracking](external-review-feedback-tracking.md).

When a reviewer uses the PR implementer's GitHub account, a corrective reply must
end with `<!-- auto-coder-review-guidance:v1 -->` on its own line. This explicitly
selects the reviewer role for that reply. Unmarked author replies (including
acknowledgments that report incomplete work) remain excluded; neither account
identity, prose heuristics, nor absence of an addressed claim establishes that
role. PR-author replies carrying an addressed marker remain implementation claims even if
they also contain the guidance marker. A trailing guidance marker cannot make
an adjudication envelope into ordinary feedback, regardless of its author. Adjudication envelopes still belong only
to the adjudication orchestrator. The guidance marker grants no adjudication,
closure, approval, or merge authority. This selection also applies to explicit
local review corrections after the latest addressed claim.

In cloud review-thread repair intake, comments in an unresolved thread that
precede a later non-root, non-adjudication reply from the PR implementer
carrying `<!-- auto-coder-review-addressed:v1 -->` are treated as already
claimed addressed for repair delegation. Only actionable comments posted after
that latest addressed reply remain eligible for delivery or native receipt
reconciliation; when no unaddressed feedback remains, the poll ends any due
no-commit checkpoint series as acknowledged and defers for independent reviewer
verification instead of failing on superseded indeterminate receipts or
resending already-addressed comments. Existing durable receipt and pending
records are preserved unchanged, mixed-feedback fencing remains active if new
guidance is posted later, and unresolved review threads continue to block merge.

New guidance uses its native comment identity with the existing receipt and
pending-reservation protocol. It does not renew an old root request, and editing
a delivered comment does not allocate a new delivery. Genuinely new guidance
uses a new explicitly marked reply; copying old guidance into a new reply creates
a different identity and can duplicate work semantically.

An existing unmarked reply can instead receive an explicit role annotation on
that same native comment. The reviewer/operator must first verify that the
original reply is reviewer guidance, still applies to the current head and
contract, and has not been superseded by later implementation or review. Append
the guidance marker to that original reply, preserving its substantive prose
and native ID, rather than copying its text into a new comment. A formerly excluded identity can then be delivered once through
the normal polling path. If that identity already has a confirmed receipt or an
uncertain pending reservation, the marker/edit does not authorize redelivery;
wording edits after delivery also retain the same identity. The production-path
regression covers unmarked, annotated and subsequently edited versions of one
native reply. This is an explicit operator declaration, not automatic inference
from historical prose. Auto-Coder does not annotate old comments, clear receipt
state, force reevaluation, resend requests or restart active work for recovery.
Ordinary independent adjudication and closure gates still apply.

A poll finding only prior delivery receipts reports a deferred repair while
the unresolved thread remains blocked. It does not claim a fresh accepted
handoff. Receipts prove prior delegation, not current implementation progress
or completed repair. The independently verified completed-Codex observation
described below can separately admit a first current-head review.

The existing `pr.repair-delegation` trace includes `head_sha` from the evaluated
PR snapshot and a fixed `reason` for receipt-only waits:
`unclassified_same_author_reply` means an excluded non-root same-author reply
has neither a recognized guidance declaration nor an addressed claim or
adjudication envelope, and its native identity has no confirmed or pending
delivery record. This is an observation of ambiguous discussion, not a claim
that the reply is actionable or caused a stall; ordinary acknowledgments can
produce it too. `actionable_feedback_already_delivered` means all selected
feedback has confirmed receipts and no such unrecorded reply was observed.
`unconfirmed_delivery_receipt` identifies the existing duplicate-suppression
failure when selected feedback has only pending receipts. The diagnostic does
not classify authors, annotate comments, resend work, or change wait outcomes.

Confirmed native identities include authenticated PR-side receipts for excluded
diagnostic candidates, even when a replacement worker has no local receipt file
and an earlier guidance reply has lost its marker. Those receipts are read-only
diagnostic evidence, separate from selected actionable identities: they do not
enter provider reconciliation or newly persisted receipt state. A receipt from
another account cannot establish confirmation. The production gate/public
projection regressions exercise trusted and untrusted receipt authors with an
absent local file, unchanged deferred outcomes, no file creation and no sends
or GitHub mutations.

These fixed reasons and the evaluated head pass through the existing public
diagnostic fact allowlist and dashboard detail renderer. No comment prose,
author login, task identity, receipt content or new API is added. The snapshot
head is not a new authoritative head lookup, and no reason establishes provider
liveness, completed repair, safe replay, or permission to merge. Private retained
review/publication evidence remains the separate scope of #2753.

## Independent reassessment after completed Codex feedback

A completed cloud correction may have changed the head while leaving an honest
partial or blocked implementation response. The original delivery identity stays
consumed, and a changed head still ends the old no-commit checkpoint series.
Neither condition is evidence that the original concern was fixed.

For ordinary Codex feedback, the controller can instead observe completion for
independent review admission. Every selected actionable identity must have a
confirmed delivery receipt, with complete current discussion and no uncertain
source. It rechecks the exact current PR and originating task ownership. The
provider must report `COMPLETED`, and its native chronological history must
show a completed assistant turn later than each selected request's retained
pre-send baseline. A terminal task unrelated to those sends, unavailable history, active
work, new feedback or unknown delivery cannot supply this observation.

The first current-head independent validator may then proceed without an
addressed marker or an older App finding. The controller does not turn the
implementer's unmarked explanation into corrective guidance, replay old
feedback, alter receipt state or restart the completed task. The existing
reviewer limit and saved-verdict deduplication still apply. A changed ordinary
blocking-thread snapshot at the post-Codex recheck invalidates the admission.
Ordinary external roots retain their closure and merge restrictions; provider
completion is not implementation verification.

The separate `pr.review-thread-gate` event is deferred with
`effect=completed-cloud-review-reassessment`, `examined_head` and
`blocking_thread_ids`. It exposes no task identity or comment content. The
preceding receipt-only repair event remains deferred, and the later validation,
correction, closure and merge events keep their own meanings. See
[Independent review-thread completion validation](independent-review-thread-completion-validation.md#first-current-head-review-after-completed-codex-feedback).

## Mixed native Codex delivery evidence

Intake fences uncertain native journal identities as well as shared producer
claims. Observed uncertainty is retained additively as a shared claim, protecting
against later native key loss. Legacy mixed comment batches stay held: new IDs,
role declarations and different canonical UUIDs do not establish independent
operations. No old uncertain request is released or replayed.

Explicit scoped corrections can use the authenticated v2
adjudication path documented in [Scoped adjudicated cloud corrections](scoped-adjudicated-cloud-corrections.md).
Its authority is the existing allowed adjudicator's explicit revision-bound
correction, not a controller-created distinct-defect decision. The former
unreachable `DISTINCT_DEFECT`-only predicate and artificially seeded positive
fixture have been removed. Ordinary unmixed comment delivery is unchanged.

A local native duplicate guard has a single-use exact-task/full-identity outcome,
separate from remote rejection. Only positive native rejection evidence can
support a remote-refusal label. The production ordinary-intake regression
`test_supported_external_feedback_producer_cannot_supply_mixed_split_resolution`
continues to verify that raw new comments do not acquire this authority.

### Ordinary legacy evidence boundary

The ordinary repair-bundle audit retains the rendered payload in SQLite; a
matching native prepared-message fingerprint can bind those exact bytes to a
known old send. Native repair metadata can also bind PR/head and current owner
incarnation. These are useful positive evidence, but neither the canonical
blocker UUID nor the accepted Finding's `delivery_status=NONE` is an unsent
receipt. Ordinary sending does not update that Finding field: it can remain
`NONE` after an actual indeterminate provider request. The bundle audit has no
immutable source/lane reservation joining accepted source, bounded obligation,
owner incarnation and complete native send membership. Its payload row can be
replaced, and the public bundle reader currently omits the stored payload bytes.
Unavailable or mismatching historical bytes cannot establish old scope.

`tests/test_legacy_review_source_evidence.py` drives the production merge entry
with real accepted Strong findings, canonical root association, durable current
Issue/Codex ownership and controlled native transport. It demonstrates both an
exact old payload/fingerprint match and the misleading `NONE` value after a real
unknown send. A different registered requirement, duplicate/paraphrased guidance,
missing owner binding and missing historical payload all remain held across
reevaluation with no second wire request or old native-record edits. This is a
negative evidence regression, not a positive mixed-dispatch implementation.

A future ordinary split requires a shared durable source/lane registration
created before any correction producer can send that source: exact source,
PR/head, verified owner, registered requirement and bounded payload, plus its
consumptive native-operation membership. Every applicable producer must honor
that reservation. Only sources positively born under that contract, or sources
with independent exact no-effect proof, can establish no prior attempt; legacy
`NONE` records cannot be backfilled as unsent. Recovering an old scope additionally
requires exact immutable payload/native fingerprint and complete held-operation
membership, with overlapping or unverifiable obligations held. The legacy boundary adds no authority or automatic semantic classifier. A later
optional [accepted Strong scope](accepted-strong-correction-scopes.md) protocol
provides future explicit semantic authority and birth provenance without adopting
legacy records.

If native transport proves definite rejection and later bookkeeping raises,
ordinary intake consumes only the current exact task/full-identity proof under
the shared lock and releases only that request's pending claim. Stale receipts
are discarded before admission; absent, mismatched and ambiguous proof remain
fenced. This does not settle or rewrite other accounting/uncertain operations.

The common Codex owner fence now also holds the v2 prototype behind any ambiguous
wildcard. Broad wire provenance alone provides no exhaustive registered
obligation eligibility; the optional accepted Strong source protocol is required; see [Durable Codex work accounting](durable-codex-work-accounting.md#before-wire-continuation-provenance-milestone).

The maintained PR-entry receipt contract supplies explicit typed native outcomes:
a fresh `NOT_DELIVERED` lookup without a shared claim permits the original
one-time handoff, whose confirmed receipt suppresses replay. `INDETERMINATE` or
unclassified receipt objects remain pending without a send. A `NOT_DELIVERED`
lookup with an older shared pending claim also remains held: native absence cannot
clear that claim. The parametrized lifecycle regression enters `_handle_pr_merge`
for both fresh delivery and these uncertainty cases, preserving the same cloud
owner and prohibiting local repair, merge and CI-failure routing.
