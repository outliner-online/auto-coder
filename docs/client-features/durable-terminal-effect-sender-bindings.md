# Durable terminal effect sender bindings

`MergeOperationStore.reserve_app_attempt` accepts verified, nonsecret GitHub App
identity evidence and commits it in the same SQLite transaction that grants an
approval or merge attempt. Approval requires the `reviewer` App role; merge
requires the `controller` App role. The binding records the role, API origin,
repository, App and installation IDs, and bot login. Tokens, JWTs, and private
keys are not accepted or stored. Principal comparison normalizes the API origin's
trailing slash and repository and bot-login casing before persistence.

A bound unfinished effect can continue only under the same principal, including
same-head metadata refresh, credential renewal and process restart. A different
principal receives a typed refusal without changing lifecycle state, receipts,
retry accounting, or the original binding. The unbound `reserve_attempt` path
remains available for the existing personal-controller flow; it cannot reserve
an already App-bound effect. All store mutations serialize their eligibility
reads and writes across controller processes, so a simultaneous metadata refresh
cannot overwrite a granted reservation.

Each reservation creates a durable generation-and-attempt-correlated record
with its exact target head. Its sender and target remain immutable. When a new
head creates a generation, the prior record retains its sender and any later
correlated receipt. A late result updates only that historical attempt and
cannot settle or alter the current generation, its deadline or its retry budget.
A later uncertain or negative result cannot erase retained positive evidence.

Exact-target `STATE_OBSERVATION` receipts require a merge commit for a merge, or
a review ID and observed actor for an approval. App-bound own-response receipts
have the same target and evidence requirements. Invalid, incomplete or
uncorrelated positive evidence leaves the unresolved effect unchanged. Receipt
actors remain observation evidence and are not treated as request senders;
positive reconciliation can therefore settle an unknown legacy sender without
inventing an App identity. The existing compact personal own-response receipt
contract remains compatible.

Store initialization transactionally adds the binding and historical attempt
schema and backfills available legacy effect, receipt and attempt evidence.
Existing lifecycle state, deadlines and throttle accounting remain unchanged.
Historical senders remain explicitly unknown. Historical heads that cannot be
reconstructed from the original generation or retained receipt also stay
unknown; a later result cannot borrow the current generation's target.
Interrupted effects pass through the existing restart/resumption hooks as
`DELIVERY_UNKNOWN`, and cannot acquire their first App binding or be replayed
while possible delivery remains unresolved. Confirmed and `NOT_NEEDED` effects
are never relabeled or replayed.

Run the bounded state, adapter, scheduler and repository-resumption regressions
with `bash scripts/test.sh tests/test_merge_operation_state.py
 tests/test_merge_operation_adapter.py tests/test_merge_operation_scheduler.py
 tests/test_merge_operation_resumption_isolation.py`.
