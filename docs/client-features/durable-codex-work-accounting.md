# Durable Codex work accounting

Ordinary Issue-owned implementation reservations can carry a durable inventory
of Codex operations. Each operation is identified independently of its provider
task ID and retains its source request, causal baseline, execution, publication,
tracking, and settlement evidence. Mutations share the implementation owner's
cross-process serialization and atomically advance the slot activity revision.
Owner and store locks use a canonical, process-local coordination registry shared
by repository instances. Reentry on the owning thread retains the outer file
lock, including when a Codex client constructs a fresh repository inside the
engine's owner lock. Other threads and processes remain excluded. Acquisition
waits at most 30 seconds and raises `ImplementationSlotUnavailable` before
transport when the lock cannot be acquired; it never discards ownership to send.
Acceptance is a monotonic fact: later ambiguous delivery cannot reclassify
accepted work as definite non-delivery. Settlement evidence must match the
operation's source request, task when bound, and causal baseline when present.

An absent, malformed, or unreconciled inventory is not an empty inventory and
cannot authorize retirement. Complete-empty initialization is limited to fresh
admissions without earlier activity, or to a receipt that identifies a complete,
consistent enumeration of durable sources and their correlated operation IDs.
Reconstruction receipts also retain a non-empty consistency identity for every
required production source. The accounting reader rechecks those identities;
any read failure, addition, removal, rebinding, or mutation makes the inventory
unreconciled until a new complete reconstruction succeeds. Reconstruction
uses two complete observations and refuses a receipt when a source changes
between them. The required inventory includes CloudRun reservations and
bindings, owned retry authorization and dispatch, follow-up delivery records,
repair admission, and initial-PR recovery records.
Malformed operation or settlement evidence makes the snapshot unavailable.
Retirement consumers receive an
immutable snapshot and a guard that holds owner and store serialization through
their removal boundary; foreign-repository, stale, or unsettled snapshots are
non-releasable.

This authority does not submit provider work, grant retries, inspect production
provider sources, change GitHub state, or enable Codex retirement.

The production fence accepts only an authoritative repository, Issue owner,
reservation incarnation, logical operation, source request, optional task, and
causal baseline. It registers that operation under the implementation owner's
serialization before calling transport. A stale/retired incarnation,
conflicting identity, incomplete accounting inventory, or replay fails closed;
a replay therefore cannot spend another POST. Delivery is projected without
equating provider acceptance with execution/publication/tracking completion,
and ambiguous delivery remains unresolved.

Production initial submission now holds that owner fence across the durable
CloudRun claim and CLI creation. Codex Cloud continuations (including repair
callers that share the client) resolve their Issue owner from retained CloudRun
task provenance and hold the fence across follow-up journaling and WHAM. The
initial-PR recovery monitor applies the same boundary around its recovery-store
reservation and direct WHAM request. Untracked tasks remain outside this
Issue-owned policy, while a known task with missing, conflicting, or retired
ownership fails before transport.

Daemon startup reconstructs every active Issue reservation from the production
CloudRun, CloudManager, retry-authorization, RetryDispatch, follow-up, repair,
and PR-recovery stores. Store-native logical snapshots provide consistency
identities for SQLite sources, while file journals use content identities. The
same production readers are invoked before later sends and retirement reads,
so a restart or material source mutation cannot silently preserve a stale
complete receipt.

Reconstruction may bind an unbound pre-send operation to the task ID in its
durable accepted receipt. This recovers retry dispatch acceptance after startup
accounted for the claim but before the provider returned its task ID. The source
request, kind, and causal baseline must still match. An existing task ID cannot
be replaced or removed, and settled or definitely unsent operations cannot gain
a task binding. Binding records acceptance without settling execution or handoff
obligations, so subsequent CI continuations can pass the normal fence without
making the Issue reservation releasable.

Ordinary due reclamation refreshes a complete reconstruction under the existing
owner guard before reading accounting. It can bind the original prepared
follow-up message fingerprint and a verified PR-recovery handoff to that same
logical operation. A fingerprint or PR binding cannot subsequently change or
disappear. Publication and tracking are complete only when the retained
`pr_observed` recovery record has a completed handoff and the same PR is a member
of the current Issue reservation. An unrelated handoff supplies no authority.
The existing-PR repair producers carry their exact repository/PR/head/branch
contract to the sender. Before POST, the sender validates its bounded prompt and
records that contract with the actual Issue/incarnation under the same owner
guard. Reconstruction requires every grouped source record to agree with that
binding. A generic continuation, replacement-publication request or historical
record without this producer binding remains unsupported for handoff completion;
an older task-level receipt cannot retroactively supply the missing contract.
For an unbound historical record, reconstruction retains only a candidate PR
handoff, with publication/tracking still unproven. The observer can establish the
missing operation responsibility from its exact fingerprint-bound native user
input only when that input matches the complete supported existing-PR producer
template (including consistent repository/PR/head/branch fields). An unrelated
or replacement request, an unsupported template or a conflicting partial journal
binding remains unknown. No historical journal is backfilled or relabeled.
The source token includes recovery records and PR membership, and is rechecked
at commit. Reconstruction never treats an unavailable source as an empty one.

