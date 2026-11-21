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


def _verify_planner(args: argparse.Namespace) -> None:
    """Admit only the plan identified by this dependency's successful producer."""
    if args.planner_result != "success" or not args.planner_artifact or not args.planner_digest:
        raise ValueError("a successful authoritative planner and its exact outputs are required")
    plan, _raw = read_plan(args.verify_plan)
    origin = plan.context.origin
    attempt = str(origin.get("run_attempt", ""))
    current = os.environ["GITHUB_RUN_ATTEMPT"]
    if not attempt.isdecimal() or str(int(attempt)) != attempt or not 1 <= int(attempt) <= int(current):
        raise ValueError("planner attempt is not a valid retained attempt of this run")
    expected = {
        "repository": os.environ["GITHUB_REPOSITORY"],
        "workflow": os.environ["GITHUB_WORKFLOW"],
        "run_id": os.environ["GITHUB_RUN_ID"],
        "event_sha": os.environ["GITHUB_SHA"],
        "ref": os.environ["GITHUB_REF"],
        "planning_attempt": f"python314-plan-{os.environ['GITHUB_RUN_ID']}-{attempt}",
    }
    if any(origin.get(key) != value for key, value in expected.items()):
        raise ValueError("planner origin differs from this workflow run/ref/source")
    if args.planner_artifact != f"python314-plan-{expected['run_id']}-{attempt}" or args.planner_digest != plan.digest:
        raise ValueError("downloaded plan contradicts the successful planner outputs")
    if plan.context.repository != expected["repository"] or plan.context.checkout_sha != args.source_sha or plan.context.profile_id != "python314-advisory-v1" or plan.context.pytest_args != SELECTORS:
        raise ValueError("planner source/profile differs from the required advisory selection")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--verify-plan", type=Path)
    parser.add_argument("--planner-result", default="")
    parser.add_argument("--planner-artifact", default="")
    parser.add_argument("--planner-digest", default="")
    args = parser.parse_args()
    checkout = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if checkout != args.source_sha:
        raise ValueError("actual checkout differs from requested advisory source")
    if args.verify_plan is not None:
        _verify_planner(args)
        return 0
    if args.directory is None or args.github_output is None:
        parser.error("planning requires --directory and --github-output")
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
            "ref": os.environ["GITHUB_REF"],
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
        stream.write(f"artifact=python314-plan-{context.origin['run_id']}-{context.origin['run_attempt']}\n")
        stream.write(f"digest={plan.digest}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
