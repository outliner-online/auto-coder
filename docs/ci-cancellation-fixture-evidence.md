# Selective cancellation recovery: GitHub fixture evidence

This is the bounded non-production acceptance control for issue #2888. It verifies
that two independently cancelled roots can be selectively recovered in sequence,
using fresh native identities, without executing an unrelated successful shard
again. The controller's production-entry regressions cover admission, persistence,
concurrency and repair classification separately.

## Verified control

- Date: 2026-11-24, 10:42–10:45 UTC.
- Repository: `kitamura-tetsuo/auto-coder-test-pr2835-v10`, an existing private
  disposable acceptance repository.
- [Fixture PR #2](https://github.com/kitamura-tetsuo/auto-coder-test-pr2835-v10/pull/2),
  with isolated base/head fixture branches. The fixture does not modify the
  repository's default branch or its existing workflows.
- Exact PR head: `374361598d57b0be006e787afb1428e8e219cb1e`.
- [Workflow run 45377259641](https://github.com/kitamura-tetsuo/auto-coder-test-pr2835-v10/actions/runs/45377259641),
  workflow ID `428911176`, event `pull_request`.
- [Executed workflow](https://github.com/kitamura-tetsuo/auto-coder-test-pr2835-v10/blob/374361598d57b0be006e787afb1428e8e219cb1e/.github/workflows/issue-2888-cancellation-fixture.yml).
  Its reusable source is `tests/fixtures/ci_cancellation_recovery.yml`.

The links require access to that private repository; readers without access cannot
independently retrieve its API records. The identities and observed outcomes below
are recorded here so the verified platform behavior is explicit. They do not grant
access or represent a reusable credential.

The initial workflow was deliberately cancelled after both roots were running and
`Unrelated successful shard` had succeeded. This proves handling of **manual
cancellation of the current run**. It does not diagnose or demonstrate a runner
infrastructure failure. No production workflow, deployment, credential, permission,
or branch-protection setting was changed.

## Requests and observed results

| Operation | Native target | Response | Subsequent observation |
| --- | --- | --- | --- |
| Cancel the current initial fixture run | `POST actions/runs/45377259641/cancel` | HTTP 202 | Attempt 1 terminal; both roots `cancelled`, unrelated shard `success`, aggregate `failure` |
| Selectively rerun root A and its dependents | `POST actions/jobs/138386725989/rerun` | HTTP 201 | Attempt 2 terminal; A `success`, B `cancelled`, aggregate `failure` |
| Selectively rerun root B using its freshly observed attempt-2 identity | `POST actions/jobs/138387699076/rerun` | HTTP 201 | Attempt 3 terminal; both roots and aggregate `success` |

Before each request, the PR was observed open at the unchanged head and the run was
observed at the expected current attempt. Both job reruns used the selective job
endpoint. No whole-workflow or failed-jobs rerun was used.

After each terminal attempt, the exact-attempt jobs endpoint, the run jobs endpoint
with `filter=latest`, and the run jobs endpoint with `filter=all` were completely
paginated. The exact-attempt and `filter=latest` inventories agreed.

## Native job identities and execution times

All times below are UTC on 2026-11-24. Each row was terminal `completed`.

| Attempt | Logical job | Native job ID | Conclusion | Execution start–finish |
| --- | --- | --- | --- | --- |
| 1 | Root A | `138386725989` | `cancelled` | 10:42:06–10:42:59 |
| 1 | Root B | `138386725535` | `cancelled` | 10:42:05–10:42:59 |
| 1 | Unrelated shard | `138386725898` | `success` | 10:42:06–10:42:09 |
| 1 | Required aggregate | `138387225592` | `failure` | 10:43:02–10:43:05 |
| 2 | Root A | `138387696258` | `success` | 10:43:56–10:43:58 |
| 2 | Root B | `138387699076` | `cancelled` | 10:42:05–10:42:59, retained |
| 2 | Unrelated shard | `138387699639` | `success` | 10:42:06–10:42:09, retained |
| 2 | Required aggregate | `138387745132` | `failure` | 10:44:01–10:44:04 |
| 3 | Root A | `138388532882` | `success` | 10:43:56–10:43:58, retained |
| 3 | Root B | `138388435822` | `success` | 10:45:18–10:45:21 |
| 3 | Unrelated shard | `138388439877` | `success` | 10:42:06–10:42:09, retained |
| 3 | Required aggregate | `138388499253` | `success` | 10:45:25–10:45:29 |

GitHub re-materialized retained jobs with **new native job IDs and the new
`run_attempt`**, while preserving their original execution and step timestamps.
Consequently, the 12 records returned by `filter=all` do not represent 12 executions.
Root B's current eligible identity after the first rerun was `138387699076`, not its
attempt-1 ID. That current identity was accepted for the second selective request.

The unrelated shard's `started_at`, `completed_at`, `runner_id`, `runner_name`, and
complete native `steps` array were identical across all three records. Its steps
array, serialized as sorted-key JSON, had SHA-256
`93be49ea2d16dfba68c751cde76752a9ae05ad780b4e27d824e8f6d15191d5f7` in every
attempt. Root A's successful execution and complete steps likewise remained
unchanged in attempt 3. These observations establish that the unrelated successful
shard and already recovered A did not execute again.

## Final required aggregate

At 10:45:39 UTC, a separate Check Runs API read confirmed:

- Check/job ID: `138388499253`.
- Name: `Required aggregate`.
- Head: `374361598d57b0be006e787afb1428e8e219cb1e`.
- Status: `completed`; conclusion: `success`.
- [Final check](https://github.com/kitamura-tetsuo/auto-coder-test-pr2835-v10/actions/runs/45377259641/job/138388499253).

The workflow's final attempt also concluded `success`. The aggregate required all
three roots to report success through their native `needs` results. “Required” here
describes the fixture's contractual final check; this experiment did not create or
alter a server-side required-status-check rule.

## Bounded reproduction

Use a dedicated non-production repository and an isolated base/head branch pair.
Copy `tests/fixtures/ci_cancellation_recovery.yml` to
`.github/workflows/issue-2888-cancellation-fixture.yml` and set its PR base-branch
filter to the selected fixture base. Keep the independent roots, successful shard,
`always()` aggregate and timeout bounds. Open a fixture PR with a harmless file
change. The roots wait for 240 seconds only on attempt 1; all jobs have finite
`timeout-minutes` values.

The following are **developer investigation commands**, not application code.
Production GitHub operations use the repository-bound `gh_cache.py` path.

1. Record the PR number, exact head, workflow run and attempt. Observe the unrelated
   shard's success and both roots running, then cancel only that fixture run:

   ```bash
   gh api "repos/$FIXTURE_REPO/pulls/$FIXTURE_PR"
   gh api "repos/$FIXTURE_REPO/actions/runs/$FIXTURE_RUN"
   gh api --paginate "repos/$FIXTURE_REPO/actions/runs/$FIXTURE_RUN/attempts/1/jobs?per_page=100"
   gh api --method POST "repos/$FIXTURE_REPO/actions/runs/$FIXTURE_RUN/cancel"
   ```

2. Wait with a bounded deadline for the run to become terminal. Read the exact
   attempt's complete jobs and confirm two native cancelled roots plus unrelated
   success. Revalidate the open PR, current head, run and attempt; send one selective
   rerun for root A's observed current native ID:

   ```bash
   gh api --method POST "repos/$FIXTURE_REPO/actions/jobs/$FIXTURE_ROOT_A_JOB/rerun"
   ```

3. When attempt 2 is terminal, read all pages of its exact jobs and the effective
   latest inventory. Record the newly materialized identities, retained execution
   timestamps and failed aggregate. Select root B only if its current eligible
   identity can be established, then rerun that current ID once:

   ```bash
   gh api --paginate "repos/$FIXTURE_REPO/actions/runs/$FIXTURE_RUN/attempts/2/jobs?per_page=100"
   gh api --paginate "repos/$FIXTURE_REPO/actions/runs/$FIXTURE_RUN/jobs?filter=latest&per_page=100"
   gh api --method POST "repos/$FIXTURE_REPO/actions/jobs/$FIXTURE_ROOT_B_CURRENT_JOB/rerun"
   ```

4. After attempt 3 is terminal, independently read its final aggregate check and
   complete `filter=all` job history. Require aggregate and workflow success at the
   unchanged head, and identical native execution/step records for every retained
   unrelated-shard record. Also require A's successful execution to remain unchanged
   during B's recovery. Close the fixture PR without merging it.

Stop on ambiguity, an unexpected head/attempt change, rejection, or a bounded
waiting timeout. Do not retry a potentially accepted request, send against a stale
job identity, or substitute a whole-workflow rerun.
