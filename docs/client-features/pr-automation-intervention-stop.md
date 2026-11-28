# PR automation intervention stops

Auto-Coder stops automatic processing of an individual pull request when it
observes the first Revert commit or reaches a budget of 30 distinct PR commits.
The first observation includes existing branch history; installing the feature
does not grant a fresh budget to a PR that already contains a Revert or 30 commits.
A standard Revert has a subject beginning `Revert ` or `Revert:`, or a complete
`This reverts commit <SHA>.` line. Incidental discussion of reverting is not a
Revert. Every PR commit is counted, including empty, repair, review-correction
and merge commits, regardless of its author.

The controller stores observed commit identities and the stop in
`~/.auto-coder/pr_automation_guard.sqlite3`. Repository names are normalized for
case. A new head, force push, successful CI, resolved review, restart, `--force`,
ordinary repair-allowance grant, or a repeated webhook cannot clear the stop.
Previously observed commits still count after history is rewritten. Repeated
observations do not count a SHA twice. Other PRs keep their independent policy.

The history producer reads fresh REST PR metadata, enumerates commits with
pagination, and verifies unchanged head/base/count. A complete immutable history
may be reused only after another fresh metadata read confirms those identities.
GitHub's 250-commit listing limit does not permit an oversized PR: its authoritative
total count is sufficient to stop it. Incomplete history below the threshold,
request failures, malformed state and unknown schema versions defer processing;
they never grant permission or fabricate a permanent abnormality. A worker
retains a timed read retry for unavailable history; this is separate from an
untimed, latched intervention stop.

A latched stop blocks new PR cleanup/close/reissue, review and correction entry,
cloud followup, base-update commit/publication, local repair publication,
review-thread closure, review publication, approval and merge. Saved corrections
and background local-job finalizers enforce the same authority. Already-sent
publication readback and receipt settlement remain possible; a stop does not erase
work, roll back a branch, close the PR, or convert a failed check to success.
Local review output is also inspected for standard Reverts in its outgoing Git
ancestry before the controller commits and before either local publication path
pushes. A detected unpublished Revert is retained as local evidence, preserving
the last observed remote head and PR commit count. It stops the next controller
commit/push even when the editor also left dirty files. CI/conflict finalizers
exclude ancestry already proven to belong to their authenticated base commit.
Review editors are instructed not to commit or change branches; that older
request has no authoritative base identity, so an unexpected editor merge
importing a Revert is conservatively stopped rather than guessed safe.
The durable invalidation worker acknowledges a blocked evaluation without
starting another repair or creating a timed retry. Read-only polling/status and
new external invalidations can still observe the retained stop. An open stopped
PR still excludes its linked Issue from successor implementation dispatch.

This is an observation boundary. A cloud task already accepted before a stop
may produce several commits before the controller next observes it. A local
invocation already running may also finish its editing; its finalizer refuses
new publication once it observes the stop. The controller cannot retroactively
cancel accepted work or guarantee an absolute remote branch length of 30. It
issues no new continuation or merge after it observes the stop.

## Inspection and external intervention

`auto-coder pr-safety status --repo owner/repo --pr 42` displays retained evidence
and the stop/acknowledgement audit history without contacting GitHub.

After investigating and correcting the cause, an operator can acknowledge the
specific current head:

```bash
auto-coder pr-safety resume --repo owner/repo --pr 42 \
  --expected-head <full-current-head-sha> \
  --reason 'Describe the external intervention and its verification'
```

Resume uses the human-operator credential role and the existing daemon/startup
resolver: explicit `--github-token`, then `GITHUB_TOKEN`, authenticated `gh`
credentials, and GitHub CLI configuration. It creates a fresh native client with
the selected token. Controller-App configuration continues to govern
repository-scoped controller clients; it does not select the credential for this
operator command. A permission or history-read failure leaves the retained stop
and acknowledgement history unchanged, without trying a different credential.

Resume requires complete current commit history, the exact expected head and a
nonempty explanation. A concurrent newer stop invalidates an in-progress
acknowledgement. It retains the stop evidence, acknowledges all previously
observed/current Revert identities, including unpublished local Reverts recorded
in the audit history, and grants 30 additional distinct commits. Local evidence
does not have to appear on GitHub to be acknowledged.
A new Revert immediately stops it again. The acknowledgement does not start a
stopped worker or bypass CI, ownership, reviews or any other normal gate.
Ordinary polling or explicit target processing can evaluate the acknowledged PR.

## Default budget evidence

The default was selected on 2026-11-28 from the most recently created 200 merged
PRs in each of [auto-coder](https://github.com/outliner-online/auto-coder/pulls?q=is%3Apr+is%3Amerged+sort%3Acreated-desc)
and [outliner](https://github.com/outliner-online/outliner/pulls?q=is%3Apr+is%3Amerged+sort%3Acreated-desc).
Excluding dependency updates left 316 PRs (median 3, 90th percentile 15, 95th
percentile 25). Excluding the five PRs with standard Reverts left 311 PRs:
median 3, 90th percentile 14, 95th percentile 24. A threshold of `>=30` would
have requested intervention for 8/311 (2.57%); thresholds of 20, 25 and 40 would
have flagged 6.75%, 4.82% and 1.61%, respectively. These are retrospective
intervention rates among merged PRs, not failure probabilities. Legitimate
large work can require intervention too. Cumulative observed identities may
stop rewritten histories earlier than these current-history sample counts.

[PR #2946](https://github.com/outliner-online/auto-coder/pull/2946/commits)
contained 97 commits and 21 Reverts. Its first Revert was commit 25,
[`140a00aa8cbe3d52639c98277b7698b5bbee14db`](https://github.com/outliner-online/auto-coder/commit/140a00aa8cbe3d52639c98277b7698b5bbee14db),
and 72 additional commits followed it. The first-Revert policy raises the
intervention signal at that earlier point, subject to the observation-boundary
limitation above.

Run these bounded production-path regressions:

```bash
env -u AUTO_CODER_RUNTIME_ROOT INSIDE_TARGET_EXECUTION=true \
  bash scripts/test.sh --ci-tests-only \
  tests/test_pr_automation_guard.py \
  tests/test_pr_automation_guard_background.py \
  tests/test_pr_automation_guard_codex.py \
  tests/test_pr_automation_guard_local.py \
  tests/test_pr_automation_guard_effects.py
```

They cover strict-reader pagination and cache invalidation, latched restart and
rewritten-history refusal, manual grants and concurrent acknowledgement,
production admission and linked-Issue exclusion, saved correction and merge
gates, worker-to-dashboard evidence, real-Git background publication and readback,
native Codex continuation refusal without losing prior delivery receipts, and
unpublished local Revert refusal before another commit or push.
