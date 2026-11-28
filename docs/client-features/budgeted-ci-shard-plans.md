# Complete time-budgeted CI shard plans

`scripts/plan_ci_shards.py` is an explicit real-collection planner; the
companion `scripts/ci_shard_plugin.py` applies immutable assignments. The
unified `Test Source for Runtime` workflow uses the source-specific native
receipt and Actions aggregate described in `tested-source-references.md` for
pull requests, main pushes and manual runs. It replaces the ordinary four-way
`PR Tests` workflow and its supervised runner.
The source selection excludes only `opencode_live` and retains native retry,
assertion, marker and fixture behavior. The planner alone never authorizes
PASS, and no changed-file omission or outcome reuse is performed.

## Profile context and real collection

Create a `ci-shard-context-v1` JSON file using the actual `git rev-parse HEAD`
of the tested checkout. The unified source workflow checks out `github.sha`
for every job: the simulated merge commit for a PR, the pushed commit for
main, or the selected event revision for a manual run. PRs have no separate
branch-head execution. Tracked working-tree changes are rejected because
`HEAD` would no longer identify all tested bytes; untracked diagnostic
artifacts do not alter `HEAD`.

All arguments in `pytest_args` are **complete and exact**: including targets,
markers, `-k`, configuration options and explicitly enabled other plugins.
The source consumer records and passes
`-m 'not opencode_live' --cov=src/auto_coder --cov-report=` identically to the
planner and every executing shard. Coverage options remain binding semantic
arguments; they are not stripped as reporting-only flags or inherited through
`PYTEST_ADDOPTS`. The following PR example shows the distinct merge and native
run-head identities; the production context generator supplies the full
tracked harness list.

```json
{
  "schema_version": "ci-shard-context-v1",
  "repository": "outliner-online/auto-coder",
  "profile_id": "source-required-v1",
  "checkout_sha": "1111111111111111111111111111111111111111",
  "pytest_args": ["-m", "not opencode_live", "--cov=src/auto_coder", "--cov-report="],
  "harness_files": ["tests/conftest.py", "tests/ci_timing_plugin.py"],
  "semantic_environment": ["AUTO_CODER_REQUIRE_BROWSER", "AUTO_CODER_IMMEDIATE_TEST_FAILURES"],
  "origin": {
    "repository": "outliner-online/auto-coder",
    "workflow": "Test Source for Runtime",
    "run_id": "41246415733",
    "run_attempt": "1",
    "planning_attempt": "source-plan-41246415733-1",
    "event_sha": "1111111111111111111111111111111111111111",
    "event_name": "pull_request",
    "run_head_sha": "2222222222222222222222222222222222222222"
  }
}
```

For current source qualification, `checkout_sha` must equal `origin.event_sha`.
The context also requires `origin.event_name` and `origin.run_head_sha`; the
native Actions event and `head_sha` must match them. A PR's `run_head_sha` is
its branch head, while its tested checkout and event SHA identify the simulated
merge commit. Push and manual events require all three SHA values to match.
The receipt plugin checks the producing job's event name and event SHA along
with its workflow, job, run and attempt. These fields remain optional for
generic planner consumers, but current source proofs cannot omit them or fall
back to the former PR-head interpretation.

The caller explicitly identifies each tracked harness file affecting collection
or execution and every semantic environment variable. The adapter always adds
`PYTEST_ADDOPTS`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD` and `PYTEST_PLUGINS` to the
fingerprint. It also hashes the bytes of `uv.lock` and `pyproject.toml`, the
effective pytest configuration-file path and root directory, exact original pytest arguments,
Python major/minor and the enumerated harness contents. The result is the
profile's `configuration_digest`. Changed source SHA, selectors, configuration,
effective environment or harness refuses plan application; different source
revisions may still use compatible duration history if their configuration
digest is unchanged. Use the digest written into the generated plan as the
configuration identity when producing the optional native duration profile.

```bash
uv run --locked python scripts/plan_ci_shards.py \
  --context source-context.json \
  --duration-profile previous-native-profile.json \
  --duration-policy source-required-v1 \
  --output source-plan.json \
  --log-dir ci-plan-logs -- -m 'not opencode_live' \
  --cov=src/auto_coder --cov-report=

