# Editable PR reviewers

Controller-designated ordinary PR adversarial reviews and Strong Audit may edit
code and add focused regression tests only for a demonstrated input-head defect
within the supplied explicit Requirements. They may run the commands needed to
establish that defect or verify its bounded correction. They use the existing
supported synchronous local backends and private workspaces. The dedicated
Strong route remains independent of ordinary routing, and automatic
implementation-session resumption stays disabled. Issue/specification reviewers
and explicitly no-edit verification remain read-only.

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

## Correction eligibility

An editable reviewer first assesses the clean admitted input HEAD. A retained
correction needs a demonstrated current explicit-Requirement violation, an absent
explicitly required test deliverable, or a material test-oracle gap satisfying
the existing bounded-gap criteria. Its evidence identifies the exact Requirement,
pre-edit counterexample or missing oracle, production boundary, material
consequence, and required corrective outcome. Optional test expansion, additional
input variants, refactoring, style cleanup and speculative hardening do not
justify a correction. Existing accepted findings keep their original identities
and scopes; a historical finding alone does not establish a current defect.

When the input-head assessment satisfies the response protocol's PASS criteria,
the reviewer returns PASS without editing source. Missing decision-critical
evidence remains INCONCLUSIVE. INCONCLUSIVE permits bounded investigation and
verification, not speculative retained changes. An ordinary PASS does not
prevent the independent Strong Audit from identifying a genuine defect under
the same standard.

The structured response always assesses the admitted pre-edit HEAD. An editing
ordinary review retains NEEDS_FIX with concrete implementation/deliverable findings
or NEEDS_TESTS with qualifying material oracle gaps; Strong Audit retains FINDINGS
with complete portable findings. A local fix or green post-edit test does not
turn that input-head result into PASS. The reviewer separately identifies the
correction and post-edit checks in its summary or evidence, without treating them
as committed input-head proof or independent closure evidence.

The controller validates the existing role-specific response and Requirement
associations before allowing source publication. A changed workspace alone is
insufficient. PASS, INCONCLUSIVE, malformed output, unknown Requirement references
or missing concrete correction evidence refuse publication before any corrective
commit or push. The unchanged-source review path retains its existing verdict
and closure rules. This deterministic check verifies the structured evidence
contract; the reviewer remains responsible for the semantic truth and bounded
scope of its evidence. Prompt-regression evaluation remains advisory.

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

Eligible settled reviewer corrections reuse `LocalReviewRepairStore` and
`execute_local_review_repair` with a captured-output executor. No second model
is invoked to copy or publish the already produced correction. The review
output identity deduplicates publication; it is not an accepted finding or a new
repair-allowance grant.

The publisher copies the selected source changes to an exact-head correction
workspace, uses the centralized commit/push helpers, and leases the existing
same-repository PR branch against the reviewed SHA. It rechecks current target
authority before publication. Before a new commit or send it also reconfirms each
cited Requirement ID and text against the live explicit Requirements, even when
HEAD is unchanged; already-entered sends retain the existing exact-candidate
readback path. A concurrently advanced branch cannot be overwritten. Commit
preparation does not invoke a formatter or another model to change the captured
correction after review. Failed preparation retains an
explicit incomplete state instead of declaring success.

A candidate commit is retained before push. Its checkpoint is separate from a
durable Git send-entry marker, which is recorded before the sole send. A
checkpointed candidate can be sent only when the record proves that no send has
entered. An entered send, or a legacy pending record with unknown send history,
is reconciled without retransmission: an unchanged or unreadable remote head
cannot prove that the earlier send was not accepted. A later observation of the
exact candidate settles publication without another push or reviewer invocation.
Pending or indeterminate output blocks blind reviewer replay and does not
authorize merge.

