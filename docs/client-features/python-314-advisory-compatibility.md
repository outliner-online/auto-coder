# Advisory Python 3.14 compatibility evidence

The independent `Python 3.14 Compatibility (Advisory)` GitHub Actions
workflow runs for pull requests changing its workflow or shared dependency
policy inputs, manual dispatch and a weekly check. Ordinary application-only
pull requests do not incur this separate, potentially expensive advisory job. It
uses the same supported `astral-sh/setup-uv@v7` and pinned uv `0.7.22`
as the required Python 3.12 workflows. Its `uv sync --locked --dev --extra
test` installs the committed dependency graph without regenerating the
lockfile. Stale or incompatible dependencies, missing Python 3.14 wheels,
source-quality failures, and test failures remain visible red job outcomes.
A failed or incomplete run is not compatibility evidence.

The workflow sets `UV_PYTHON=3.14` for uv sync and every uv run so the
repository's default interpreter pin cannot silently create a Python 3.12
virtual environment even after `setup-python` selects 3.14. A separate
version assertion refuses an unexpected interpreter. The workflow verifies
the selected interpreter and clean exact Git checkout,
then runs Black, isort, Flake8 and package-wide mypy before planning the complete
`not opencode_live` pytest selection. Like the required source workflow, each test shard
prepares the Python-only management Docker fixture from the tested source and
verifies real headless Chromium before running the tests. These prerequisites
are required to avoid incorrectly reporting success with eligible tests
deselected or skipped for missing fixtures; they do not run OpenCode live
tasks. The fixture's isolated container Python is not the interpreter under
evaluation: Python 3.14 is selected for the installed application and host
pytest process.

This independent workflow is **advisory**, not a publication, merge or
deployment authority. It publishes only diagnostic CI artifacts, grants no package write
permission and does not alter any required Python 3.12 workflow, Docker
base image, production worker, release selection or branch-protection rule.
Green advisory evidence alone is not approval to migrate the production
baseline. See `immutable-ci-uv-setup.md` for the shared immutable-lock gate.

The committed graph preserves Aider 0.86.2's NumPy 1.26.4 and SciPy 1.15.3
on Python below 3.14. On Python 3.14 and later, uv dependency overrides use
Aider upstream's `numpy>=2.3,<2.5` and `scipy>=1.16.1,<1.18` ranges
([upstream requirements](https://github.com/Aider-AI/aider/blob/main/requirements.txt)).
The released Aider metadata still pins numerical releases without Python 3.14
wheels, so merely installing OpenBLAS would leave unsupported NumPy source
builds. These overrides retain both dependencies and lock their compatible
wheels; the compatibility workflow still admits the unchanged committed lock.
`audioop-lts` is installed on Python 3.13 and later because Aider's pydub
import requires the removed standard-library `audioop` module. Real backend
import and PageRank regression tests ensure these dependencies execute, rather
than accepting AiderClient's caught ImportError as evidence of availability.
Production Python 3.12 interpreter selection and numerical versions remain
unchanged. This dependency repair changes no production trace schema or
processing behavior, so no dashboard observability update is required.

The advisory workflow also installs a real Python 3.12 interpreter before
selecting Python 3.14 for the application and pytest host. Worker and management
staging integration fixtures select that separate production interpreter;
they still execute the real installers and all original verification checks.
The installer passes its configured interpreter explicitly to both uv venv and
uv sync, so an inherited advisory `UV_PYTHON=3.14` cannot replace the required
3.12 fixture runtime. Candidate bytecode is compiled with the candidate
interpreter so cache-tampering verification remains meaningful across host
Python versions. Missing production Python is a test failure, not a skip.

Chromium is cached under the runner's temporary directory, outside the source
checkout, so later native fixture builds still verify a clean exact revision.
The complete advisory selection retains coverage context validation using the
observer's context-capable tracing engine and explicit SQLite connection
closure at native bootstrap-store boundaries. Publisher policy unit tests
exercise both Python 3.12 acceptance and Python 3.14 rejection: a successful
advisory run does not grant Python 3.14 production publication authority.

The quality/planning job invokes `scripts/plan_python314_tests.py`, which binds
the exact checkout, Python interpreter, selectors, harness and semantic environment
to the existing `scripts/plan_ci_shards.py` planner. Its separate
`python314-advisory-v1` profile cannot consume Python 3.12 native history.
The source estimator's validated tracked snapshot is an optional, explicitly
non-native bootstrap; missing entries retain its conservative same-file/five-second
fallback. These are scheduling estimates, never measured Python 3.14 execution
times. The existing 420-second estimated-work target, twelve-shard cap and
indivisible-node policy apply. Each matrix member recollects the full native
selection and applies only its immutable assignment via `scripts/ci_shard_plugin.py`.
Browser, Muse and Supervisor tests remain selected; only OpenCode live tests are
excluded. Native failures keep their exit status, with no whole-suite retries or
fail-fast matrix cancellation. Prerequisite preparation is duplicated on every
test runner; quality runs once before the matrix.

The original `Python 3.14 source quality and non-live tests (advisory)` check name
now aggregates planning/quality and every native shard. Failed, cancelled, skipped
or missing dependencies fail it. This changes no branch protection or merge gate.
Current-run-and-attempt plan artifacts retain the full inventory and assignments;
each shard retains JUnit and the existing opt-in native timing diagnostics even
after a failed test command. No outer coverage invocation existed in this profile;
the native selection still executes its coverage-context regressions unchanged.

For comparison, record the actual checked-out SHA separately from event SHA,
locked dependency hashes, exact Python/uv and runner image versions, cache state,
runner count, queue seconds, quality/planning/setup steps, every test-command step,
and native setup/call/teardown times. Compare complete successful same-source
attempts with identical instrumentation and prerequisites. Use maximum shard job
elapsed time and aggregate completion latency separately, accounting for the
serial planning job, duplicated collection and browser/fixture setup, and available
concurrency. Repeat/interleave when possible and report variation or inconclusive
results. Historical run 44853039779 attempt 1 used Python 3.14.10 on Ubuntu 26.04:
4,910 job seconds, 3 queue seconds, 4,706 test-command seconds, and 4,695.26 seconds
for its single native pytest session (13,898 passed, 137 skipped, 27 deselected).
Its collection occupied about 38 seconds; no outer retry was observed. Progress
timestamps locate expensive independent management fixtures, but are not native
phase measurements. A new matrix or estimated work total alone proves no measured
end-to-end improvement. Publication and comparable Actions runs are separate
evidence still needed before claiming a runtime reduction.

Verify orchestration locally with
`CI=true bash scripts/test.sh tests/test_ci_uv_lock_policy.py tests/test_python314_orchestration.py tests/test_ci_shard_plan.py`.
The orchestration regression executes the workflow's actual planner and native
shard command with complete parameterized non-live profiles, then exercises the
actual aggregate shell gate. It proves exact single assignment, unchanged passing
outcomes, retained JUnit, and propagation of a genuine native assertion failure.
This workflow-only scheduling change emits no production dashboard events and
requires no dashboard observability schema change.
