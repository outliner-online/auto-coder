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
history fenced after later store loss. If every pre-marker store was lost before
migration, the fence reports UNKNOWN unless independent non-Jules publication evidence
establishes unrelated ownership. A deployment-owned
`state/retired_jules_installation_provenance.json` receipt may identify a genuinely
fresh installation only with schema `retired-jules-installation-provenance-v1`, state
`confirmed-fresh`, and an installation identity that exactly matches the independently
provided `AUTO_CODER_INSTALLATION_ID` for the current deployment. Missing, malformed,
historical, foreign, or conflicting provenance is not fresh evidence and remains
fenced. When no receipt exists, ambiguous ownership remains UNKNOWN; independently
established unrelated ownership can still follow ordinary gates.
Deployments must create the receipt as part of independently verified fresh installation
and provide the same identity to the runtime; the lifecycle never creates or upgrades
it. This fallback is read-only and does not create empty history on a confirmed fresh
installation. If an established
observation store cannot be opened, startup retains the readable ledger as
authority: exact bindings remain fenced while independently attributed unrelated
PRs keep their ordinary gates.
