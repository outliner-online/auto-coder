# Current-head review after historical Codex feedback disappears

A confirmed review-feedback delivery can outlive its pre-send turn in the
provider's current history. The current task may have finished newer work, but
that history can no longer establish completion of the original request. The
old receipt remains consumed; its absent historical turn is not recreated or
reclassified as successful work.

Auto-Coder can admit the first independent review of the current PR head from a
separate observation of the settled current task. This admission requires the
existing exact PR/task ownership checks, complete current review discussion,
confirmed and correctly bound selected delivery receipts, and no uncertain
shared reservation or task-owned native receipt. It is available only when no
applicable current-head review verdict exists.

The native task must identify a successfully completed current assistant turn,
with corroborating latest status and no active current request. Complete native
history must contain the same current request and response in a coherent graph,
with no active assistant work. Each selected pre-send baseline still present
must retain its normal positive completion evidence. A missing historical
baseline instead requires a valid durable acceptance time before the current
assistant began. That time is an ordering guard, not evidence that the missing
request completed. Receipts whose baseline is present continue to use native
completion evidence, including requests whose local acceptance was recorded
just after their server-side assistant turn began.

Current native execution and task-owned receipts are rechecked after the
observation. Missing, contradictory, incomplete, active or changing evidence
retains the wait. A changed current PR, task owner or post-Codex review-thread
snapshot also invalidates admission.

The observation returns `current_task_settled`, separately from `completed`.
It starts an independent assessment of the current implementation; it does not
rewrite delivery receipts, replay old feedback, settle historical work
accounting, renew correction allowance, resolve threads or authorize merge.
Existing review limits, saved-verdict deduplication and final unresolved-thread
merge gates remain in effect. Any newly established defect proceeds through the
normal correction machinery and its existing publication/overlap checks.

The `pr.review-thread-gate` trace is `DEFERRED` with
`effect=settled-cloud-task-reassessment`, `examined_head`, and
`blocking_thread_ids`. The dashboard displays this separately from successful
historical completion and from subsequent validation, correction or merge.
No task identity or comment contents are added to the trace. Regression tests
cover both the native observation and the production coordinator admission in
`tests/test_codex_completed_review_feedback.py`,
`tests/test_codex_settled_review_feedback.py`, and
`tests/test_completed_cloud_review_reassessment.py`.
