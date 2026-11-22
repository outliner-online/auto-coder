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

A poll finding only prior delivery receipts is deferred while the unresolved
thread remains blocked. It does not claim a fresh accepted handoff. Receipts
prove prior delegation, not current implementation progress or completed repair.

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
membership, with overlapping or unverifiable obligations held. This boundary
adds no authority or automatic semantic classifier and is not implemented here.

If native transport proves definite rejection and later bookkeeping raises,
ordinary intake consumes only the current exact task/full-identity proof under
the shared lock and releases only that request's pending claim. Stale receipts
are discarded before admission; absent, mismatched and ambiguous proof remain
fenced. This does not settle or rewrite other accounting/uncertain operations.

The common Codex owner fence now also holds the v2 prototype behind any ambiguous
wildcard. Current wire provenance does not yet provide exhaustive registered
obligation eligibility; see [Durable Codex work accounting](durable-codex-work-accounting.md#before-wire-continuation-provenance-milestone).
