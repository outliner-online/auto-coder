# Durable ordinary PR capacity waits

An ordinary PR deferred solely by implementation capacity remains unfinished on
its existing SQLite invalidation identity. The worker commits a capacity guard
before relinquishing its claim; a failed write leaves the processing claim owned
and stops that worker. Neither an error-free result nor exit success acknowledges
capacity-deferred work as completed.

The existing capacity observer releases these guards whenever normal capacity is
available, including on startup and after a free-before-retention race. It reads
local capacity and retained identities, not a broad GitHub PR list. While capacity
is unavailable, notifications coalesce without making the waiter claimable or
causing repeated authority reads. Wakes preserve independent retry/stabilization
bounds and enter the ordinary serialized invalidation worker, which refreshes
the current PR and all existing eligibility, CI, review, mergeability and
protection gates. Capacity is scheduling evidence only and may be occupied again
before admission; that PR then waits again without bypassing admission.

`pr.capacity-wait` reports committed retention as `deferred`, guard release as
`completed` with `effect=scheduling-only`, and failed retention as `failed`.
These events never certify implementation, publication or merge. Diagnostic
failures do not change business state. See `tests/test_pr_capacity_waiters.py`.
