# Retired stale-Jules automatic retries

Jules PR and no-PR staleness observations no longer create automatic retry
grants, increment Issue attempts, requeue Issues, or submit replacement provider
work. A stale Jules PR may still be closed, but its closure message explicitly
states that no replacement attempt will be started.

Persisted `stale_jules` origins and `stale-jules-recovery-*` request rows remain
readable for historical attribution. Retry admission returns a retired/deferred
outcome for that origin without acquiring implementation ownership or changing
the stored request. Explicit operator retries remain distinct and continue
through the existing manual admission, generation, incarnation, idempotency,
and capacity gates.
