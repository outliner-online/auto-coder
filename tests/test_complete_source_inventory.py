"""Production-profile protection for the complete source native inventory."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from auto_coder.ci_shard_plan import read_plan

pytestmark = pytest.mark.usefixtures("_use_real_commands")

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests/data/complete-source-baseline-nodeids.txt.gz"
PROVENANCE = ROOT / "tests/data/complete-source-baseline-nodeids.provenance.json"
THIS_NODE = "tests/test_complete_source_inventory.py::test_production_complete_source_inventory_preserves_baseline_exactly_once"


def _baseline() -> tuple[str, ...]:
    raw = gzip.decompress(BASELINE.read_bytes())
    provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    nodes = tuple(raw.decode("utf-8").splitlines())
    assert provenance == {
        "inventory_sha256": hashlib.sha256(raw).hexdigest(),
        "nodes": len(nodes),
        "profile_id": "source-required-v1",
        "python_major_minor": "3.12",
        "schema_version": "complete-source-baseline-v1",
        "selector": "not opencode_live",
        "source_sha": "0924a591da911c91633612136a7e626d98bdba7d",
    }
    assert len(nodes) == len(set(nodes))
    return nodes


def _require_exact_baseline(candidate: tuple[str, ...]) -> None:
    expected = (*_baseline(), THIS_NODE)
    assert Counter(candidate) == Counter(expected)


def test_production_complete_source_inventory_preserves_baseline_exactly_once(tmp_path: Path) -> None:
    """Run the workflow planner and reject omission of any immutable baseline node."""
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    context = tmp_path / "context.json"
    plan_path = tmp_path / "plan.json"
    environment = {**os.environ, "AUTO_CODER_REQUIRE_BROWSER": "1", "AUTO_CODER_IMMEDIATE_TEST_FAILURES": "1"}
    context_result = subprocess.run(
        [
            sys.executable,
            "scripts/source_ci_runtime.py",
            "--log-dir",
            str(tmp_path / "context-logs"),
            "context",
            "--repository",
            "outliner-online/auto-coder",
            "--source-sha",
            sha,
            "--event-sha",
            sha,
            "--run-id",
            "1",
            "--run-attempt",
            "1",
            "--output",
            str(context),
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert context_result.returncode == 0, context_result.stdout + context_result.stderr
    planned = subprocess.run(
        [
            sys.executable,
            "scripts/plan_ci_shards.py",
            "--context",
            str(context),
            "--duration-policy",
            "source-required-v1",
            "--bootstrap-duration-snapshot",
            "tests/data/pr-test-durations.json",
            "--bootstrap-duration-provenance",
            "tests/data/pr-test-durations.provenance.json",
            "--output",
            str(plan_path),
            "--log-dir",
            str(tmp_path / "plan-logs"),
            "--",
            "-m",
            "not opencode_live",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert planned.returncode == 0, planned.stdout + planned.stderr
    plan, _raw = read_plan(plan_path)
    _require_exact_baseline(plan.inventory)

    protected = next(node for node in plan.inventory if node.startswith("tests/test_startup_pending_remote_errors.py::"))
    without_protected_node = tuple(node for node in plan.inventory if node != protected)
    with pytest.raises(AssertionError):
        _require_exact_baseline(without_protected_node)
