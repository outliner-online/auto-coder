# scripts/test.sh enforces the package-wide mypy gate

Both root development dependency declarations require `mypy>=2.4.0`, and
`uv.lock` resolves mypy 2.4.0. Provision the committed development toolchain
with `uv sync --locked --extra dev`; `uv run --locked mypy --version`
reports the checker selected by that lockfile.

`scripts/test.sh` runs `mypy --config-file pyproject.toml -p auto_coder` --
the same command, package scope, and root `pyproject.toml` `[tool.mypy]`
configuration as the `Test Source for Runtime` "Source identity and quality" job and the `mypy`
pre-commit hook -- in place of the previous import-only probe that was
wrapped in `|| true` and could never fail the script. A nonzero result (or
an unavailable checker, missing/unreadable explicit configuration, or failed
tool provisioning) now stops the script under `set -Eeuo pipefail` before
the "all checks passed" message and before `local_test_log_collector.py` or
pytest ever runs, on both the `uv` and system-Python runner paths and in
both CI and local execution. The Black/isort/Flake8/mypy quality-stage order,
CI check-only vs. local auto-fix formatting behavior, and the collector's
exactly-once invocation with the original argument vector and exit status
are unchanged.

The only quality-free path is the explicit leading `--ci-tests-only` mode, used
by the dedicated OpenCode live workflow; the independent unified source workflow
owns the shared quality gate. It is also available to bounded test invocations
with a separate quality check. The unified
source shards invoke pytest directly after an immutable plan; their aggregate
also requires the source-quality job. The script consumes that option before collector argument forwarding;
setting `CI` or `GITHUB_ACTIONS` alone does not enable it. Container forwarding
preserves the explicit mode and the original test arguments. Direct invocations
retain the complete quality gate by default.

The GitHub cache bridge explicitly types its request-extension payload as
`dict[str, object]`, matching its context variable under mypy 2.4.0's
literal-key inference. It still forwards only present `timeout` and
`auto_coder_operation_id` entries with their original values and restores
the previous context after either successful or failed delegation.

Container forwarding (`AM_I_AUTOCODER_CONTAINER=true` with an explicit,
nonempty `REPO_NAME` and no true `INSIDE_TARGET_EXECUTION`) still redirects
to `auto-coder-<repo>` and now also propagates a target checkout's mypy
failure to the outer caller, since the forwarded script is this same,
now-enforcing `scripts/test.sh`. The retired PR-only shard supervisor is no
longer used. A completed mypy failure remains a failed quality gate. This is
test/CI infrastructure only:
production processing, structured trace events, and dashboard projections
are unchanged.
