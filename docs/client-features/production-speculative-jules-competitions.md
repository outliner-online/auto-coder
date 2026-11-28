# Retired speculative Jules competitions

Auto-Coder no longer creates, resumes, observes, selects, adopts, or cleans up
speculative Jules competition candidates. The historical competition ledger and
retained candidate observations remain read-only provenance for old pull requests.

Production PR ingress and the final merge sender consult that provenance. Retired
or unselected candidates are refused, and selected candidates must still match the
durable selected identity and current Issue/PR identity before the ordinary CI,
review, and merge gates apply. Missing or conflicting provenance fails closed for
a possibly Jules-owned PR, without making unrelated PRs depend on Jules transport.

Restart maintenance sends no Jules requests, adoption notifications, aggregate
failure notifications, or loser-close mutations. Existing records and pending
cleanup rows remain inspectable rather than being deleted.
