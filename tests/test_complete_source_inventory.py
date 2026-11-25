"""Production-profile protection for the complete source native inventory."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import subprocess
import sys
import tarfile
from collections import Counter
from pathlib import Path

import pytest
from scripts import check_source_test_shards

from auto_coder.ci_shard_plan import read_plan
from auto_coder.tested_source_refs import TestedSourceError as SourceQualificationError
from auto_coder.util.gh_cache import GitDataResponse

pytestmark = pytest.mark.usefixtures("_use_real_commands")

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests/data/complete-source-baseline-nodeids.txt.gz"
PROVENANCE = ROOT / "tests/data/complete-source-baseline-nodeids.provenance.json"
NATIVE_RECEIPTS = ROOT / "tests/data/complete-source-native-receipts-1911e76.tar.gz"
NATIVE_RECEIPTS_PROVENANCE = ROOT / "tests/data/complete-source-native-receipts-1911e76.provenance.json"
INDEPENDENT_NATIVE_INVENTORY = ROOT / "tests/data/complete-source-native-inventory-1911e76.txt.gz"
INDEPENDENT_NATIVE_INVENTORY_PROVENANCE = ROOT / "tests/data/complete-source-native-inventory-1911e76.provenance.json"
PYTHON_EXECUTABLE_CASE = "tests/test_management_containment_executable_aliases.py::test_absolute_alias_cannot_hide_resolved_dispatcher_family["


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


def _independent_native_inventory() -> tuple[str, ...]:
    """Load the plain-pytest collection captured independently of the CI planner."""
    raw = gzip.decompress(INDEPENDENT_NATIVE_INVENTORY.read_bytes())
    provenance = json.loads(INDEPENDENT_NATIVE_INVENTORY_PROVENANCE.read_text(encoding="utf-8"))
    nodes = tuple(raw.decode("utf-8").splitlines())
    assert provenance == {
        "collection_command": "uv run python -m pytest --collect-only -q -m 'not opencode_live'",
        "inventory_sha256": hashlib.sha256(raw).hexdigest(),
        "lock_sha256": "c99f72a9365d07f813e908c16e7c7c1e235ffb35fd61dfd65e1842a517061efa",
        "nodes": len(nodes),
        "profile_id": "source-required-v1",
        "python_major_minor": "3.12",
        "schema_version": "complete-source-independent-native-inventory-v1",
        "selector": "not opencode_live",
        "source_sha": "1911e76a774d794ca2d42f7512646e19c8d69aba",
    }
    assert len(nodes) == len(set(nodes))
    return nodes


def _require_exact_current_inventory(collected: tuple[str, ...], planned: tuple[str, ...], assigned: tuple[str, ...]) -> None:
    """Require each independently collected current node once at both boundaries."""
    expected = Counter(collected)
    assert len(expected) == len(collected)
    assert Counter(planned) == expected
    assert Counter(assigned) == expected


def _require_baseline_inventory(
    baseline: tuple[str, ...],
    collected: tuple[str, ...],
    planned: tuple[str, ...],
    assigned: tuple[str, ...],
) -> None:
    """Require every node once in the fixed same-source comparison fixture."""

    def baseline_identity(node: str) -> str:
        # pytest derives this parameter ID from sys.executable, whose checkout
        # prefix differs between the captured baseline and a GitHub runner.
        if node.startswith(PYTHON_EXECUTABLE_CASE) and node.endswith("/.venv/bin/python3]"):
            return PYTHON_EXECUTABLE_CASE + "<python-executable>]"
        return node

    expected = Counter(map(baseline_identity, baseline))
    assert len(expected) == len(baseline)
    for candidate in (collected, planned, assigned):
        observed = Counter(map(baseline_identity, candidate))
        assert observed == expected


class _RecordedActionsEvidence:
    """Replay the authoritative Actions readback captured with native receipts."""

    def __init__(self, run: dict[str, object], jobs: dict[str, object], artifacts: dict[str, object]) -> None:
        self._responses = {
            "actions/runs/45146500439/attempts/1": run,
            "actions/runs/45146500439/attempts/1/jobs?per_page=100": jobs,
            "actions/runs/45146500439/artifacts?per_page=100": artifacts,
        }

    def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> GitDataResponse:
        assert method == "GET" and payload is None
        return GitDataResponse(200, self._responses[path])


def _materialize_recorded_attempt(tmp_path: Path) -> tuple[Path, Path, dict[str, object], dict[str, object], dict[str, object]]:
    """Verify and materialize immutable native plan/receipt evidence."""
    archive = NATIVE_RECEIPTS.read_bytes()
    provenance = json.loads(NATIVE_RECEIPTS_PROVENANCE.read_text(encoding="utf-8"))
    assert provenance == {
        "archive_sha256": hashlib.sha256(archive).hexdigest(),
        "profile_id": "source-required-v1",
        "run_attempt": 1,
        "run_id": 45146500439,
        "schema_version": "complete-source-native-receipts-v1",
        "selector": "not opencode_live",
        "source_sha": "1911e76a774d794ca2d42f7512646e19c8d69aba",
    }
    artifacts_root = tmp_path / "artifacts"
    with tarfile.open(NATIVE_RECEIPTS, "r:gz") as evidence:
        members = {member.name: member for member in evidence.getmembers() if member.isfile()}

        def payload(name: str) -> dict[str, object]:
            handle = evidence.extractfile(members[name])
            assert handle is not None
            value = json.loads(handle.read())
            assert isinstance(value, dict)
            return value

        run = payload("run.json")
        jobs = payload("jobs.json")
        artifacts = payload("artifacts.json")
        plan_name = next(name for name in members if name.endswith("/plan.json"))
        plan_path = tmp_path / "plan.json"
        plan_handle = evidence.extractfile(members[plan_name])
        assert plan_handle is not None
        plan = json.loads(plan_handle.read())
        # The recorded attempt predates the split event/run-head identity fields.
        # Adapt only the copied, digest-external origin to the current verifier;
        # the immutable archived bytes and semantic plan remain unchanged.
        plan["origin"].update(
            {
                "event_sha": provenance["source_sha"],
                "event_name": run["event"],
                "run_head_sha": run["head_sha"],
            }
        )
        plan_path.write_text(
            json.dumps(plan, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        for name, member in members.items():
            if not name.endswith("/receipt.json"):
                continue
            destination = artifacts_root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            receipt_handle = evidence.extractfile(member)
            assert receipt_handle is not None
            destination.write_bytes(receipt_handle.read())
    return plan_path, artifacts_root, run, jobs, artifacts


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
            "--event-name",
            "pull_request",
            "--run-head-sha",
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
            "--cov=src/auto_coder",
            "--cov-report=",
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
            "--cov=src/auto_coder",
            "--cov-report=",
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

    # Keep the historical comparison bound to its recorded source and profile.
    # It must not freeze later revisions: the production collection and plan
    # above deliberately use the current revision as their recurring oracle.
    baseline = _baseline()
    assert any(node.startswith("tests/test_startup_pending_remote_errors.py::") for node in baseline)
    _require_baseline_inventory(baseline, baseline, baseline, baseline)

    # Model the correlated-omission counterexample at that same recorded source:
    # agreement among reduced collection, plan, and assignment views must not
    # make a missing historical node acceptable.
    protected = baseline[0]
    reduced_collection = tuple(node for node in baseline if node != protected)
    reduced_plan = tuple(node for node in baseline if node != protected)
    reduced_assignments = tuple(node for node in baseline if node != protected)
    with pytest.raises(AssertionError):
        _require_baseline_inventory(baseline, reduced_collection, reduced_plan, reduced_assignments)


def test_recorded_native_receipts_qualify_complete_same_source_baseline_and_reject_omission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replay one real successful workflow attempt through the aggregate CLI."""
    plan_path, artifacts_root, run, jobs, artifacts = _materialize_recorded_attempt(tmp_path)
    evidence = _RecordedActionsEvidence(run, jobs, artifacts)
    monkeypatch.setenv("GITHUB_TOKEN", "recorded-actions-readback")
    monkeypatch.setattr(check_source_test_shards, "GitHubGitDataClient", lambda _token, _repository: evidence)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_source_test_shards.py",
            "--repository",
            "outliner-online/auto-coder",
            "--run-id",
            "45146500439",
            "--run-attempt",
            "1",
            "--source-sha",
            "1911e76a774d794ca2d42f7512646e19c8d69aba",
            "--plan",
            str(plan_path),
            "--artifacts",
            str(artifacts_root),
        ],
    )
    assert check_source_test_shards.main() == 0

    plan, _raw = read_plan(plan_path)
    executed: list[str] = []
    for shard in range(1, len(plan.shards) + 1):
        receipt_path = artifacts_root / f"source-test-shard-45146500439-1-{shard}" / "receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        executed.extend(receipt["executed"])
    independent_inventory = _independent_native_inventory()
    _require_baseline_inventory(independent_inventory, independent_inventory, plan.inventory, tuple(executed))

    # A plan and matching native receipts can agree after the same upstream
    # selection omission, but the independent same-source collection rejects it.
    protected = independent_inventory[0]
    reduced_plan = tuple(node for node in plan.inventory if node != protected)
    reduced_receipts = tuple(node for node in executed if node != protected)
    with pytest.raises(AssertionError):
        _require_baseline_inventory(independent_inventory, independent_inventory, reduced_plan, reduced_receipts)

    first_receipt = artifacts_root / "source-test-shard-45146500439-1-1" / "receipt.json"
    receipt = json.loads(first_receipt.read_text(encoding="utf-8"))
    omitted = receipt["executed"][0]
    for key in ("assigned", "selected", "executed"):
        receipt[key] = [node for node in receipt[key] if node != omitted]
    receipt["reports"] = [report for report in receipt["reports"] if report["nodeid"] != omitted]
    first_receipt.write_text(
        json.dumps(receipt, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SourceQualificationError, match="incomplete or failed"):
        check_source_test_shards.main()


def test_recorded_timing_comparison_reports_confounded_result_as_inconclusive() -> None:
    """Do not turn unmatched successful runs into a measured improvement claim."""
    evidence_path = ROOT / "tests/data/complete-source-timing-comparison-aed1e23.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    candidate = evidence["candidate"]
    reference = evidence["reference"]
    comparison = evidence["comparison"]

    assert evidence["schema_version"] == "complete-source-timing-comparison-v1"
    assert evidence["controls"] == {
        "automatic_retry_observation": ("Both successful runs are attempt 1; every receipt records one native invocation " "and each selected node executes once."),
        "profile_id": "source-required-v1",
        "python_major_minor": "3.12",
        "selector": "not opencode_live",
        "workflow": "Test Source for Runtime",
    }
    assert candidate["run_attempt"] == reference["run_attempt"] == 1
    assert candidate["max_job_elapsed_seconds"] == (candidate["pre_command_seconds"] + candidate["max_command_elapsed_seconds"] + candidate["post_command_seconds"])
    assert reference["max_job_elapsed_seconds"] == (reference["pre_command_seconds"] + reference["max_command_elapsed_seconds"] + reference["post_command_seconds"])
    for observation in (candidate, reference):
        phases = observation["max_native_phase_seconds"]
        assert phases["total"] == pytest.approx(phases["setup"] + phases["call"] + phases["teardown"], abs=0.001)

    assert comparison["job_elapsed_delta_seconds"] == (candidate["max_job_elapsed_seconds"] - reference["max_job_elapsed_seconds"])
    assert comparison["command_elapsed_delta_seconds"] == (candidate["max_command_elapsed_seconds"] - reference["max_command_elapsed_seconds"])
    assert comparison["native_phase_delta_seconds"] == pytest.approx(
        candidate["max_native_phase_seconds"]["total"] - reference["max_native_phase_seconds"]["total"],
        abs=0.001,
    )
    assert candidate["source_sha"] != reference["source_sha"]
    assert candidate["configuration_digest"] != reference["configuration_digest"]
    assert candidate["inventory_nodes"] != reference["inventory_nodes"]
    assert candidate["max_shard"].split("/")[1] != reference["max_shard"].split("/")[1]
    assert comparison["conclusion"] == "inconclusive"
    assert set(comparison["reasons"]) == {
        "different source revisions",
        "different configuration digests",
        "different native inventories",
        "different shard counts",
        "single successful observation per configuration",
    }
