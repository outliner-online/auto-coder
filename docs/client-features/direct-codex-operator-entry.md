# One-shot direct Codex operator entry

This entry is a staged product implementation, not an installed Docker or Codex
configuration change. Activation requires the operator's concrete approval of the
channel, CLI execution mode, private write roots, and persistent mount impact.
It does not relax the existing #2843 continuation or cleanup contracts.

`python -m auto_coder.direct_codex` accepts a repository, executable, model,
prompt file, timeout and optional editable mode. It uses a new private Git
workspace, a fresh private HOME/runtime and the existing
`InvocationProcessSupervisor` with mandatory Landlock and denial observation.
There is no bare-subprocess fallback or retry after a failed policy installation.
The selected CLI mode is meaningful only inside that supervised path. Local
container/capability/no-new-privileges/seccomp checks refuse an absent baseline;
they are not authoritative attestation of mounts or isolation. Operators must
review host checkout and home volume mounts, which may remain writable from the
controller even when provider writes are restricted.

No ambient HOME/config/auth files are discovered or copied. Only explicitly
supplied OPENAI_API_KEY/CODEX_API_KEY values can be forwarded; the CLI forwards
none by default and requires one explicit --credential-env selection per key. Actual provider
authentication compatibility is not established by this change. The entry must
not be used to revisit denied credential discovery or bypass a rejected path.

The result is a retained private workspace plus direct-child boundary evidence,
not a validated immutable artifact. It performs no caller writeback, git
commit/push, GitHub update, or deployment. Inspection/capture, verification of
captured bytes, caller-checkpoint checking and safe transfer remain separate
controller work. It deliberately has no session continuation or root reuse.
The JSON receipt reports `direct_child_completion` from the supervisor evidence
and reports `descendant_termination` independently as `unknown`; it never treats
the former as proof of the latter. Attempted execution retains
runtime/result state, including on failure, for explicit operator disposal.
This can consume disk space and is not a claim of automatic cleanup completion.

Read-only mode allows private runtime writes but not source writes; editable mode
allows only its private repository and runtime. The current Landlock producer
restricts writes, not general read visibility; do not claim it hides every
same-UID secret. Successful direct-child completion alone does not prove an
issue fixed or its tests passed. Strong Audit and semantic review are unchanged.

`tests/test_direct_codex_entry.py` covers the entry's argument/environment and
private-root orchestration using a synthetic supervisor. Those unit tests are
not real Codex, live kernel policy, or beta/release acceptance evidence. No real
service execution is part of the implementation pass.

`scripts/verify_direct_codex_kernel.py` additionally exercises the real entry,
private workspace, supervisor, Landlock and denial observer using a bounded shell
fixture in an already approved non-root Docker environment. It uses synthetic
repositories, no credentials, no real Codex, no inference service and no namespace
creation. It checks source SHA identity, no-edit write denials, editable private
writes with caller/peer protection, direct failure and finite timeout. It fails
if prerequisites are unavailable; it does not install or relax security settings.
This is not real-provider or release acceptance evidence.

## Explicit single-use login HOME

An operator may explicitly select `--prepared-home` after a separately approved
login into a new private HOME. No ambient HOME is discovered or copied. Its parent,
HOME and `.codex` must be non-symlink private directories owned by the current UID,
and HOME must be separate from the caller repository. Environment credential
forwarding and a prepared HOME cannot be selected together.

The entry places an exclusive single-use claim beside HOME, outside delegated
runtime write roots. Once claimed, it keeps the claim and authentication HOME after every subsequent
outcome, including failure before provider start; it never removes the claim or retries.
Authentication storage and renewal writes are permitted only inside that selected
private HOME. This does not provide session continuation or authorize credential
access, authentication changes, deployment or unrestricted execution. Exact model,
mode and runtime scope still require the operator's applicable approval. This
private one-shot path does not change production worker selection or its dashboard.
