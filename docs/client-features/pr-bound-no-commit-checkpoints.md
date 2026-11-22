# PR-bound no-commit checkpoints

Accepted cloud review-thread and adversarial corrective requests on an existing
PR can receive up to **three additional checkpoint messages**, at least **60
minutes** apart. The interval starts when the original request, or preceding
checkpoint, is confirmed accepted. Normal exact-head PR reevaluation services
the due check through the existing CI-watch cadence. There is no new scanner or
timer, and an elapsed hour alone does not send.

The shared policy supports Codex Cloud, Jules and Claude Routine through their
existing authenticated same-task senders and typed native call capabilities.
Codex receives the logical identity in its native journal; Jules and Claude use
their task/text signature while retaining the shared reservation. Claude's OAuth
quota admission and durable cooldowns stay controlling. Unsupported providers
cannot register a series. No execution-state read is added: the task can still
be running, or its state unknown.

## Admission and durable outcomes

Only an actually accepted corrective handoff with an exact PR, owner/task,
branch and head baseline registers a series. Its source identity, acceptance
clock, ordinal and reservation live in `no_commit_series` alongside feedback and
validation receipts in `~/.auto-coder/<owner>/<repo>/cloud_review_repairs.json`.
No credentials or task text are copied into this record. Reobservations never
reset its clock or cap. Shared cross-process locking serializes source writes,
new request admission and checkpoint reservation/dispatch.

The normal duplicate-correction wait and retained current-correction handoff
revisit the series. Before another send, current PR metadata and the exact
cloud owner are refreshed, with existing exhaustion and scope gates retained.
Other existing PR CI/conflict/adjudication sends invalidate the prior series
under the same guard without acquiring their own recovery budget. A newer send
whose delivery is unknown also prevents the old policy from sending more work.

Any observed head/branch/base change invalidates the old series. This is not a
claim that an arbitrary bot, merge, empty or unrelated commit implemented the
correction; new work requires ordinary admission against current evidence.
Closure or changed ownership ends the series. The existing addressed marker on
the final implementer reply ends ordinary thread recovery with
`acknowledged_outcome`, leaving independent validation responsible for the
finding. Natural-language completion claims and provider READY/PAUSED names are
not classified. The policy also represents authoritative `no_change` and
`input_blocked` outcomes as terminal without spending an ordinal; it does not
add a provider-output reader to infer those outcomes.

The exact logical ordinal is durably reserved before outbound send. Acceptance
advances the clock. A crash, exception or Boolean False retains that reservation
as unknown and forbids a new logical send after restart. A proven pre-send
Claude quota refusal/active hold retries the same ordinal after normal admission
allows it. Unknown sends require delivery reconciliation; elapsed time never
clears them. Checkpoints do not create failed corrections, change #2142 allowance,
request empty commits, approve findings, resolve threads or authorize merging.

## First-slice boundary

Registration covers review-thread and adversarial corrective handoffs made after
this policy is installed. Legacy receipts without a frozen clock are not
backfilled. Initial Issue starts, initial PR-conversion task launches, standalone
CI/conflict/adjudication corrections and first-PR/no-PR reminders do not register
series. A PR later appearing does not invent an initial request's missing clock
or baseline. Those existing behaviors, Jules session reconciliation, manual CLI,
CI-failure `continue_if_paused`, readiness, configuration and merge/security
policy remain intact. The unused CloudTaskEngine restart sweep is removed.

## Verification and operator visibility

`tests/test_pr_no_commit_recovery.py` covers 59/60 minutes, acceptance-relative
repeats, the three-cap across restart/concurrency, unknown sends, quota refusal,
supersession, baseline changes, terminal outcomes, unsupported providers and
preservation of source receipts. `tests/test_codex_cloud_pr_review_flow.py`
exercises accepted production handoff and duplicate processing with all three
native signatures, current-scope refresh, explicit addressed outcome and the cap.
`tests/test_dashboard_observability.py::test_pr_no_commit_checkpoint_reaches_mounted_detail`
traverses passing-CI PR processing and durable registration to a Jules checkpoint,
while active CI suppresses it before the corrective handoff.

The `pr.no-commit-recovery` stage reports provider/task, head, ordinal, reason and
next eligible time. `accepted` confirms assignment only. `delivery_unknown` and
`observation_or_delivery_unavailable` retain uncertainty. `baseline_changed`,
`owner_changed`, `closed`, `acknowledged_outcome` and `limit_reached` stop that
series. Inspect the stage and delivery file when investigating holds; do not
reset a reservation merely because another hour passed.
