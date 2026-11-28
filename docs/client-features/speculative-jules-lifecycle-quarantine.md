# Retired speculative Jules artifact fence

Pull requests durably associated with a historical Jules competition are classified
before ordinary PR effects and again at the final merge mutation boundary.
Candidate collection and reserved dispatch apply the same fence before draft
readiness, empty-PR closure, stale-PR closure, or retry side effects. Their existing
strict branch-safety read runs before classification so a reduced or stale snapshot
cannot authorize effects against a different fresh identity. Withheld PRs remain
open for linked-Issue exclusion; withholding them does not launch a replacement. Retired,
unselected, uncertain, and conflicting artifacts are fenced without cleanup. Only a
positively selected artifact may proceed, and its durable selection and exact current
PR/Issue identity are revalidated at the merge boundary before ordinary gates apply.

Classification uses the persisted competition ledger, retained observations, and
strict GitHub PR identity reads. It never performs Jules session GET/list requests.
Historical cleanup and delivery obligations remain inspectable but are inert: daemon
restart does not close loser PRs, refresh candidates, adopt winners, or enqueue failed
generations.

A ledger binding remains a fence if its separate observation is missing. An
unreadable namespace also prevents a selected association in another namespace
from granting authority. An unrecognized bot author alone does not establish
that a PR is unrelated to an incomplete competition history.

Positive durable Codex publication ownership, a Claude session matching a
linked Issue binding, and established local publication markers may identify an unrelated PR even when the retired ledger is
unavailable. Those signals never override a surviving retired-PR association.
Unavailable, empty, conflicting, or incomplete historical evidence is reported as
`UNKNOWN` at ingress and final merge, distinct from a known selected candidate
whose durable acceptance is stale; both states remain mutation-ineligible.
Dependabot identity is accepted only together with its namespaced publication
branch, rather than from bot authorship alone. When either historical store is
present, startup migrates pre-marker installations by recording a durable history
marker outside SQLite. The marker keeps missing, empty, or unavailable established
history fenced after later store loss. If every pre-marker store was lost before
migration, the fence reports UNKNOWN unless independent non-Jules publication evidence
establishes unrelated ownership. Installation freshness does not establish PR
origin: a newly installed worker can encounter an existing repository's historical
candidates. Installation receipts and environment identifiers therefore cannot
bypass this PR-specific fence. An unattributed PR remains UNKNOWN even on a new
worker; positively attributed ordinary PRs retain their existing gates. The
no-history fallback does not create replacement historical databases. If an established
observation store cannot be opened, startup retains the readable ledger as
authority: exact bindings remain fenced while independently attributed unrelated
PRs keep their ordinary gates.

Local publication origin uses the marker emitted by the ordinary local PR creator.
For a bot-authored local publication, the current controller identity must also match
the author login and account ID. An unrelated bot cannot use the marker as proof.
A retained local repair job is not original PR provenance, and Issue finalization
receipts alone are insufficient: the finalizer can associate an already-existing
PR as well as create a new one. Those receipts do not bypass historical ownership.
