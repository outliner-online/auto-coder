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


def _require_exact_current_inventory(collected: tuple[str, ...], planned: tuple[str, ...], assigned: tuple[str, ...]) -> None:
    """Require each independently collected current node once at both boundaries."""
    expected = Counter(collected)
    assert len(expected) == len(collected)
    assert Counter(planned) == expected
    assert Counter(assigned) == expected


def _require_baseline_inventory(baseline: tuple[str, ...], collected: tuple[str, ...], planned: tuple[str, ...], executed: tuple[str, ...]) -> None:
    """Require every immutable baseline node once at each candidate boundary."""
    expected = Counter(baseline)
    assert len(expected) == len(baseline)
    for candidate in (collected, planned, executed):
        observed = Counter(candidate)
        assert all(observed[node] == count for node, count in expected.items())


@pytest.mark.skipif(sys.version_info[:2] != (3, 12), reason="complete-source inventory is locked to Python 3.12")
def test_production_complete_source_inventory_preserves_current_collection_exactly_once(tmp_path: Path) -> None:
    """Run the workflow planner and reject omission of a current-profile node."""
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    context = tmp_path / "context.json"
    plan_path = tmp_path / "plan.json"
    environment = {**os.environ, "AUTO_CODER_REQUIRE_BROWSER": "1", "AUTO_CODER_IMMEDIATE_TEST_FAILURES": "1"}
    context_result = subprocess.run(
        [
            "uv",
            "run",
            "python",
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
    collection_path = tmp_path / "independent-inventory.json"
    collected = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "-m",
            "pytest",
            "-p",
            "scripts.ci_shard_plugin",
            "--collect-only",
            f"--ci-context={context.resolve()}",
            f"--ci-collect-output={collection_path.resolve()}",
            "-m",
            "not opencode_live",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert collected.returncode == 0, collected.stdout + collected.stderr
    current_inventory = tuple(json.loads(collection_path.read_text(encoding="utf-8"))["nodeids"])
    planned = subprocess.run(
        [
            "uv",
            "run",
            "python",
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
    assigned_union = tuple(node for shard in plan.shards for node in shard)
    _require_exact_current_inventory(current_inventory, plan.inventory, assigned_union)

    # The immutable source/profile inventory is the required lower bound. The
    # current collection may add nodes, but neither the candidate plan nor the
    # plan-derived native execution inventory may omit a baseline node.
    baseline = _baseline()
    assert any(node.startswith("tests/test_startup_pending_remote_errors.py::") for node in baseline)
    _require_baseline_inventory(baseline, current_inventory, plan.inventory, assigned_union)

    # Model the reported counterexample: the same baseline node disappears at
    # collection, planning, and receipt-execution boundaries. Agreement among
    # the reduced candidate views must not make that omission acceptable.
    protected = baseline[0]
    reduced_collection = tuple(node for node in current_inventory if node != protected)
    reduced_plan = tuple(node for node in plan.inventory if node != protected)
    reduced_executions = tuple(node for node in assigned_union if node != protected)
    with pytest.raises(AssertionError):
        _require_baseline_inventory(baseline, reduced_collection, reduced_plan, reduced_executions)
