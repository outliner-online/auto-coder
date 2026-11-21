# PR lifecycle directives and fresh draft admission

Issue ownership inferred from a PR body requires a closing directive at the
start of a line, Markdown list item/heading, or sentence/clause. All existing
closing verb forms remain supported, including past-tense forms, optional
colon and same-repository qualifiers. `This PR fixes #123` and successive
`Fixes #123, closes #456` clauses remain directives. Narrative such as
`Grid: existing closed #5459/REQ-007–008 ... No Issue Requirements or ownership
were rewritten` supplies context, not implementation ownership. Broad review
context extraction is unchanged. Issue-bearing branches, native connections,
and recorded provider or PR memberships retain their existing authority.

Startup discovery uses the same restricted parser before adding open PRs to
an active owner's durable membership. It must not learn an incidental
association and then use that association to block terminal reconciliation.
Closed Issues can still retain legitimately live implementation siblings;
current Issue state alone is not a reason to discard PR ownership.

Before a fresh autonomous draft reservation, the controller checks whether a
local repair backend is configured. With none configured it reads current
strict GitHub PR metadata and the existing cloud-origin resolver, constrained
to lifecycle-validated Issue ownership rather than broad review-context prose. A confirmed
draft without a usable provider origin is deferred before execution admission,
so it cannot consume capacity as either an Issue or standalone PR owner merely
to return a backend refusal. Unavailable or ambiguous evidence also defers
without reserving. Configured local routes, provider routes, a PR freshly found
ready for review, explicit manual processing, and already retained owners
continue through their ordinary gates. This does not start a provider, change
backend settings, enable specification review, or release existing work.

## Historical recovery boundary

The active slot schema records PR numbers, provider session IDs, execution
identities, admission flags, incarnation and activity revision. It does not
record which directive, branch, native association, or operator admission
created each PR membership. An existing `implementation_prs=[5940]` under
Issue 5459 is therefore indistinguishable in this schema from a legitimate
historical membership whose text or native link subsequently changed.
Reparsing current prose cannot authorize removal. Existing open memberships
remain retained through restart, including this historical association.

Automatic historical correction needs a separate supported, bounded operation
and positive source evidence unavailable in this schema. Such a design must
bind the exact repository, owner incarnation/revision and membership to proven
erroneous admission, establish complete absence of legitimate execution,
provider and repair/handoff ownership, fail closed on unknown or conflicting
sources, and revalidate under the existing coordination guards. It must
preserve genuine sibling work, historical start fencing and idempotency.
No JSON editing or broader reclamation is introduced here. Normal lifecycle
reconciliation can still retire an otherwise idle closed Issue once every
recorded/native PR is positively terminal.
