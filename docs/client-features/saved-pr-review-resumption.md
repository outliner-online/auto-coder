# Saved PR review resumption

Saved PR review work is resumed through the typed, synchronous `pr_review_resumption` service before another reviewer is admitted. Both the early unresolved-thread route and the ordinary saved-result route use this boundary, which preserves closure-first ordering, the non-force 60-second unavailable-evidence deferral, force behavior, and an explicit `requires_reassessment` replay outcome.

The service is orchestration rather than a new authority. Existing review-attempt, accepted-finding, review-cycle, publication, and corrective-delivery owners remain authoritative, while `pr_processor` retains the caller-owned review runtime and every final merge gate. Its ports are bound at call time so controlled production-boundary substitutions remain available to regression tests.

The extraction is observability-neutral: it continues to emit existing processor stage events through typed ports, with the same stage identifiers, outcomes, and facts. No dashboard event schema, origin, or trace-to-view mapping changes.

When real replay of a saved NEEDS_FIX or NEEDS_TESTS result finds only resolved
or non-actionable native feedback, the ordinary caller consumes
`requires_reassessment` and continues to existing independent current-target
review admission in the same invocation. This also supports legacy published
results without a typed ordinary-correction payload. No new commit or force is
required, and replay sends no stale correction. The signal grants no closure,
PASS, approval, delivery or merge authority; current attempts, admission,
publication and final merge checks still decide the outcome. A replay without
that signal retains its existing repair, deduplication and wait behavior.

The ordinary transition emits `pr.adversarial-validation` with outcome `Deferred`
and facts `effect=saved-review-reassessment`, `requires_reassessment=true`,
`examined_head`, and `saved_verdict`. It is an admission request, not a successful
`pr.repair-delegation` delivery or a reused-review audit record. Subsequent
validation or refusal events describe the actual result; the dashboard uses its
existing stage/fact rendering with no new schema or processing origin.

Typed saved corrections also reacquire their selected canonical roots before
replay. A resolved authenticated root whose complete substantive discussion has
not been independently assessed at the saved input binding becomes an exact
`discussion_revalidation_thread_ids` obligation. The checkpoint is consumed
before Strong repair or saved ordinary replay, survives reconstruction, and
recollects the root and chronological replies even without an addressed marker.
Only those owned roots enter this local input view; native resolution is never
PASS, canonical acceptance, or permission to resend a correction.

A bound STILL_VALID disposition prevents another reviewer invocation for
unchanged evidence. New substantive replies require independent reassessment;
missing dispositions and incomplete or unauthenticated observations cannot close
the blocker. Accepted-Strong lifecycle, independent completion, publication, CI,
and merge gates retain their existing authority. Production emits
`pr.adversarial-validation` / `Deferred` with
`effect=saved-discussion-reassessment`, the exact `thread_ids`, `examined_head`,
`saved_verdict`, `requires_reassessment=true`, and a bounded reason. This records
an input obligation, not successful review or delivery.

### Recovering a completed review with a missing discussion binding

A completed, published same-head PASS can leave a pending discussion input when
an older closure effect discarded the normalized producing result. The consumed
review-admission key alone must not leave that PR waiting forever. When the
retained record still identifies the latest registered and published PASS for
the current repository, PR, head, base and opening, and its normalized ordinary
result is absent, the coordinator admits one independent binding-recovery review.
The additional admission key is stable for the same target and complete
substantive discussion; it does not contain an attempt number or a timestamp.
Both admission keys survive reconstruction, and a failed, interrupted or
incomplete recovery cannot repeatedly invoke the reviewer on unchanged input.
Missing discussion, unreadable authority, an unpublished result, a newer attempt
or a different target remains blocked.

This recovery supplies no approval or closure authority. The fresh review must
retain the complete current discussion and exact canonical concern scope before
the existing closure and publication gates can consume its result. Historical
closure payloads and effect identities remain unchanged, so confirmation resumes
the original pending effect instead of publishing a duplicate review. If a
closure contains findings introduced by multiple Strong rounds, each finding
must be matched to its actual originating round. Assigning every finding to the
latest round would reject an otherwise valid new discussion binding.

After the durable admission succeeds, production emits
`pr.adversarial-validation` / `Deferred` with
`effect=saved-discussion-binding-recovery`, `examined_head`, the selected
`thread_ids`, `requires_reassessment=true`, and `reason`. This event records
admission only; the subsequent review, publication and merge events record the
actual result. The existing dashboard stage and fact rendering is unchanged.
