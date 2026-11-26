# OpenCode live test scheduling

The dedicated `OpenCode Live Tests` check runs the complete `opencode_live`
marker selection through `bash scripts/test.sh --ci-tests-only`. Its existing
three pytest workers share one production image prepared from the exact checkout.
The load scheduler uses `--maxschedchunk 1` to reduce reserved work behind long
tests. Xdist still retains its required current/next item lookahead; the option
does not guarantee that only one item is assigned to a worker.

Marker discovery, parameterized profiles, the fail-closed container-scenario
validator, native pytest exit codes, existing host retry/skip policy, pinned
Python/uv/Node/OpenCode/Codex prerequisites, and job/step timeouts are preserved.
There are no additional jobs or image builds, so the third worker does not
duplicate dependency installation or runtime-image preparation. The explicit test-only script mode
matches the previous workflow's direct pytest invocation, which also did not run
the script's quality provisioning stage. Ordinary local script calls retain their
complete gate.

Native setup/call/teardown durations are printed with `--durations=0`. The existing
always-uploaded `opencode-live-logs/` directory also contains `junit.xml`, alongside
image identity and fail-closed selection/results diagnostics. `-ra` exposes the
existing retry-related skip reasons without adding retries or changing outcomes.
Repository tests exercise the actual workflow command, script, collector, xdist
workers, and validator with a controlled fixture: every selected parameter/profile
and newly marked case executes exactly once, ordinary tests remain excluded, and
an actual assertion failure outside the mandatory container scenarios returns 1
even when completeness validation succeeds.

## Evidence and remaining measurement

Issue #2911's qualifying run `45330611896`, job `138230761660`, used one runner
and two workers: queue 2 seconds, job 696 seconds, image preparation 63 seconds,
test step 607 seconds, and native pytest 602.92 seconds (18 passed, 9 skipped).
The native duration report identifies a 236.62-second longest indivisible test.
One worker remained occupied by that test and its following 85.66-second case,
while the other processed the remaining independent cases; both workers were
active until the end of the session. The independent call durations total much
more than the longest indivisible test, so adding one in-job worker can
redistribute measured work while retaining the single prepared image. It does
not alter queueing, setup, retry, selection, or failure semantics. A comparable
after measurement must come from the changed GitHub Actions job; this source
change does not represent an estimated schedule as measured latency.

Issue #2871's cited run `44867758076`, job `136735160868`, used one runner and two
workers: queue 2 seconds, job 750 seconds, image preparation 79 seconds, test step
646 seconds, native pytest 643.02 seconds (18 passed, 9 skipped). The test step
includes existing retries; those 643.02 seconds are session elapsed time, not a sum
of native call phases. The log shows Codex cases preceding the long container
work and a late container authentication case. This motivates reducing reserved
chunks rather than duplicating image preparation across new jobs.

A local controlled 27-case sleep workload with unequal durations and two workers
ran through `scripts/test.sh --ci-tests-only`: default load chunking took 32.83
seconds (pytest 30.73), bounded chunks took 30.43 seconds (pytest 28.74), and work
stealing took 35.77 seconds (pytest 34.97). Work stealing was rejected. These
are actual fixture invocation timings, **not live-test or CI latency measurements**.
The fixture uses scaled approximate workload weights, not measured native phase
durations. It performs no image setup or CLI work and has no Actions queue.
Local Python was 3.12.14, uv 0.12.19, and pytest-xdist 3.8.0; the cited CI used
Python 3.12.16 and pinned uv 0.7.22. The local host was Debian 13 rather than
the cited runner's Ubuntu 26.04. An isolated repeat after regression execution
finished took 32.38 seconds (pytest 30.74) with default chunks and 29.30 seconds
(pytest 28.64) with bounded chunks; both passed all 27 controlled cases.
Full marker collection through the supported script selected 27 live nodes,
with exactly the same node IDs as the cited CI log (no missing or added nodes).

The local unchanged production-image preparation failed after 15.449 seconds
because the Docker build could not reach Debian package repositories. The pinned
host OpenCode install also failed with an incompatible platform-package selection.
No live baseline or after run is therefore available. Representative before/after
job and step timings, native phase/retry attribution, and a passing complete live
run remain required before claiming the Issue's latency acceptance criterion.
No Actions run was dispatched for this investigation.

This changes test orchestration only. Production processing origins, admission
gates, provider routing, structured trace events, and dashboard projections are
unchanged, so no dashboard observability update is required.
