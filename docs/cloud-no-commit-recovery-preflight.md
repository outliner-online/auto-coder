# Cloud no-commit recovery preflight

Inspected 2026-11-22 against `outliner-online/auto-coder` main
`630d38c11b9d5aca2300b969d1c155cdb41e8294`, before behavioral edits.
Objective: recover accepted cloud implementation requests that demonstrably
produced no relevant implementation commit, without duplicating owned work.

## Latest scope

The owner's later instruction permits a continuation while the task is running
or its execution status is unknown. No new turn/status reader or idle detector
is needed. The policy inputs are an accepted request, known commit baseline,
current owner/target, clock and counter. Send at confirmed-acceptance-relative
60-minute intervals, at most three additional continuations. Keep existing
sender identity, deduplication, uncertain-delivery, quota, approval and protection
gates. Use CI/review waits already available in context, without new readers.

## Producers and durable authority

| Boundary | Existing owner/source | Remaining integration |
| --- | --- | --- |
| Initial Issue | `IssueDispatchService` handoffs in `issue_dispatch.py`; the three `_process_issue_*_mode` functions; `CloudManager`; implementation slots | Accepted task and attempt are durable. Handoff `updated_at` is mutable bookkeeping, not a frozen acceptance clock. No shared initial owned-branch commit baseline exists. A configured publication ref is not proof of an observed branch/head. |
| PR review correction | `pr_processor` cloud-origin resolution, review-feedback reservation/receipt files, `ProviderRepairCoordinator`, `RepairAllowanceLedger` (#2141/#2142) | Reuse exact owner, canonical generation/bundle and reviewed head; record the acceptance clock and continuation ordinal against this original request. Not all live review deliveries flow through the coordinator. Elapsed time must not settle a failed correction or reopen exhausted blockers. |
| PR CI/conflict correction | Live CI `continue_if_paused`, conflict delivery journal and `_send_followup_with_quota_admission` in `pr_processor` | Separate existing producers have exact repair target/head evidence. They need registration in the same series contract, without changing ordinary CI routing. |
| Codex sender | `PendingCodexFollowUp`, `CodexCloudClient.send_followup` | Already retains logical identity, fingerprint, acceptance time and indeterminate delivery. Reuse it and its owner fence; do not add provider reads to classify running versus paused. |
| Jules sender | Authenticated `JulesClient.send_followup` / `send_message` | Real same-session send exists. PR reviewed-head evidence can support a second adapter without execution-state inspection. Its Boolean return does not distinguish definite non-delivery from lost POST response: retain unknown instead of trying a new logical send. |
| Claude sender | `ClaudeRoutineClient.send_followup`, `claude_followup_waits` | Same-session authenticated CLI and typed quota holds exist. It too can participate with exact PR ownership/baseline; absence of an initial observed branch remains unsupported evidence, regardless of normalized task status. |
| Initial publication | `CodexPRRecoveryMonitor` (#1864), started by `automation_engine` | Existing completed/no-PR recovery reserves one Create PR send after its own 120-second grace. Arbitrate with this obligation before either policy sends; do not change its timeout or equate no PR with no commit. |
| Jules reconciliation | `jules_engine.check_and_resume_or_archive_sessions`, called by `automation_engine` | Live failed/timeout, approval, feedback/no-PR and force-PR sends already exist. The no-PR retry can reset its count and repeat. Slot activity admission protects retirement, not cross-policy duplicate work. This owner must be included in arbitration, not shadowed by a second scanner. |
| Retirement | Implementation reclamation/retirement and Codex source reconstruction (#2335/#2725) | Reuse owner incarnation/activity revision; register admitted sends before outbound mutation so retirement cannot lose responsibility. |
| Normal gates | `pr_progress_policy`, strict current-head CI and review/blocker admission, provider governor and Claude quota waits | Preserve these gates. Do not add a new CI/review observer, execution detector, API, DB or slot model. |

#2092 is Jules initial prompt guidance, composed only for new tasks. It is not
runtime commit-progress evidence or authorization for blanket approval replies.
The supplied Outliner #5993 incident demonstrates a useful checkpoint prompt;
HTTP 200, READY and missing turns do not explain why prior execution stopped.

## Minimal scope decision

Multiple independent new **registration producers** remain necessary even after
removing all execution-state classification: initial Issue admission and the
existing PR review/CI/conflict handoffs. Existing publication and Jules recovery
also require shared send arbitration to meet the no-overlap constraint. There is
no single current producer whose wiring would cover the requested contract.

Use the request's explicit stop option rather than ship a guessed aggregate
scanner. Before enabling sends, explicitly own (1) a compact common series
record in an existing durable source, populated by those actual acceptance
producers with a proven branch/commit baseline, and (2) migration/arbitration of
existing publication/Jules sends through that same owner. No provider execution
state machine is needed. Unknown initial branch evidence is an explicit
unsupported case, not proof of no commit. This change completes only the
independently safe dead-sweep removal; the 60-minute/three-send policy is not
implemented or claimed tested.

`check_and_resume_tasks` has no production callers; only its two unit tests
invoke it. Its automatic client discovery, state-file helpers/constant and
constructor state-file argument have no other callers. The live
`get_client_for_provider`, provider interfaces, CI continuation, manual operator
CLI and Jules reconciliation are retained. Removal is observability-neutral:
there is no production call or trace emitter to remove. Resolver tests and
existing production routing tests verify retained behavior.

Open Issues/PRs and matching remote branch names were inspected; none was found
to own this policy. No numbered-Issue publication requirement was found in
current repository guidance. No Issue, PR, provider follow-up, configuration
change, deployment, merge or external message is created by this preflight.

Follow-on verification must exercise both initial and corrective production
registration with deterministic time and durable sources: 59/60 minutes,
acceptance-relative repeats, three-cap across restart/concurrency, existing send
dedup/ambiguity, commit and lifecycle/owner stop, plus two provider-shaped paths
and unsupported baseline refusal. Add trace reasons, runnable observability
coverage and operator documentation when the policy actually exists.

## Verification of the removal

`env -u CODEX_HOME CI=true UV_CACHE_DIR=/workspace/.uv-cache
AUTO_CODER_LOG_FILE=/workspace/auto-coder-runtime/logs/auto-coder.log
AUTO_CODER_DISABLE_AUTO_UPDATE=1 PATH=/workspace/auto-coder/.venv/bin:$PATH
bash scripts/test.sh --ci-tests-only tests/test_cloud_task_engine.py
tests/test_verified_codex_pr_repair_routing.py
tests/test_codex_cloud_pr_ci_flow.py tests/test_codex_cloud_followup_script.py
-q --timeout=120`: **46 passed in 1.46s**, exit 0. The log collector could not
save its redundant test-log JSON under the read-only home directory; test
execution and its result completed normally. An earlier run inherited
`CODEX_HOME`, bypassing the CLI test fixture's temporary credentials: 32 passed,
14 failed. Unsetting that environment variable made the unchanged manual CLI
suite use its intended local HTTP/CLI doubles; no credentials were changed.

`.venv/bin/mypy --config-file pyproject.toml -p auto_coder`: **success, 375 source
files**. Black check, isort check and flake8 on the two changed Python files,
and `git diff --check`, passed. These checks verify removal and preserved
resolution/CI/manual paths, not the unimplemented continuation policy.
