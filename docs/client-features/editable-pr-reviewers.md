# Editable PR reviewers

Controller-designated ordinary PR adversarial reviews and Strong Audit may edit
code, add focused regression tests, and run the commands needed to verify or
correct the supplied explicit Requirements. They use the existing supported
synchronous local backends and private workspaces. The dedicated Strong route
remains independent of ordinary routing, and automatic implementation-session
resumption stays disabled. Issue/specification reviewers and explicitly no-edit
verification remain read-only.

Designated PR and Strong reviews select each backend's normal `options` list;
read-only invocations select `options_for_noedit`. Configure custom provider
arguments in every mode-specific list that uses that provider. The lists are
selected independently and are not automatically merged.

The permission fragments in `prompts.yaml` distinguish editable and read-only
review calls. Editable reviewers may perform relevant dependency setup, builds,
formatting, and focused tests, but must leave only intended source, test, and
documentation corrections. Disposable experiments, logs, caches and generated
verification output belong in `.agent-tmp/` or repository-ignored locations.
They do not stage, commit, push, change branches or HEAD, mutate GitHub, resolve
threads, or claim merge authority. These are workflow ownership rules within the
project's existing container/Git accident-containment model, not a new hostile
agent or network-isolation guarantee.

`bind_pr_review_designation()` scopes the bootstrap policy to the current
invocation and any provider/backend fallback inside it. The automatic unscoped
implementation baseline is skipped with `reason=pr_review_policy` and
`baseline=not_run`; this records no test success. Relevant focused verification
is permitted and the complete suite remains owned by CI. The policy resets on
normal return and exceptions and does not affect concurrent or subsequent
implementation work.

## Source changes and review evidence

A production editable review starts from the exact clean admitted PR commit.
`editable_review.py` captures the source-file state as well as HEAD before model
execution or dynamic checks. Contents, additions, deletions, file modes and
symlinks participate in the comparison; new ignored or disposable untracked
runtime output does not become a source correction. Tracked paths remain part
of the comparison even when their names resemble output directories.

If source is unchanged, the existing verdict, Requirements coverage, accepted
finding and thread-disposition rules continue to apply to that commit. If source
changes, the controller stops consuming the original-head verdict before it
can become PASS, closure evidence, a thread resolution, or merge approval.
An unchanged HEAD is insufficient because tests and reviewer edits may have
changed files without committing them. Commands that failed or were interrupted
are reported as such rather than being promoted to successful validation.

## Controller publication and recovery

Settled reviewer corrections reuse `LocalReviewRepairStore` and
`execute_local_review_repair` with a captured-output executor. No second model
is invoked to copy or publish the already produced correction. The review
output identity deduplicates publication; it is not an accepted finding or a new
repair-allowance grant.

The publisher copies the selected source changes to an exact-head correction
workspace, uses the centralized commit/push helpers, and leases the existing
same-repository PR branch against the reviewed SHA. It rechecks current target
authority before publication. A concurrently advanced branch cannot be
overwritten. Commit preparation does not invoke a formatter or another model to
change the captured correction after review. Failed preparation retains an
explicit incomplete state instead of declaring success.

A candidate commit is retained before push. Interrupted or ambiguous publication
is reconciled using the existing durable pending record and remote branch; a
confirmed matching candidate is not produced again by another reviewer. Pending
or indeterminate output blocks blind reviewer replay and does not authorize
merge.

The editing review's saved transport identities are invalidated while accepted
lifecycle evidence is retained. The resulting candidate must pass independent
review in a fresh conversation and the existing CI/merge gates. The same provider
or model may perform that fresh review, but the editing conversation and its
verdict cannot approve their own correction. If a later reviewer makes another
source correction, its verdict is likewise withheld for the resulting candidate.
Existing accepted findings, ordinary closure rules and Strong Audit requirements
are not replaced by successful publication or a passing test command.
