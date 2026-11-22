# Accepted Strong correction scopes

An existing authorized Strong audit can optionally attest a complete new correction
scope through its existing accepted finding. This extends neither reviewer identity
nor permissions and adds no endpoint, retry switch or provider polling. The server
supplies the frozen older inventory to the normal Strong input; only that normal
revision-bound Strong acceptance can register authority. Closure and native
adjudication v2 cannot create it. Omission preserves ordinary review behavior.

The optional `correction_scope` v1 object declares `exhaustive: true`,
`independently_actionable: true`, exactly one qualified existing `requirements`
entry, `independence_evidence`, and an exhaustive `older_operations` list. Each old
entry binds `operation_id`, `snapshot_sha256`, and complete sorted
`native_membership`. The already-authorized Strong reviewer supplies semantic
independence, explicitly against those named frozen records. Different identifiers,
text or digests never establish independence. V1 supports one complete implementation
finding per accepted result and no regression-gap/sub-Requirement partition.
Shared registered Requirements remain overlapping, including paraphrases. Exact
current authoritative Requirement text is required; Objective or unrelated prose
cannot create a duty. Canonical merged/ambiguous owners are held.

`strong_correction_scope.register_births` runs inside the existing owner lock,
then review transition/store locks, after normal authenticated acceptance checks.
Before the accepted result becomes visible it writes a producer birth inside the
existing owner accounting document. The immutable snapshot binds exact source
round/finding/payload, acceptance and finding-set revision, head/base, contract,
policy, open epoch, server reviewer provenance, owner incarnation/task/launch and
writable-head identity. It does not assign old NONE records a birth. A crash after
birth but before acceptance leaves an ineligible orphan and conservatively holds
later overlapping registration. There is no automatic orphan replay or repair.

The actual accepted-finding canonical bridge seals that birth only after observing
the complete, authenticated root publication. It checks exact rendered body bytes
and unique source/canonical owner, then freezes all existing ordinary native
comment identities, root hashes and a sealed revision. Edited, missing,
untrusted or changed membership cannot qualify. Source revision, canonical aliases,
Requirement, outcome and exact unresolved root body are checked again at delivery.

Every tracked native correction lane reaches the existing `CodexWorkFence` and
`CodexWorkAccounting.register`. In that same atomic before-wire transaction,
`claim_sources` consumes the selected exact sealed birth. Broad follow-up, repair,
publication and checkpoint lanes conservatively consume any possibly covered
unattempted births; only positively verified separate writable subjects are
excluded. Unknown subjects are wildcards. Missing CloudRun evidence for a task
with a registered birth cannot fall back to untracked native sending. A scope
attestation alone is never proof of non-attempt, and missing native keys are never
such proof. Exact native receipts remain the only delivery evidence.

Ordinary review intake selects one sealed unattempted source, builds only its
bounded instruction from the registered finding and existing Requirement, and
uses its existing verified Codex-cloud owner. Under the common owner lock it
rechecks the accepted snapshot, complete reconciled old inventory, exact named
native groups and prompt fingerprint. Old native groups use the sender's existing
canonical `sha256(task + NUL + logical_identity)` lookup keys, typed journal reader,
exact operation/source/baseline and full recorded repair target. Since native rows
have no head-repository field, the existing authenticated publication attribution
must positively match the frozen launch/head repository/ref as well. Only persisted
`indeterminate`/`delivered` rows are admissible, and accepted accounting requires
all group rows delivered. Miskeyed, deficient or contradictory old evidence is held
without relocation or repair. Immediately before reservation/POST the
existing final-authority callback refreshes origin, open PR, exact head/base,
current contract/policy, root bodies, allowance, draining and pending-effect gates.
The source reader also requires an OPEN/RECURRENCE canonical blocker with no
reconciliation needs, so invalidation/correction or a scope reconciliation hold
revokes eligibility even when root IDs and accepted source bytes are unchanged.
Only two exhaustive approved same-PR scopes can bypass the old wildcard overlap
hold. Shared writable heads across PRs remain overlapping; genuine different
branches retain the reviewed subject-separation behavior. Force cannot bypass
birth, replay, authority or uncertainty checks.

An actual attempt permanently consumes its birth, including pre-wire interruption,
unknown response, positive rejection or later bookkeeping failure. This version
provides no automatic retry of that source. Final-authority refusal before native
reservation removes only its exact shared pending identities; it does not edit an
old attempt. Positive native rejection releases only the current exact shared
pending claim under existing proof rules. Generic exceptions and crash-after-wire
remain fenced across restart. Old source snapshots, native identities and pending
claims are never repurposed or marked delivered by new scope admission.

Legacy, incomplete, unattempted orphan/sibling, edited or unverifiable old scope
makes the context unavailable and prevents mixed delivery. Broad unsupported lanes
remain explicit wildcards; v2 adjudication alone cannot waive them. This is a
future provenance path, not legacy migration. Current #6009 remains held: its
uncertain history has no such complete producer records, and the newer comment
repeats the old executable/type-only coverage obligation. No publication-only
action is introduced. #2889 acceptance clocks and confirmed-delivery checkpoints
continue through their existing sender and series rules; a new actual accepted
correction enrolls through that existing path, without backfilling old clocks.

`tests/test_strong_correction_scope.py` exercises normal Strong parser/acceptance,
durable birth, canonical bridge and authenticated root observation, actual ordinary
intake and the native sender with controlled GitHub/WHAM read/write ports. The
first real send becomes unknown. A supported expanded-closure transition schedules
a later Strong round whose explicit independent failing-shard Requirement is
accepted and delivered exactly once, without seeding a decision, distinction,
birth or receipt. Old scope/native/shared evidence stays unchanged. Controls cover
unrelated/duplicate/whitespace/paraphrase obligations, legacy wildcard, incomplete
or forged old references, missing native membership, stale actor/head/contract,
edited roots/publication, a remote-only head change after outer checks, pending
effects, exhausted allowance, missing owner run,
alternate force lanes, exact cross-lane replay and crashes before acceptance,
before wire and after wire, and native accepted/unknown/rejection certainty when
later lifecycle bookkeeping fails. Miskeyed and wrong-repair-target native rows
remain byte-for-byte unchanged while blocking new sends. Real competing-process
owner-lock tests revoke canonical authority, add a reconciliation hold, or edit
only the physical root during the wait; all produce zero new POSTs and no new
native source claim. Reconstructed clients/evaluations never duplicate.

Run `bash scripts/test.sh --ci-tests-only tests/test_strong_correction_scope.py`.
The common contested-lock and unrelated-subject controls remain in
`tests/test_scoped_final_authority.py` and `tests/test_codex_subject_separation.py`.
These are controlled production-entry integration tests, not live-service e2e tests.
