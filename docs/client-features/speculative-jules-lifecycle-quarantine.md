# Retired speculative Jules artifact fence

Pull requests durably associated with a historical Jules competition are classified
before ordinary PR effects and again at the final merge mutation boundary. Retired,
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
history fenced after later store loss. When no historical storage survives, or
storage cannot be opened during startup, the read-only fence remains active without
creating replacement databases: independently verified unrelated ownership retains
ordinary gates, while unresolved ownership is explicitly `UNKNOWN`.