The editing review's saved transport identities are invalidated while accepted
lifecycle evidence is retained. The resulting candidate must pass independent
review in a fresh conversation and the existing CI/merge gates. The same provider
or model may perform that fresh review, but the editing conversation and its
verdict cannot approve their own correction. If a later reviewer makes another
source correction, its verdict is likewise withheld for the resulting candidate.
Existing accepted findings, ordinary closure rules and Strong Audit requirements
are not replaced by successful publication or a passing test command.

## Evidence-only supersession of retained corrections

Ordinary review resumption can release an old `indeterminate` correction only
when a newer head of the same open PR already contains its complete retained
output. It never creates a commit, pushes, resends a publication, or invokes the
old reviewer to establish this disposition.

New captured-output commit failures retain an immutable pre-push evidence
checkpoint. Legacy records require corroboration from the same durable attempt,
owner, registered detached Git worktree, original-only HEAD reflog, and a
nonempty fully staged correction. The worktree must have no unstaged/untracked
content or unfinished Git operation. The retained index, affected file contents
and modes, and control-file metadata must still match the settled failure;
post-failure modifications, even equal content restaged later, are insufficient.
A candidate, publication-entry checkpoint, missing ownership/history evidence,
or conflicting origin retains the blocker. Git inspection disables optional
index refreshes so observing a legacy record does not invalidate its evidence.

The controller reads current PR metadata without caches and independently
checks the remote branch. The replacement must be a changed descendant of the
original head on the same repository and branch. Every affected path must
match the retained Git blob and mode exactly, including additions, deletions,
executable bits and symlinks. Unrelated changes elsewhere are allowed; partial
or merely similar replacements are not accepted.

Settlement rechecks target and retained evidence while atomically comparing
the full attempt identity in SQLite. A unique supersession record retains the
replacement head and proof; the original error, attempt and worktree are
preserved. Concurrent evaluators and restart reuse that disposition, and stale
writers cannot replace it. A process exit after a new pre-push checkpoint
preserves the indeterminate output instead of admitting another execution.

Supersession is neither approval nor publication attribution. The current PR
processing pass defers, so even a head change during reconciliation cannot
reuse CI accepted for a previous head. The next ordinary pass applies normal
CI, independent review and merge gates. Current-head recovery and ambiguous
publication continue to use their existing rules. A precise evidence category
is exposed in the retained wait reason when reconciliation cannot proceed.

## Current-head commit identity recovery

Ordinary review and Strong Audit corrections use a verified, repository-scoped
Controller App as both Git author and committer. The configured controller
provider must prove `contents:write` for the repository. The App login is then
resolved through GitHub's bot-user endpoint; the noreply address contains that
bot **account ID**, not the App registration ID. Git receives this identity in
its individual command environment, preserving caller and global configuration.
The Reviewer App remains independent, and personal or OS identity is not a
fallback. See [GitHub's App committer example](https://github.com/actions/create-github-app-token#configure-git-cli-for-an-apps-bot-user).

This is a publication-time requirement. An unavailable Controller App retains
settled correction output and reports its blocker. It does not stop unrelated
worker startup; the startup policy and complete Controller App runtime migration
remain owned by issues #2939 and #2878 respectively.

When a retained correction still belongs to the current open PR head, normal
review resumption can recover a definite local Git identity failure. It accepts
legacy `Author identity unknown` records only with the existing immutable
worktree/history evidence, or a checkpointed controller-identity failure before
Git commit. Other failed hooks, uncertain invocations, moved heads, missing
ownership, modified output, or ambiguous publication cannot enter this path.
Newer-head incorporation continues to use evidence-only supersession.

A per-PR process lock and durable compare-and-swap journal preserve the original
attempt, error, source evidence and verified identity. Commit entry is recorded
before Git runs. The resulting candidate must have exactly the original parent,
captured file tree, author/committer and originating review reference. Its SHA
is durably recorded before the existing exact-head leased push. Lost candidate
checkpoints and lost push acknowledgments are reconciled from corroborated
state; an entered commit without a provable candidate is never blindly retried.
Neither recovery nor publication invokes the reviewer again. The new head still
requires its own CI and a fresh independent review before merge.
