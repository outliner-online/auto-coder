# Lifecycle-selected incremental PR review

Adversarial PR validation selects its initial or incremental review policy from
the accepted review lifecycle, independently of whether the current provider
can resume an earlier conversation. An accepted lifecycle with a prior reviewed
head therefore retains incremental review after a process restart, local
workspace release, or provider continuation fallback. With no accepted
lifecycle, validation uses the initial-review policy.

Provider ownership and resumability continue to control transport only. A
retained session is continued when its existing safety checks permit that;
otherwise validation uses the supported fresh no-edit invocation without
forcing an old local workspace to resume.

Every incremental prompt is self-contained. It supplies the current
Requirements manifest, current head and patch/test evidence, accepted prior
head, persisted canonical test-oracle gaps, and applicable accepted Strong
findings. It does not assume access to an earlier conversation or invent prior
requirement-coverage conclusions. When reusable coverage evidence is not
explicitly available, the reviewer establishes coverage from current evidence
while retaining incremental discovery and canonical gap-closure limits.

The advisory `incremental-pr-rereview` Promptfoo target evaluates the shipped,
production-composed incremental prompt against three distinct semantic cases:
convergence after the exact original oracle is protected, rejection of an
unrelated variant and parent-only demand as blockers, and detection of a
correction-induced material violation of an exact current Requirement. These
live-model observations remain separate from deterministic routing and parser
tests. The target's committed execution record truthfully records when no local
real-model evaluation occurred; CI records any later execution in its immutable
Prompt Regression advisory report.

## Dynamic-check follow-up transport

The controller chooses transport again before correcting a dynamic-check target
or asking the reviewer to assess its test output. Cloud sessions and local
sessions whose exact private workspace remains retained use explicit
continuation. A local adapter without a retained workspace, including the
current Codex adapter, receives a fresh invocation containing the complete
original review prompt, the preceding review response and the current focused
follow-up. The new call does not depend on provider conversation history and
is recorded as fresh execution.

Transport selection occurs before continuation is attempted. A refused retained
or cloud continuation remains a failure; it is never replaced after refusal.
The same current-head, Requirements, canonical-finding, source-correction and
thread-disposition checks apply to the fresh response. Successful tests alone
cannot supply a verdict, and a source-changing response still requires a later
independent review. Validation attempt evidence fingerprints the actual transmitted prompt,
including the complete fresh-call context and its Requirements manifest.

An operational BLOCKED result retained before this fix remains historical
non-passing evidence. The supported explicit `--only <PR> --force` entry can
request independent revalidation after the runtime is updated; no decision-store
reset or synthetic passing result is required.