Recovery keeps the current journals and retired history. Restore a qualified
worker build using an owned drain/stop and one observed start against the same
current state; never restore an older journal over later activity or clear an
unknown operation to free capacity. If correlation or the producer binding is
unavailable, retain the reservation with its ordinary reevaluation obligation
until independently supported evidence or an explicitly authorized disposition
exists. The repair never infers responsibility from the task-level handoff alone.


Accepted retry reconstruction also correlates its exact request, numeric attempt
and task with an accepted CloudRun and the same reservation's verified
`pr_observed` recovery handoff. Dispatch tracking/slot acknowledgement alone
does not prove PR publication. Both dispatch checkpoints, matching creation
provenance, completed recovery handoff and owned PR membership are required to
recover publication/tracking facts. The retry source consistency token includes
the run, recovery and owner binding, so later changes invalidate the observation.
This never declares execution settled: current, causally terminal native
provider evidence and the existing closed-PR predicate remain necessary.

## Before-wire continuation provenance milestone

Issue-owned native Codex follow-ups now freeze `send_provenance` in the existing
accounting operation, under the existing cross-process owner serialization,
before native journal reservation and WHAM. It binds the prepared-payload
fingerprint, complete sorted native membership and available exact PR/head/base
branches to that operation's existing task, source, owner and incarnation.
This is wire provenance, not an approved-obligation or source-birth certificate.
There is no new external envelope, setting or enablement flag. Broad/legacy scope remains `wildcard`. The later optional
[accepted Strong scope](accepted-strong-correction-scopes.md) producer additionally
binds its sealed birth revision and complete native membership here.

Reserved, interrupted and unknown wildcard work overlaps another follow-up,
repair or publication operation for the same verified subject. The original
owner-wide milestone hold is narrowed as described below. Legacy records lacking provenance
are also wildcard when reserved/unknown. The check runs inside the owner lock,
before creating another operation or entering transport; neither a different
message/lane/batch nor `force` establishes separation. All native correction
producers converge here, including broad CI continuation and the scoped v2
prototype. Existing direct publication recovery uses the same fence. Untracked
tasks retain their existing outside-policy behavior. Missing/corrupt accounting
continues to fail closed. Historical records are not assigned source birth or
new provenance.

When no ambiguous work exists, ordinary new broad continuation remains available.
A recorded wire attempt cannot be forcibly reused: that would overwrite its
before-wire provenance without reserving a new attempt. Existing accepted
unforced duplicates still use native idempotent receipt handling. This milestone
does not add retry/attempt sequencing or mint new identities for old work.

Native accepted, unknown and positive definitely-not-delivered outcomes are
retained independently of execution/publication settlement. The exact outcome
is persisted before later lifecycle bookkeeping, preserving known rejection if
that bookkeeping raises. Missing journal entries and generic exceptions never
prove rejection. Existing native reconciliation can update future provenance to
accepted only when prepared fingerprint and the entire observed native membership
match; no new provider getter or polling is introduced. It cannot create legacy
provenance or settle lifecycle responsibility. Existing checkpoint clocks and
acceptance timestamps are unchanged.

