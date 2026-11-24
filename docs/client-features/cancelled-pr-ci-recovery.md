# Selective recovery of cancelled PR CI jobs

Auto-Coder can recover native GitHub Actions `cancelled` test jobs once for each
repository, PR, exact head, workflow run and configured logical job. Manual
cancellation, runner interruption and an unavailable cause have the same
eligibility. A failed or timed-out job does not become a cancellation candidate.

## Explicit workflow configuration

Recovery is disabled unless normal PR test workflows and their exact, unique
native job names are configured. Use the existing repository-scoped `config.toml`
overrides. For this repository's normal test workflow, an example is:

```toml
[[repository."outliner-online/auto-coder".ci_cancellation_recovery.workflows]]
workflow_path = ".github/workflows/pr-tests.yml"
root_job_names = [
  "Lint & Type Check",
  "Tests with Coverage (1/4)",
  "Tests with Coverage (2/4)",
  "Tests with Coverage (3/4)",
  "Tests with Coverage (4/4)",
]
aggregate_job_names = ["Tests with Coverage"]
multi_root_verified = true
```

A root entry declares a normal test or quality job eligible for selective
recovery. Aggregate entries declare pure required-status reducers of those roots;
they must not contain independent tests or deployment work. Exact job names,
including matrix dimensions, must remain unique across the workflow. Renamed or
ambiguous jobs require configuration correction; names are never substituted for
missing native run/attempt/job identities. Duplicate paths, duplicate names,
root/aggregate overlaps and malformed configuration are rejected.

`multi_root_verified` defaults to `false`. Keep it false until the bounded
non-production fixture establishes both selective root reruns and the final
required aggregate without reexecuting the unrelated successful shard. The
[committed fixture evidence](../ci-cancellation-fixture-evidence.md) records the
successful GitHub control for this implementation. The example does not change
any running worker's configuration or enable a production repository by itself.
Manual dispatch, push, deployment and ambiguously associated executions do not
qualify as normal PR runs.

## Notification and reconciliation path

The existing webhook ingress persists `workflow_job` run/attempt/job identity and
native conclusion, including `completed` / `cancelled` candidates, in the same
transaction as its durable CI invalidation. Deliveries may repeat or arrive out
of order; they remain wake signals, never permission to write. The runtime verifies
that both completed workflow-job and workflow-run events have actually reached
that repository's ingress before enabling recovery. A configured repository
without this evidence reports `webhook_delivery_unverified`.

The normal PR evaluator consumes the same durable CI watch. Missing individual
notifications use its existing bounded 300-second reconciliation deadline; there
is no additional open-PR scanner or per-PR polling loop. A failed workflow can
contain cancelled jobs, so recovery examines the complete exact-attempt native
job inventory rather than guessing from the overall workflow conclusion. All
reads and the selective POST use the existing repository credential and shared
GitHub request governor. Cooldown/admission refusal defers an unsent operation
without switching credentials.

## Claims and current identity

The controller verifies an open, unchanged PR head, one unambiguous PR
association, the latest effective normal workflow run and its exact current
terminal attempt. It reads every job page and repeats current-identity validation
at the effect boundary. Obsolete heads, superseded attempts, active execution,
missing identity and ambiguous selectivity defer recovery.

`CancellationRecoveryStore` retains a SQLite atomic claim before the selective
`POST /repos/{owner}/{repo}/actions/jobs/{job_id}/rerun`. It serializes possible
effects for the same run across controller processes, and keeps a logical job's
one-time claim across attempts and restart. The default database is
`~/.auto-coder/ci-cancellation-recovery.sqlite3`; deployments may set
`AUTO_CODER_CI_RECOVERY_DB` to their persistent storage location. Accepted and
ambiguous requests remain suppressing. A definitive rejection is recorded and
is not retried against an obsolete identity. Only a proven unsent operation can
release its claim for a later fresh evaluation. Store failures prevent a POST.

Each evaluation sends at most one selective request. Subsequent notifications
or the retained watch reobserve GitHub before considering another root. GitHub
can give *retained* jobs new native IDs and attempt numbers while preserving
original execution timestamps. Consequently, a newer ID/attempt alone cannot
settle a prior ambiguous effect: the claimed root must show a new terminal
execution before another root is sent. Successful unrelated shards are never
explicitly rerun; GitHub owns the selected root's necessary dependents. There is
no whole-workflow fallback.

## CI, repair and dashboard semantics

A rerun request or acceptance is not CI success. The ordinary native required,
aggregate, review, deployment and merge gates remain responsible for eventual
success. Failed, active and unknown results remain non-success.

Cancellation itself no longer authorizes code repair. For configured workflows,
fresh native evidence separates interrupted roots and their pure aggregate gates
from independently failed tests. An independent failure still reaches the normal
exact-head repair authority, including durable local-job restart, Jules and Codex
Cloud routes. An unrelated optional cancellation cannot exempt an independently
failed aggregate. The immutable observation keeps every native conclusion for
merge decisions; `repair_trigger` changes only repair admission.

The `pr.ci-cancellation-recovery` trace exposes status, safe reason, run, attempt,
job, logical name and durable claim identity. Acceptance is an `accepted_handoff`;
uncertainty and refusal are `deferred`. `ci_success=false` prevents the diagnostic
from representing a passing test or completed merge. Credentials, transport
errors and signed URLs are excluded from recovery diagnostics. The existing
dashboard detail renderer displays the stage and evidence.

## Regression commands

```sh
CI=true INSIDE_TARGET_EXECUTION=true bash scripts/test.sh --ci-tests-only \
  tests/test_ci_cancellation_recovery.py \
  tests/test_ci_cancellation_repair_authority.py \
  tests/test_ci_cancellation_integration.py \
  tests/test_ci_repair_authority.py \
  tests/test_ci_webhook_intake.py
```

These tests enter the production recovery, repair and PR handler boundaries with
controlled native REST responses, real SQLite persistence, HTTP webhook ingress
and the dashboard detail projection. The separate GitHub fixture supplies the
non-mocked external proof of selective rerun behavior.
