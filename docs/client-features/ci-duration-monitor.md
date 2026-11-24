# Daily CI duration review submissions

`CI Duration Monitor` runs daily at 21:17 UTC (06:17 Japan time) and supports a
manual `workflow_dispatch`. Manual runs default to **dry run**. The monitor only
runs on the repository default branch, uses `contents: read`, `actions: read`,
and `issues: write`, and does not execute code or download artifacts from the
observed PR revisions. A shared concurrency group serializes scheduled/manual
publication; `cancel-in-progress: false` avoids interrupting issue submissions.

## Detection and evidence

`.github/ci-duration-monitor.json` names the ordinary PR/source/browser/live/
worker workflows. Known matrix shards are
combined into one logical family; any other job in a monitored workflow receives
its own exact-name family. A new workflow requires an explicit config entry.
A family can opt in to `require_test_step: true`, so only recognized jobs
containing the configured test step contribute samples. Step presence does not
require success or valid timestamps: failed tests and unavailable optional timing
retain their existing handling. Families that omit this option retain their
job-name matching behavior.
Publication, promotion, rollback, manually selected impact experiments, and prompt
model evaluations are outside this initial monitor's CI allowlist.

The GitHub REST run-list API discovers runs **created in the last 48 hours**.
Every completed attempt of those completed runs is inspected through its exact
attempt jobs endpoint, across all pages. Reruns of runs originally created before
that window are not discovered; use a larger manual `lookback_hours` to inspect
them. A search exceeding GitHub's 1,000-result cap fails visibly instead of
claiming that a truncated search was complete. The current/latest run must have
completed; an in-progress latest attempt is inspected on a later scan.

A successful, failed, or timed-out job qualifies at **600 seconds or more** from
`started_at` to `completed_at`. Cancelled/skipped jobs and incomplete timestamps
are not treated as measured slow work. Queue time is separate and can be unknown. GitHub can reset `created_at` for
retained jobs in a rerun while keeping their original execution timestamps; a
negative queue interval is reported as unknown, preserving the valid job runtime
and original completion time. Other out-of-order intervals are unknown rather
than zero: invalid job durations are skipped, and invalid optional step/workflow
timing does not abort the scan.
All qualifying evidence in the window is considered, even if five newer fast
runs exist. Issues show the latest five per-family run-attempt maxima and the
qualifying sample when older. Known sibling shards share one family maximum;
the table includes shard count and min/max runtime.

Step durations are elapsed Actions command durations. They can contain harness,
setup, retry, and test work; they are not native pytest phase measurements. The
monitor reports the three longest steps and suggests split/rebalance review when
the configured test-command steps occupy at least half the job. Otherwise it
suggests investigating setup/runtime first. It does not apply a split itself.

Workflow elapsed time is measured separately from attempt start through the last
job completion. A workflow exceeding 600 seconds while all measured jobs remain
short produces report evidence only, because queueing, dependency ordering, and
aggregation are not proof that job splitting would help.

## Issue ownership and review handoff

The English issue template lives in `.github/ci-duration-issue.md`. It includes a
fixed Objective, explicit `REQ-NNN` contract, acceptance scenarios, non-goals,
source pointers, and a repository-specific initial implementation plan. This is
a review candidate, not a claim that metadata alone proves a safe partition.
Reviewers must inspect current code, repair material gaps, and decompose missing
independent prerequisites before implementation.

The monitor creates the complete issue with only the `ci-duration-monitor`
management label, posts an initial identity/evidence comment, then adds
**`review-ready` as the final mutation for that submission**. Both the body and
the retained initial comment carry an exact stable repository/workflow/family
identity. A reviewer can rewrite the issue body without breaking deduplication.
The management label and initial comment should be retained.

Subsequent evidence is append-only in comments. Reviewer-edited bodies are never
replaced. Exact run/attempt/job evidence markers suppress duplicate comments.
The monitor checks current labels and paginated issue events: a historically
applied `review-ready` label is never reapplied after a reviewer consumes it.
An issue created before an interrupted/failed readiness operation is completed
on a later run only when there is no historical readiness event. Complete pending
submissions are recovered from retained identity/evidence even after the original
run ages outside the discovery window; incomplete edited specifications are not
submitted automatically. Failed writes
are not blindly retried; an accepted operation with a lost response is recovered
from GitHub on the next scan.

An open managed issue is reused. A closed issue is never reopened; a new issue is
created only for a qualifying job completed strictly after the latest closure.
Historical overlapping scans alone cannot recreate a closed issue. Removing the
management label or deleting the retained identity comment and body marker removes
the identity evidence this automation relies on.

`GITHUB_TOKEN`-origin issue events do not start other Actions workflows. This
monitor submits the label for external/polling reviewers; it does not change the
existing Auto-Coder implementation-readiness subsystem or dispatch it directly.

## Operation and verification

Run a local read-only scan with:

```sh
uv run python scripts/ci_duration_monitor.py \
  --repository outliner-online/auto-coder --source-ref "$(git rev-parse HEAD)"
```

Add `--publish` only to create/update issues. Publishing requires `GITHUB_TOKEN`
(or `GH_TOKEN`) and an existing `review-ready` label; the dedicated management
label is created if absent. The adapter uses
`auto_coder.util.gh_cache.GitHubGitDataClient`, preserving the repository's
established GitHub request boundary. No `gh` subprocess is used by the monitor.

Logs go to stdout and a rotating `ci-duration-monitor-output/monitor.log` with
filename/function/line fields. The JSON report and Markdown summary are saved in
that directory and uploaded as a 14-day Actions artifact; the summary is also
appended to the Actions step summary. API/configuration failures fail the monitor.
Safe GET requests retry transport failures and HTTP 502/503/504 at most three
times, waiting one then two seconds. Authentication/rate-limit/schema failures
are not retried, and mutations are always single-attempt; durable reconciliation
owns their recovery. Dry runs make no GitHub mutations, including label creation.

Focused regression verification uses
`bash scripts/test.sh tests/test_ci_duration_monitor.py`. The tests exercise the
CLI through its GitHub request boundary, including pagination, threshold
boundaries, matrix grouping, interrupted writes, reviewer edits, closure
recurrence, and readiness consumption. This scheduled tool does not alter
controller admission, backend execution, dashboard event schemas, test selection,
or CI qualification, so no dashboard trace contract changes are necessary.
