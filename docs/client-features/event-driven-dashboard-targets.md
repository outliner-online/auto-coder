# Recorded open targets in the dashboard

The dashboard uses advisory observations from the worker's existing complete
startup enumeration and successful strict target reads. Startup therefore shows
known Issue/PR identities immediately, even before the worker has obtained their
titles. Later strict open observations supply titles; strict closed and 404
observations remove that target. Author allowlists and admission decisions do not
hide observed targets or grant execution from this view. No dashboard refresh
causes a GitHub request, and no discovery timer is added.

The status projection includes `open_target_observation` with enumeration
availability, its successful observation timestamp and diagnostic source. Before
a successful complete enumeration, the UI reports unavailable enumeration rather
than an empty repository. Failed enumeration retains prior observations with a
stale indication. Complete empty observations are distinct. The list is recorded
evidence, not a live repository listing; individual transport or malformed reads
do not erase targets. A strict observation made during an enumeration wins over
that older enumeration, including a closure/404 tombstone.

Legacy polling snapshots remain a fallback for legacy callers while complete
observations are unavailable. They do not manufacture complete-enumeration
availability. This in-memory view is reset on restart and never drives durable
invalidation completion, implementation ownership, validation or merge authority.
The public diagnostics API allowlist is unchanged.

A typed remote failure during startup ownership recovery remains incomplete even
when the dashboard is reachable. The existing pending-work status shows an
`evaluation_failed` operational block with a zero deadline; the startup warning
and `pending-work list` provide a sanitized classification/status and selected
retry guidance. Neither a generic wake nor elapsed time starts ordinary workers
or releases an owner. Real typed throttles retain the existing bounded retry
schedule. The lifecycle controls and corrected single-obligation recovery are
covered by `tests/test_startup_pending_remote_errors.py`.
Restart preserves the same startup block or future deadline instead of silently
trying again inline. A scheduler-completed startup is not enumerated again if
its row is retired while the startup reader is in flight. Available fresh startup,
due recovery and explicit selected retry still reach ordinary serving state.

Run `pytest -q tests/test_open_target_observations.py tests/test_entity_invalidation.py`
for production reconciliation/strict observation/status/presentation regressions.
The initial two regression cases fail against the previous engine: one returns
an empty set after successful nonempty enumeration, and the other lacks an
availability observation. The UI calls the tested `open_targets_notice` projection.
