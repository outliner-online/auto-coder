# Durable terminal effect sender bindings

`MergeOperationStore.reserve_app_attempt` accepts verified, nonsecret GitHub App
identity evidence and commits it in the same SQLite transaction that grants an
approval or merge attempt. The binding records the App role, API origin,
repository, App and installation IDs, and bot login. Tokens, JWTs, and private
keys are not accepted or stored.

A bound unfinished effect can continue only under the same principal, including
after credential renewal or process restart. A different principal receives a
typed refusal without changing lifecycle state, receipts, retry accounting, or
the original binding. The unbound `reserve_attempt` path remains available for
the existing personal-controller flow.

Each App reservation also creates an immutable generation-and-attempt-correlated
record. When a new head creates a generation, the prior record retains its
sender and any later correlated receipt. A late result updates only that exact
historical attempt and cannot settle or alter the current generation. Receipt
actors remain observation evidence and are not treated as request senders.

Store initialization transactionally adds the binding column and historical
attempt table to the pre-binding schema. Existing effects and receipts retain
their lifecycle and are represented with an explicitly unknown sender; no App
identity is inferred from reviewer configuration or receipt actors.
