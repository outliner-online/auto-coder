# Duration-weighted PR test shards

PR Tests uses four contiguous pytest-split groups and the
original test selection and within-group order. Before starting an attempt,
`scripts/run_pr_test_shard.py` validates the reviewed duration snapshot at
`tests/data/pr-test-durations.json`, floors zero durations to one millisecond,
and writes a deterministic normalized copy in the shard diagnostic directory.
Every permitted attempt uses that same copy; a missing, malformed, negative,
non-finite, duplicate, overflowing, or subsequently changed input fails the
shard rather than silently changing its allocation. The snapshot is not updated
by CI and no attempt stores partial timing data into it.

The accompanying provenance file identifies the source run, job, revision,
versions, selector, units, normalization and digest. The current values are
approximations from intervals between completed-test log timestamps, including
intervening fixture and reporting time; they are not pytest's native phase
measurements or a guarantee of future execution time. Pytest-split ignores
removed nodes and uses the mean of matched current nodes for unknown tests, or
an equal default if none match. The positive floor prevents zero-weight boundary
overflow. Updates to the snapshot require review and fresh partition checks.

The shard supervisor retains its existing attempt deadline, bounded progress
extension, idle limit, timeout-only retry policy, process-group cleanup and
failure propagation. Balancing does not change the suite, assertions, markers,
check identities, or workflow timeout. CI output must still be evaluated for its
actual result; estimates and a successful partition check are not a passing run.

Each attempt enables the native pytest timing recorder in an attempt-specific
directory beneath the shard diagnostics artifact. This keeps setup, call and
teardown phase evidence separate across the single timeout-only retry and makes
like-for-like job comparisons possible without changing test outcomes.

The six-shard experiment in PR run
`https://github.com/outliner-online/auto-coder/actions/runs/45101592400` was
inconclusive and was not adopted: it had no identically instrumented four-shard
baseline and three candidate shards exhausted both timeout attempts. The
production matrix therefore remains at four shards until comparable repeated
measurements establish native-work dominance, independence, and an improvement
beyond run-to-run variation.
