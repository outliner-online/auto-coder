# Jules retirement from ordinary Issue dispatch

Ordinary Issue backend selection excludes every candidate whose resolved
`backend_type` is `jules`, including aliases and the former built-in default.
Filtering retains the configured order and priority-group boundaries of supported
local, Codex Cloud, and Claude Routine candidates. A Jules-only policy therefore
has no eligible ordinary candidate rather than silently acquiring a default.

The ordinary adapter also rejects a directly supplied Jules candidate before any
provider effect is called. The candidate still crosses the shared
`IssueDispatchGuard` claim boundary, so durable accepted or indeterminate ownership
for the same logical attempt continues to suppress a fresh alternative dispatch.
The difficult/high-score cloud route likewise admits only Codex Cloud and Claude
Routine candidates.

Cloud startup is derived from the supported remote providers in the effective
ordinary selector, not from Jules configuration. The legacy
`--jules-mode`/`--no-jules-mode` CLI selector is retained only to produce an
explicit retirement error; it cannot alter the supported cloud policy.

This change is observability-neutral for supported dispatches: existing selection
and provider handoff events retain their stages and fields. Unsupported Jules
candidates cannot reach the provider selection effect, so no new dashboard event
schema or production-to-view scenario is introduced.
