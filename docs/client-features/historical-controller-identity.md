# Historical controller identity

Controller protocol evidence remains attributable after a repository changes its
controller GitHub account. Declare the complete set of previous controller logins
in `~/.auto-coder/<owner>/<repo>/config.toml`:

```toml
[github-controller-identity-history]
prior_logins = ["old-controller"]
```

This declaration belongs only to the exact repository and GitHub API origin. It
is not inherited from global configuration or another repository. Each configured
login is resolved to GitHub's canonical login and positive account ID. Duplicate
logins, conflicting account identities, malformed values and a prior account
that is also the current account are rejected. Account IDs establish authorship;
a matching display login cannot override a different observed account ID.

## Absent, empty and unavailable history

| Configuration | Meaning |
| --- | --- |
| Personal controller, no App configuration and no `prior_logins` declaration | Preserve the legacy current-controller-only behavior. |
| Controller-App configuration, no `prior_logins` declaration | Historical controller association is unknown. An empty table without the optional field also leaves it unknown. |
| `prior_logins = []` | The operator declares a complete empty set of previous controllers. |
| Nonempty `prior_logins` list | The operator declares the complete previous-controller set for this repository. |
| A required historical account lookup fails | Historical association is unavailable, not an empty set; the diagnostic identifies the declaration/account to check. |

Current identity is obtained from the credential actually used by the client.
Configuring an App does not relabel a personal-token client's publications. This
feature does not perform the separately staged migration of operational callers
to the controller authentication provider.

Unknown or unavailable history blocks only a relevant exact operational candidate:
a closure receipt for the examined namespace/thread/root, a stale-resolution marker
for that thread, or a receipt with the requested feedback digest. Quoted examples,
other operation namespaces and different feedback digests do not create a blocker.
A fresh operation with no relevant historical candidate remains usable. With a
complete declaration, protocol text from a nonmember remains untrusted discussion.
An unavailable current identity cannot authorize a publication or settlement.

## Preserve historical closure evidence

Closure observation retains the original reply ID, publisher account and login,
operation key and validated head. A prior controller's reply can undergo only the
existing legal transitions for that same operation/head; edit permission does not
allow rebinding it. Otherwise-valid current-controller reply reuse is preserved.
Multiple attributable replies remain ambiguous even if one has the current author.

PENDING and ROLLBACK_REQUIRED receipts and stale BLOCKER/CLEARED chronology survive
migration. Recovery uses the active credential and the original reply. If GitHub
refuses the historical edit, the receipt and rollback/cleanup obligation remain
pending with an identity/permission diagnostic. Recovery does not use an old token,
create a replacement receipt or treat the old effect as absent. Marker cleanup
requires authoritative read-back before removing its local obligation.

Semantic discussion projection excludes the same attributable operational receipt
that recovery reads, so migrating accounts does not manufacture a change in the
substantive review discussion. Historical controller association grants no reviewer
authority and does not alter automated-review eligibility or human-thread rules.

## Feedback recovery and authority changes

Ordinary and adversarial provider-feedback recovery read every page of the native
PR conversation without cache reuse. A successfully read empty conversation is
complete; a failed, malformed, duplicate or incomplete page is unavailable. An
unavailable scan causes zero provider sends. A prior controller's attributable
receipt for the exact requested feedback identity suppresses duplicate delivery.

Each existing receipt/effect critical section pins its resolved author association.
GitHub mutations bind their actual request to the selected credential that passed
the final dispatch check. Provider transports revalidate immediately before native
submission. A material account, App identity or history-declaration change blocks
the affected effect before send. Rotation of a token for the same proven identity
preserves valid authority. The GitHub request governor releases a definitely-unsent
reservation before any bounded credential re-preparation; identity lookup never
waits behind its own active mutation reservation.

An accepted provider effect is retained as `observed_feedback` in the existing
review-repair state before it can be marked delivered. If authority changes before
settlement, no successful delivery is recorded or reported under the mismatched
snapshot. Later reconciliation under valid authority consumes the original accepted
evidence and sends no duplicate instruction. The existing file locks, provider
session ownership, pending reservations and strict receipt read-back remain in use.

## Recovery and diagnostics

When a relevant historical candidate cannot be attributed, verify the repository's
complete `prior_logins` declaration and GitHub access to each account. For a denied
historical-reply update, verify that the active controller can perform that exact
update; leave the retained receipt pending until its original effect can be
reconciled. Do not declare an empty history merely to clear a known old receipt.

Unavailable identity/history, failed conversation reads and refused settlement use
the existing PR processing failure/deferred/blocked paths and loguru console and
rotating-file logging. Diagnostics describe the reason and affected operation; they
do not include credentials or raw unrelated comment payloads. Dashboard event
schemas and provider routing are unchanged; the existing stage/action details show
why recovery remains pending. See `docs/dashboard-observability.md` for the mapped
production regressions.
