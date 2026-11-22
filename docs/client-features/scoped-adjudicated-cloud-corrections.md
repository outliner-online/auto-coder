# Scoped adjudicated cloud corrections

An already authorized numeric review adjudicator may submit an explicit v2
`UPHOLD/FIX` correction through the same native GitHub reply reader as v1.
The exact automated root, immutable physical source, current head/base/contract,
allowlists and decision graph still control applicability. No principal, role,
force/retry permission, dashboard session or closure authority is added. V1
parsing, rendered envelopes and serialized integrity bytes retain their format.
The dashboard v1 form is unchanged; this slice accepts literal v2 native replies.

V2 uses `<!-- auto-coder-review-adjudication:v2 -->` and exactly the v1 fields
plus `scoped_followup`, an object containing `task_id`, `owner_incarnation`,
`payload`, `distinct_from` and `distinction`. `distinct_from` is a list of
`[old_decision_uuid, exact_snapshot_sha256]` pairs. `distinction` is the same
adjudicator's explicit assertion that the new bounded correction is independent
of those named old payloads. The controller does not infer that relation from
prose similarity or canonical IDs. All fields must be nonblank; unknown fields,
other verdict/directive pairs and implicit force/publication actions are rejected.
The checked-in v2 schema describes the literal structure. Role markers cannot
turn an invalid adjudication envelope into free-form repair prose.

The current Codex-cloud owner must have verified PR attribution, the exact task
and active incarnation. The final reader repeats source, owner, open-PR, target,
allowance and existing pending-effect checks. Other providers do not qualify. A pass containing scoped decisions holds
unscoped UPHOLD effects rather than retrying them or updating their journals;
ordinary passes without scoped decisions retain v1 behavior.
The bounded payload is wrapped by the existing exact-PR repair contract and
ordinary native instruction preparation. The original root text and older
payloads are not appended. Existing sender, owner accounting and per-PR guards
remain the only transport path; no polling or validation invocation is added.

The producer freezes its physical source/body digest, actor, context, contract,
exact target, task/incarnation, bounded payload/native fingerprint and explicit
old references in `scoped_review_followups` within the existing shared review
state document. Its stable native identity is PR/task/decision-bound. The first
claim and pending identity commit before sending. Generic receipt and checkpoint
writers preserve this field. A prior claim is never renewed, including after
unknown, rejected or interrupted delivery; accepted claims suppress later sends.
Only the new claim's acceptance and receipt are updated. Old snapshots, native
records, pending identities and operation evidence remain untouched.

Every old uncertain operation must have a complete earlier producer snapshot,
a matching native receipt and current owner/accounting binding, native payload
fingerprint, target and causal turn. The referenced actor must be the same
adjudicator. Explicit references must exactly cover all retained uncertain scoped
sources for the PR. Legacy shared/native/accounting claims without such snapshots,
missing or mismatched snapshots, unnormalized accounting evidence, same payload,
changed owner/head, stale/edited source, and unconfirmed other effects stay held.
Admission reads existing accounting without enriching old evidence. The current
operation's native evidence is normalized before it becomes an older snapshot;
an interruption leaving that evidence incomplete conservatively holds later work.
A fresh native sender key or different batch key never proves non-delivery.

This route is separate from #2889 hourly checkpoints. It does not backfill old
acceptance clocks, retire uncertainty or enroll a new checkpoint series. Normal
new-correction invalidation of a prior series still applies. Unknown sources
remain merge blockers through the ordinary unresolved-thread and pending gates.
No `PUBLISH_EXISTING` action exists. Current #6009's repeated coverage guidance
is not qualified or converted; its legacy unknowns and same-scope work stay held.

`tests/test_scoped_review_followup.py` starts with actual authorized native
sources, persisted current CloudRun/PR attribution and owner accounting, the real
reader/planner/claim/native sender, and controlled GitHub/WHAM I/O. The old source
becomes unknown at its first permitted POST; an explicitly distinct second source
then produces exactly one new payload emission. Reconstructed readers/clients
preserve old source/native/accounting evidence and suppress replay. Controls cover
missing legacy evidence, native-key loss, authorization and principal changes,
source edits/collisions, owner/head/closure changes, same payload, pending effects,
unknown new delivery and definitive rejection. No ledger distinctness is seeded.
