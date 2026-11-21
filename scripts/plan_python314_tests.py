"""Bind the advisory profile to the existing complete native CI shard planner."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from scripts.plan_ci_shards import main as plan_main

from auto_coder.ci_shard_plan import ShardContext, read_plan

SELECTORS = ("-m", "not opencode_live")
HARNESS = (
    "tests/conftest.py",
    "tests/ci_timing_plugin.py",
    "tests/pytest_fault_diagnostics.py",
    "scripts/ci_shard_plugin.py",
    "scripts/plan_ci_shards.py",
    "scripts/plan_python314_tests.py",
    "src/auto_coder/ci_shard_plan.py",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()
    checkout = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if checkout != args.source_sha:
        raise ValueError("actual checkout differs from requested advisory source")
    repository = os.environ["GITHUB_REPOSITORY"]
    context = ShardContext(
        repository,
        "python314-advisory-v1",
        checkout,
        SELECTORS,
        HARNESS,
        ("AUTO_CODER_REQUIRE_BROWSER", "AUTO_CODER_IMMEDIATE_TEST_FAILURES"),
        {
            "repository": repository,
            "workflow": os.environ["GITHUB_WORKFLOW"],
            "run_id": os.environ["GITHUB_RUN_ID"],
            "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
            "planning_attempt": f"python314-plan-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}",
            "event_sha": os.environ["GITHUB_SHA"],
        },
    )
    args.directory.mkdir(parents=True, exist_ok=True)
    context_path = args.directory / "context.json"
    with context_path.open("x", encoding="utf-8") as stream:
        json.dump(context.json(), stream)
    plan_path = args.directory / "plan.json"
    result = plan_main(
        [
            "--context",
            str(context_path),
            "--duration-policy",
            "source-required-v1",
            "--bootstrap-duration-snapshot",
            "tests/data/pr-test-durations.json",
            "--bootstrap-duration-provenance",
            "tests/data/pr-test-durations.provenance.json",
            "--output",
            str(plan_path),
            "--log-dir",
            str(args.directory / "logs"),
            "--",
            *SELECTORS,
        ]
    )
    if result:
        return result
    plan, _raw = read_plan(plan_path)
    matrix = {"shard": list(range(1, len(plan.shards) + 1))}
    with args.github_output.open("a", encoding="utf-8") as stream:
        stream.write(f"matrix={json.dumps(matrix, separators=(',', ':'))}\n")
        stream.write(f"count={len(plan.shards)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