Mixed-send eligibility remains disabled for broad and legacy records. The optional
accepted Strong producer now supplies exhaustive future source birth and approved
membership through the ordinary canonical path; all native correction lanes claim
possibly overlapping births atomically at this boundary. Unsupported broad lanes
remain wildcard. Separately actionable scope
must come from trusted exhaustive records, never UUIDs, strings or digests.
Legacy #6009 is neither backfilled nor qualified as a Strong scope; however, an
ambiguous follow-up operation can be settled as `definite_non_delivery` when
complete WHAM history independently proves the request was never accepted (see
[Complete-graph non-delivery settlement](#complete-graph-non-delivery-settlement)).

`tests/test_codex_send_provenance.py` exercises real ordinary acceptance and native
pre-wire state, restart, broad continuation with/without force, legacy records,
pre-/post-wire crashes, exact positive receipt reconciliation and rejection with
bookkeeping failure. Scoped regressions now prove that the v2 journal cannot
bypass an old wildcard. The later accepted Strong scope tests supply the bounded future mixed positive.

## Verified PR subject separation

A wildcard is narrowed to its repository/PR number only when that PR is positively
bound to both the current owner incarnation's PR membership and the exact task's
durable CloudRun association. Both sides are re-read under owner serialization.
Different PR numbers can proceed under one Issue owner only when both also have
verified canonical writable head identities and those repository/ref pairs differ. SHA, base changes, close
and reopen activity do not distinguish the same PR. Task IDs or lane/kind names
alone do not prove separate subjects. Missing legacy provenance, association,
current ownership or target remains an owner-wide unknown overlap; no historical
record is backfilled or released. Repository namespaces remain independent.

The existing initial-publication monitor can supply its fresh, exact existing
no-matching-PR observation plus immutable launch/head-repository/head-ref binding.
Only after these and current ownership verify can a distinct canonical launch
head establish a different prospective PR subject. An ambiguous/noncanonical ref,
missing binding, closed Issue, existing publication or conflicting observation
cannot establish this separation. The monitor retains all its existing grace,
preflight, causal and publication-budget gates; no new polling or action exists.
Before-wire provenance freezes that launch/head binding, and the exact native
outcome is retained before lifecycle bookkeeping. If the run later acquires one
verified PR association, that numeric PR dominates the prospective branch subject;
multiple/unverified associations remain broader uncertainty.

This is subject separation only. Wildcards on one PR still overlap across heads
and force cannot bypass them. Same-PR mixed sends remain disabled unless both operations carry exhaustive
accepted Strong scope evidence described above. `tests/test_codex_subject_separation.py` starts
with a real ordinary unknown request and drives native continuation, the real CI
producer and initial-publication monitor. It covers independent PR/publication
positives, same-subject and evidence-deficient negatives, close/reopen activity,
stale incarnation and cross-repository controls while preserving the old records.

Different PR numbers with the same writable head repository/ref remain overlapping
even with different bases. Missing head identity cannot establish separation;
this also applies when older provenance has only a PR number. Same-PR head
changes never establish a new subject. Canonical repository casing is normalized
for comparison and ambiguous ref representations remain held.

Scoped corrections now carry their existing bounded `current_source` check and
exact registered-obligation comparison through the native sender. The existing
owner file lock is acquired before this final check, which runs after overlap
admission and immediately before accounting/native reservation and POST. It
rechecks current provider attribution, PR/head, source decision, contract/finding
and existing pending/allowance gates; no new engine, poller or permission exists.
A false result or failed authority read has a typed pre-wire refusal. Only the
exact scoped pending claim becomes `not_sent` under its existing shared lock;
there is no native reservation or provider request. Generic/post-wire failures
remain fenced. Untracked/unbounded senders cannot accept this scoped callback.

This final check is limited to the scoped correction path. Ordinary, CI, conflict,
checkpoint and initial-publication routes retain their current authority checks.
In particular, the initial-publication monitor still obtains its fresh remote
observation before owner-lock acquisition; its remote lock-wait freshness gap is
not fixed by this increment. The v2 scoped path still cannot enable same-PR mixed delivery; the later accepted
Strong source path reuses this final authority boundary for its qualified exception.
`tests/test_scoped_final_authority.py` uses an actual competing process/file lock,
observes a real failed flock attempt, changes only the remote fixture's head or
decision after outer checks and proves zero POSTs, plus exact positive and
ambiguous/accepted bookkeeping exceptions with durable replay suppression.

Existing-PR head identity comes from the existing authenticated attribution
producer's exact head-match publication proof, bound to the same repository, PR,
Issue, task, attempt and launch, together with current ownership and the frozen
producer head ref. Task-URL-only attribution does not retain canonical head proof
and remains insufficient for cross-subject separation. A record's label alone
cannot qualify mismatching ownership/launch/head evidence. No new attribution
record is synthesized at the send fence.

## Complete-graph non-delivery settlement

An ambiguous (`reserved` or `delivery_unknown`) Codex follow-up operation can be
settled as `SETTLED` with `definite_non_delivery=true` (`accepted=false`) only
when WHAM supplies complete, fail-closed proof that the prepared message never
entered the task's turn history:

- `CodexWhamClient.get_task(task_id)` returns a live `WhamTask` whose
  `current_assistant_turn` is in a terminal status (`completed`, `failed`,
  `error`, `cancelled`, `canceled`).
- `CodexWhamClient.get_task_turns(task_id, require_complete=True)` returns an
  unpaginated, complete turn set whose parent/child graph passes bidirectional
  acyclicity verification (`codex_followup_causality._parents`), with every
  assistant turn terminal.
- Every user turn exposes readable input text (`native_user_text`) and zero user
  turns match the operation's recorded `request_fingerprint`.
- The operation's recorded `causal_baseline` already has a completed later
  `user -> terminal assistant` successor chain, preventing premature settlement
  of an in-flight POST.

When all checks pass, `followup_non_delivery_evidence` derives a deterministic
`wham-complete-graph-not-delivered:<sha256>` evidence ID. Under owner
serialization, `reconcile_proven_non_delivered_followups` verifies no native
journal member is `delivered`, removes only the proven-non-delivered native and
shared pending entries, settles the accounted operation via
`CodexWorkAccounting.settle_proven_non_delivery` (clearing `task_id` to `null`
to preserve the strict `task_id => accepted` schema invariant while retaining
`causal_baseline`, `request_fingerprint`, and the cryptographic evidence ID,
without ever reclassifying `ACCEPTED` work), and refreshes the reconstruction
receipt. This reconciliation runs automatically
before overlap fencing in `CodexCloudClient.send_followup` and is also exposed
for inspection and explicit execution via
`auto-coder pr-repair reconcile-codex-followups --repo <owner/repo> --pr <number> [--apply] [--json]`.
