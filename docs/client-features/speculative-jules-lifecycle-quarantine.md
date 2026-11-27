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

Positive durable Codex or Claude publication ownership and established local
publication markers may identify an unrelated PR even when the retired ledger is
unavailable. Those signals never override a surviving retired-PR association.
Dependabot identity is accepted only together with its namespaced publication
branch, rather than from bot authorship alone. Missing, empty, or unavailable history remains fenced unless independent evidence
establishes unrelated ownership.