uv run --locked python -m pytest -p scripts.ci_shard_plugin \
  --ci-plan=source-plan.json --ci-shard=1 -m 'not opencode_live' \
  --cov=src/auto_coder --cov-report=
```

The final `--` and subsequent pytest arguments must match `pytest_args` exactly.
Use attached adapter controls (`--ci-context=PATH`, `--ci-collect-output=PATH`,
`--ci-plan=PATH`, `--ci-shard=NUMBER`) rather than separated values. pytest 9
performs initial root/config discovery before registering third-party options;
separated paths can otherwise become unintended collection targets. The adapter
rejects separated controls instead of accepting an altered profile. The actual
bytes of the effective config file, including an untracked `-c pytest.ini`, are
fingerprinted to detect outcome-affecting changes before test execution.
Only adapter controls (`--ci-*`, `--source-ci-receipt`, `--collect-only`) and
explicitly recognized reporting flags (`-q`, `-v`/`-vv`/`-vvv`, `--junitxml`,
`--junit-xml`, `--color`)
are excluded from that semantic argument comparison; other flags stay binding.
If storing context and plan files outside the checkout changes pytest's inferred
root directory, pass the same explicit `--rootdir` in `pytest_args` on both
invocations so node IDs and configuration identity remain stable.
For collection, the planner invokes actual `python -m pytest` with
`--collect-only`, explicitly loads the adapter, and receives versioned JSON
written by the adapter after all standard selectors and collection hooks.
It does not scrape terminal text or read inventory from historical profiles.
Collection failure, duplicates, incorrect checkout/profile/configuration or an
empty required selection refuse publication. Every plan is written atomically
to a **new** file, with exclusive final-name creation and no input overwrite.

## Immutable plan schema and packing

The canonical compact, sorted-key, UTF-8 `ci-shard-plan-v1` JSON has three
top-level parts:

- `semantic`: context (without origin), exact tested checkout SHA, computed
  `profile` (`repository`, `profile_id`, `configuration_digest`,
  `python_major_minor`), ordered complete `inventory` and its SHA-256 digest;
  aligned node `estimates` (`estimated_ms`, `sample_count`, `provenance`),
  `duration_source`, `budget_ms`, `shard_cap`, `effective_capacity_ms`,
  `capacity_limited`, `predicted_total_ms`, and contiguous `shards` (one-based
  `id`, exact `nodeids`, `estimated_ms`, `over_budget_nodes`).
- `digest_sha256`: SHA-256 of the canonical semantic JSON plus one newline.
- `origin`: repository, producing workflow, run, run attempt, planning attempt,
  event SHA, event name and native run-head SHA. These fields are optional
  except repository for generic planner consumers; current source qualification
  requires the complete origin and validates it against the producing attempt.
  Origin is **not** in the semantic digest, so another planning attempt can
  identify the same semantic plan without relabeling its original receipts.

Native `ci-duration-profile-v1` history is used only if the full immutable
snapshot validates and its repository, profile ID, configuration digest and
Python major/minor match. Obsolete nodes are ignored. Under the default general
consumer policy, every unmeasured current node receives a positive estimate of
`max(1 second, maximum P95 of measured current nodes)`; with no measured current
nodes every estimate is 1 second. The source-specific policy below adds its
tracked-snapshot bootstrap and five-second floor.
Fallback provenance is explicitly labeled. Missing, corrupt, stale or foreign
history is diagnostic and **never changes the required inventory**.

Durations are rounded **up** to integer milliseconds, with at least one
millisecond. The ordered inventory is greedily packed into the minimum number
of nonempty contiguous groups whose estimated work fits the 420,000 ms target,
when feasible. If that needs more than 12 groups, the smallest larger
effective capacity fitting 12 is found by integer binary search. Single nodes
over 420 seconds remain indivisible and fully assigned, even for an 820-second
or longer estimate; those nodes are identified separately. Capacity saturation
and predicted shard/test-work totals are visible, and no test is clipped,
omitted or given an artificial deadline.

## Fail-closed application and responsibility

The explicit pytest application mode reads and verifies the entire canonical
plan at configuration time. Before the first test, it confirms actual Git
checkout and fingerprint, complete post-selector inventory, disjoint original-
order assignments, selected shard ID and unchanged plan bytes. It applies only
the plan's exact member set and checks final selected items again immediately
before entering pytest's native test loop. Subsequent changes to the plan file
fail execution; the adapter never replans after seeing results. It prints the
plan digest and selected node IDs diagnostically. The source-specific receipt
plugin separately writes native setup/call/teardown evidence and fails an
incomplete source execution. The plan itself is never success authority.
Native skips, xfails, failures and interruptions retain their pytest semantics.

The source aggregate requires successful quality, plan and shard dependencies
before its current-attempt Actions/receipt verification. It then combines
readable, nonempty coverage databases from every planned shard and uploads the
combined database, HTML, XML and text reports. Missing or invalid coverage,
failed report generation or failed required upload also fails the aggregate;
there is no coverage-percentage threshold. `COVERAGE_FILE` points outside the
worktree and may differ by job without changing the bound instrumentation
arguments. The collection-only database is excluded from execution coverage.
Optional duration history and timing reports cannot authorize source success.
Publication eligibility and historical annotation readback remain the
responsibility of the tested-source consumer described in
`tested-source-references.md`.

The current-run source workflow and native retained Supervisor consumers have
**different** run-attempt evidence and result-gate policies. Both must enforce
their own origin/attempt rules and aggregate the actual native results of every
required shard; a stored plan or timing snapshot alone proves no successful
test. The planner does not schedule runners, install Chromium, guarantee queue
availability, shorten process startup, set test timeouts, or promise a 420-second
wall-clock completion. Estimated work ignores variance, fixture overlap,
worker setup, queue delay, matrix overhead and cancellation behavior.

Console diagnostics use colored loguru; `ci-shard-plan.log` under `--log-dir`
rotates at 10 MB with five retained backups. No GitHub credentials or mutable
duration-history service are needed.

## Source-only tracked-snapshot bootstrap

The complete-source workflow opts in to `--duration-policy source-required-v1`,
passing `tests/data/pr-test-durations.json` together with the tracked provenance
document. Both are optional advisory estimation inputs. The planner checks the
snapshot's exact raw SHA-256, node count, `not opencode_live` selector and
seconds unit, rejects duplicate keys and invalid durations, and accepts only
current-collected node IDs. The source revision recorded by the historical
inter-result snapshot is not a native profile identity and need not equal HEAD.

Estimates use validated exact-node native P95 first, exact-node snapshot duration
second, the largest exact native/snapshot duration of **other current nodes in
the same test file** third, and a minimum five-second source-only floor last
(or a higher explicitly supplied cold-start estimate). Positive durations are
rounded upward to milliseconds; valid zero-second historical measurements get
a conservative positive millisecond. Every estimate records its provenance.
A same-file fallback records the exact source node and origin of its maximum.
Source status and reasons for unavailable/mismatched native or snapshot inputs,
source floor and predicted work are included in the canonical immutable semantic
plan and checked by the plan reader. Malformed optional data degrades prediction
only; malformed authoritative collection or a plan still fails closed.

A source CLI invocation includes the following options before the exact pytest
selector following the separator:

```shell
--duration-policy source-required-v1 \
--bootstrap-duration-snapshot tests/data/pr-test-durations.json \
--bootstrap-duration-provenance tests/data/pr-test-durations.provenance.json
```

Native history remains usable only with its original validated complete
configuration/Python identity, including compatible partial profiles. Neither
source estimate type is a test-selection, cached-outcome or PASS authority.
The existing sixteen-shard contiguous allocation, 420-second target, fixed
current-attempt plan and native receipt aggregate remain unchanged. Estimated
pytest work excludes runner queue, fixture/browser setup, external stalls and
observed critical path. Prior timing improvements are projections until verified
in independent Actions runs. Unselected general and Supervisor callers retain
their existing 1-second and explicit 120-second cold-start policies.
