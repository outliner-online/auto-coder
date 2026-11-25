# Source test duration estimates

`Test Source for Runtime` plans its complete current pytest selection with
`scripts/plan_ci_shards.py` and the `source-required-v1` duration policy. The
immutable plan uses a 420-second estimated-work target and a twelve-shard cap.
The retired fixed-four PR runner is no longer an execution path. The retained
`tests/data/pr-test-durations.json` snapshot and its
`tests/data/pr-test-durations.provenance.json` companion supply optional bootstrap
estimates and a diagnostic comparison with the former assignment strategy.

## Advisory bootstrap estimates

The planner collects the current source tests with `-m 'not opencode_live'` and
assigns every collected node exactly once. Duration history cannot add, remove,
or skip tests. For a current node, a matching native p95 measurement takes
precedence over an exact-node entry in the tracked snapshot. A node with neither
uses the largest known estimate from its current test file; a file with no known
estimate uses the source floor, five seconds with the workflow's default
configuration.

Before using the snapshot, the planner verifies the SHA-256 digest, selector,
units and node count against its provenance, and checks that durations are
finite and nonnegative. Zero values receive a one-millisecond weight. A missing
or invalid snapshot is reported as unavailable and ignored; planning continues
with native history and the source fallback estimates. The plan records the
estimate provenance and snapshot status so the allocation can be inspected.

The tracked values approximate intervals between completed-test log timestamps,
including intervening fixture and reporting time. They are not native pytest
setup/call/teardown durations or a guarantee of future execution time. The
companion file records their source run, job, revision, versions, selector,
units, normalization and digest. CI does not rewrite these tracked files;
reviewed updates must keep the snapshot and provenance consistent. New native
phase measurements use the source workflow's separate optional duration cache.

## Comparison with the former four-way assignment

`scripts/source_ci_runtime.py report` uses
`src/auto_coder/source_ci_metrics.py` to reconstruct the former four-way
assignments on the same current inventory. When complete current-attempt receipts
are verified, the report compares both assignments using those current native
pytest phase measurements. This is an assignment comparison, not a second
execution of the retired workflow or a measured before/after speedup. Queue,
setup and actual workflow durations are reported separately when available.

Neither bootstrap estimates nor a successful comparison qualify source tests.
The required result comes from the complete planned execution and verified
current-attempt evidence, as described in
[tested source references](tested-source-references.md).
