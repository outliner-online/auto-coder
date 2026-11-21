# PR lifecycle directives and fresh draft admission

Issue ownership inferred from a PR body requires a closing directive at the
start of a line, Markdown list item/heading, or sentence/clause. All existing
closing verb forms remain supported, including past-tense forms, optional
colon and same-repository qualifiers. `This PR fixes #123` and successive
`Fixes #123, closes #456` clauses remain directives. Narrative such as
`Grid: existing closed #5459/REQ-007–008 ... No Issue Requirements or ownership
were rewritten` supplies context, not implementation ownership. Requirement
citations such as `Closed #5459/REQ-007–008` are not plain Issue directives.
Issue numbers must be complete tokens: `Closes #5459example` and
`Closes owner/repo#5459example` do not establish ownership. Whitespace,
end of text and punctuation remain valid delimiters.
CommonMark block and inline tokens exclude quoted Markdown, fenced/indented
code and HTML from directive extraction. This includes tab indentation,
list-contained fences and fence-like content with a non-whitespace suffix;
that suffix cannot close a fence. Parser-recognized inline source spans are
masked without rendering or unescaping text into a new directive. Excluded
regions cannot bridge a closing keyword to an Issue token elsewhere.
`markdown-it-py` is an explicit dependency, using the already locked package. Genuine directives following those
regions remain eligible, including every supported closing verb. Broad review
context extraction is unchanged. Issue-bearing branches, native connections,
and recorded provider or PR memberships retain their existing authority.

Startup discovery uses the same restricted parser before adding open PRs to
an active owner's durable membership. It must not learn an incidental
association and then use that association to block terminal reconciliation.
Closed Issues can still retain legitimately live implementation siblings;
current Issue state alone is not a reason to discard PR ownership.

At the serialized creation of a fresh autonomous draft reservation, the
controller checks whether a local repair backend is configured. The store
checks owner absence and commits admission under the same lock used by
reconciliation, including admission after capacity reconciliation. If fresh
evidence is not prepared, it unwinds that store lock, performs the strict
GitHub/provider reads, and re-enters admission with the prepared result. The
callback inside the lock only checks that result; it performs no network or
provider lookup. Preparation unwinds before existing hierarchy reads so those
reads are not repeated just to prepare a draft. The existing deployment
registry dispatch-authority guard still encloses admission preparation; this
change removes I/O from the slot-store mutex, not that separate guard. The
strict PR fetch uses its existing 30-second per-request transport timeout. An earlier active-owner snapshot cannot waive fresh admission
after retirement. Retained live execution keeps its normal duplicate guard.
With no local backend configured, preparation reads current strict GitHub PR
metadata matching the observed head reference and SHA, and uses the cloud-origin
resolver, constrained
to lifecycle-validated Issue ownership and independently recorded sessions
matched by explicit PR session markers, rather than broad review-context prose.
Missing-slot Jules/Claude session recovery remains eligible; conflicting
recorded session associations defer admission. A confirmed
draft without a usable provider origin is deferred before execution admission,
so it cannot consume capacity as either an Issue or standalone PR owner merely
to return a backend refusal. Unavailable or ambiguous evidence also defers
without reserving. Configured local routes, provider routes, a PR freshly found
ready for review, explicit manual processing, and already retained owners
continue through their ordinary gates.

An autonomous draft's execution carries its lifecycle-derived Issue scope and
head into repair routing. When preflight selected a provider, its exact
provider/task and publication-attribution token are also bound through repair.
The final send gate freshly reconfirms the head and selected origin. Changed or
unavailable evidence defers without sending to a different task. Review and
conflict repair cannot regain a broad body-reference fallback inside this
scope; informational Issue/review context extraction remains unchanged.

This does not start a provider merely to establish evidence, change
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
