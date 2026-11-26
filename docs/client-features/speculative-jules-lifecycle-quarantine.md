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
