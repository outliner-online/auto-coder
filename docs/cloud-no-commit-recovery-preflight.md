# Cloud no-commit recovery preflight

Inspected 2026-11-22 against `outliner-online/auto-coder` main
`630d38c11b9d5aca2300b969d1c155cdb41e8294`, before behavioral edits.
Objective: recover accepted cloud implementation requests that demonstrably
produced no relevant implementation commit, without duplicating owned work.

The owner's subsequent scope decision selects existing PR-bound review-thread
and adversarial corrective handoffs as the first vertical slice. Running or
unknown provider execution is permitted; no execution-state detector is added.

## Traced authorities and selected boundary

| Boundary | Existing owner/source and decision |
| --- | --- |
| Initial Issue starts | `IssueDispatchService`, three `_process_issue_*_mode` producers, CloudRun/CloudManager and implementation slots. No uniform frozen acceptance clock plus proven branch baseline exists. Initial Issue and initial PR task launches are a separate slice, even if a PR later appears. |
| Review/adversarial PR corrections | `pr_processor` resolves exact ownership, checks correction allowance, renders a bounded PR/head/branch prompt and retains delivery identities in `cloud_review_repairs.json`. Extend this producer/file; never invent an old acceptance clock from observation time. |
| Delivery | Codex's logical-identity-aware native journal; Jules's authenticated same-session `send_message`; Claude's existing-session CLI and OAuth quota admission. Use the existing send abstraction with typed native-signature capabilities; Boolean False remains unknown. |
| Correction generation (#2141/#2142) | Keep existing owner, canonical bundle/generation and exhaustion gates. Checkpoints never create failed corrections, spend/reset their allowance or re-open exhausted blockers. |
| Publication (#1864) | `CodexPRRecoveryMonitor` retains its completed/no-PR Create PR reminder and 120-second grace. This slice requires an existing PR and an accepted correction, so neither initial task nor publication reminder is registered. |
| Jules checkpoint/recovery (#2092) | Initial prompt guidance is not runtime commit evidence. Live session reconciliation, approvals, failed-session recovery and no-PR messages remain unchanged. |
| Retirement (#2335/#2725) | Existing owner/incarnation/source fences remain in actual senders. Codex checkpoints use its ordinary retirement-accounted follow-up journal. |
| CI/review/availability | Ordinary PR processing and existing periodic exact-head CI invalidations revisit the handoff. Preserve CI, review, approval, scope, governor and Claude quota gates. Refresh target/head and ownership only when a checkpoint is due. |

`CloudTaskEngine.check_and_resume_tasks` had no production callers, only two unit
tests. Remove its sweep-only discovery/state helpers and constructor state-file
argument; retain live provider resolution, CI continuation and the manual CLI.

## Implementation decision

Retain original request identity, PR/head/branch/provider/task, acceptance clock,
ordinal and send reservation alongside review receipts. Use one cross-process
file guard for source writes, new acceptance and checkpoints. Persist the exact
ordinal before sending. Lost responses/crashes retain that reservation. Typed
pre-send quota refusals retry the same ordinal through existing quota admission.

At normal reevaluation, an unchanged bound target can receive one checkpoint
when 3600 seconds have passed since confirmed acceptance, at most three additional
sends. New review requests and other existing PR corrective sends invalidate the
old series under the same guard. Other origins do not become registration
producers. Closure, changed owner/baseline, an explicit addressed outcome or the
cap stops recovery. A head change invalidates the baseline; it does not classify
an empty, bot, base-merge or unrelated commit as implementation progress.

No new engine, DB, slot model, provider execution reader or live selection/timeout
is introduced. No duplicate open Issue/PR owner or numbered-Issue publication gate
was found. The supplied #5993 incident demonstrates a useful checkpoint prompt,
not why prior execution stopped. See [operator behavior and verification](client-features/pr-bound-no-commit-checkpoints.md)
for delivered scope and limitations.

## Focused verification

The final production-boundary selection passed **328 tests in 21.75s**, exit 0:

```bash
env -u CODEX_HOME CI=true UV_CACHE_DIR=/workspace/.uv-cache \
  AUTO_CODER_LOG_FILE=/workspace/auto-coder-runtime/logs/auto-coder.log \
  AUTO_CODER_DISABLE_AUTO_UPDATE=1 PATH=/workspace/auto-coder/.venv/bin:$PATH \
  bash scripts/test.sh --ci-tests-only \
  tests/test_pr_no_commit_recovery.py \
  tests/test_codex_cloud_pr_review_flow.py \
  tests/test_adversarial_validation_pr_flow.py \
  tests/test_cloud_conflict_delegation.py \
  tests/test_claude_followup_admission.py \
  tests/test_pr_repair_exhaustion.py \
  tests/test_dashboard_observability.py::test_pr_no_commit_checkpoint_reaches_mounted_detail \
  tests/test_cloud_task_engine.py \
  tests/test_verified_codex_pr_repair_routing.py \
  tests/test_codex_cloud_pr_ci_flow.py \
  tests/test_codex_cloud_followup_script.py -q --timeout=120
```

`.venv/bin/mypy --config-file pyproject.toml -p auto_coder` passed across **376
source files**. Black check, isort check, flake8 on all seven changed Python
files, and `git diff --check`, passed. Test transports were local controlled
boundaries; no live provider follow-up was run. The test collector could not
save its redundant JSON log under the read-only home directory; test execution
itself completed with exit 0. Unsetting inherited `CODEX_HOME` lets the manual
CLI tests use their own temporary test credentials; credentials were unchanged.
