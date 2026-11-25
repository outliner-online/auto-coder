"""Real source-selected pytest collection, native receipts and failure paths."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from scripts import check_source_test_shards

from auto_coder.ci_shard_plan import ShardContext, read_plan
from auto_coder.source_ci_evidence import PLAN_JOB_NAME, WORKFLOW_PATH, _artifact_name, receipt_complete, validate_receipt
from auto_coder.tested_source_refs import TestedSourceError as SourceQualificationError
from auto_coder.util.gh_cache import GitDataResponse
from tests.test_ci_shard_plan import _history, _inventory, _plan_cli, _project, _run

pytestmark = pytest.mark.usefixtures("_use_real_commands")

GOOD_TESTS = """
import pytest

@pytest.mark.parametrize("value", [1, 2], ids=["new-one", "new-two"])
def test_parameterized(value):
    assert value > 0

@pytest.fixture
def no_prerequisite():
    pytest.skip("documented prerequisite unavailable")

def test_prerequisite_skip(no_prerequisite):
    raise AssertionError("skip must precede call")

@pytest.mark.xfail(reason="known behavior")
def test_expected_failure():
    assert False
"""

BAD_TESTS = """
import pytest

def test_native_failure():
    assert False, "real pytest failure must not qualify"

@pytest.fixture
def teardown_error():
    yield
    raise RuntimeError("real teardown failure must not qualify")

def test_native_teardown_error(teardown_error):
    assert True
"""


def _source_project(tmp_path: Path, source: str, *, pytest_args: tuple[str, ...] = ("-m", "not opencode_live")) -> tuple[Path, Path, ShardContext]:
    project, context_path, previous = _project(tmp_path, source=source)
    context = ShardContext(
        previous.repository,
        "source-required-v1",
        previous.checkout_sha,
        pytest_args,
        previous.harness_files,
        (),
        {
            "repository": previous.repository,
            "workflow": "Test Source for Runtime",
            "run_id": "7",
            "run_attempt": "1",
            "planning_attempt": "source-plan-7-1",
            "event_sha": previous.checkout_sha,
            "event_name": "pull_request",
            "run_head_sha": "f" * 40,
        },
    )
    context_path.write_text(json.dumps(context.json()), encoding="utf-8")
    return project, context_path, context


def _apply_receipt(project: Path, plan_path: Path, shard: int, receipt: Path, *, attempt: str = "1", event_name: str = "pull_request", env_patch: dict[str, str] | None = None, pytest_args: tuple[str, ...] | None = None) -> subprocess.CompletedProcess[str]:
    plan, _raw = read_plan(plan_path)
    return _run(
        project,
        "-m",
        "pytest",
        "-p",
        "scripts.ci_shard_plugin",
        "-p",
        "scripts.source_ci_receipt_plugin",
        f"--ci-plan={plan_path}",
        f"--ci-shard={shard}",
        f"--source-ci-receipt={receipt}",
        *(plan.context.pytest_args if pytest_args is None else pytest_args),
        env_patch={"GITHUB_WORKFLOW": "Test Source for Runtime", "GITHUB_JOB": "complete-source-shard", "GITHUB_RUN_ID": "7", "GITHUB_RUN_ATTEMPT": attempt, "GITHUB_SHA": plan.context.checkout_sha, "GITHUB_EVENT_NAME": event_name, **(env_patch or {})},
    )


def _iso(seconds: int) -> str:
    return (datetime(2026, 11, 23, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


class _AggregateEvidence:
    """Actions readback for receipts produced by the real native plugins."""

    def __init__(self, plan_path: Path, artifacts: Path) -> None:
        self.plan_path = plan_path
        self.artifacts = artifacts
        self.plan, _raw = read_plan(plan_path)

    def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> GitDataResponse:
        assert method == "GET" and payload is None
        if path == "actions/runs/7/attempts/1":
            return GitDataResponse(
                200,
                {
                    "id": 7,
                    "run_attempt": 1,
                    "head_sha": self.plan.context.origin["run_head_sha"],
                    "event": self.plan.context.origin["event_name"],
                    "path": WORKFLOW_PATH,
                    "status": "in_progress",
                    "run_started_at": _iso(0),
                },
            )
        if path == "actions/runs/7/attempts/1/jobs?per_page=100":
            jobs: list[dict[str, object]] = [{"name": PLAN_JOB_NAME, "run_id": 7, "run_attempt": 1, "id": 100, "status": "completed", "conclusion": "success", "started_at": _iso(1), "completed_at": _iso(5), "steps": []}]
            steps = [{"name": name, "conclusion": "success"} for name in ("Prepare Python-only management test fixture", "Install Chromium", "Verify real headless Chromium", "Run complete required source selection")]
            for shard in range(1, len(self.plan.shards) + 1):
                receipt_path = self.artifacts / _artifact_name(7, 1, shard) / "receipt.json"
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                jobs.append({"name": f"Complete source tests ({shard}/{len(self.plan.shards)})", "run_id": 7, "run_attempt": 1, "id": 100 + shard, "status": "completed", "conclusion": "success", "started_at": receipt["started_at"], "completed_at": receipt["completed_at"], "steps": steps})
            return GitDataResponse(200, {"total_count": len(jobs), "jobs": jobs})
        if path.startswith("actions/runs/7/attempts/1/jobs?per_page=100&page="):
            return GitDataResponse(200, {"total_count": len(self.plan.shards) + 1, "jobs": []})
        if path.startswith("actions/runs/7/artifacts?per_page=100"):
            names = [_artifact_name(7, 1, None)] + [_artifact_name(7, 1, shard) for shard in range(1, len(self.plan.shards) + 1)]
            artifacts = [{"name": name, "id": 400 + index, "expired": False, "created_at": _iso(21), "workflow_run": {"id": 7}} for index, name in enumerate(names)]
            return GitDataResponse(200, {"total_count": len(artifacts), "artifacts": artifacts})
        raise AssertionError(path)


def _run_aggregate(monkeypatch: pytest.MonkeyPatch, plan_path: Path, artifacts: Path) -> None:
    evidence = _AggregateEvidence(plan_path, artifacts)
    monkeypatch.setenv("GITHUB_TOKEN", "aggregate-read-token")
    monkeypatch.setattr(check_source_test_shards, "GitHubGitDataClient", lambda _token, _repository: evidence)
    monkeypatch.setattr(sys, "argv", ["check_source_test_shards.py", "--repository", evidence.plan.context.repository, "--run-id", "7", "--run-attempt", "1", "--source-sha", evidence.plan.context.checkout_sha, "--plan", str(plan_path), "--artifacts", str(artifacts)])
    check_source_test_shards.main()


def _create_source_context(project: Path, output: Path, source_sha: str, event_sha: str, run_head_sha: str, event_name: str) -> subprocess.CompletedProcess[str]:
    return _run(
        project,
        str(Path(__file__).resolve().parents[1] / "scripts/source_ci_runtime.py"),
        "--log-dir",
        str(output.parent / "context-logs"),
        "context",
        "--repository",
        "owner/repo",
        "--source-sha",
        source_sha,
        "--event-sha",
        event_sha,
        "--run-head-sha",
        run_head_sha,
        "--event-name",
        event_name,
        "--run-id",
        "7",
        "--run-attempt",
        "1",
        "--output",
        str(output),
    )


@pytest.mark.parametrize("event_name", ["pull_request", "push", "workflow_dispatch"])
def test_actual_context_cli_records_tested_event_and_actions_head_separately(event_name: str, tmp_path: Path) -> None:
    project, _context_path, previous = _project(tmp_path, source="def test_one(): assert True\n")
    output = tmp_path / "source-context.json"
    head = "f" * 40 if event_name == "pull_request" else previous.checkout_sha

    result = _create_source_context(project, output, previous.checkout_sha, previous.checkout_sha, head, event_name)

    assert result.returncode == 0, result.stdout + result.stderr
    observed = ShardContext.read(output)
    assert observed.checkout_sha == observed.origin["event_sha"] == previous.checkout_sha
    assert observed.origin["run_head_sha"] == head
    assert observed.origin["event_name"] == event_name
    assert observed.pytest_args == ("-m", "not opencode_live", "--cov=src/auto_coder", "--cov-report=")


@pytest.mark.parametrize("defect", ["foreign_checkout", "foreign_event", "invalid_head", "push_foreign_head", "dispatch_foreign_head"])
def test_actual_context_cli_refuses_misbound_source_identity_before_writing(defect: str, tmp_path: Path) -> None:
    project, _context_path, previous = _project(tmp_path, source="def test_one(): assert True\n")
    output = tmp_path / "refused-context.json"
    source = event = previous.checkout_sha
    head = "f" * 40
    event_name = "pull_request"
    if defect == "foreign_checkout":
        source = event = head
    elif defect == "foreign_event":
        event = head
    elif defect == "invalid_head":
        head = "not-a-commit"
    elif defect == "push_foreign_head":
        event_name = "push"
    elif defect == "dispatch_foreign_head":
        event_name = "workflow_dispatch"

    result = _create_source_context(project, output, source, event, head, event_name)

    assert result.returncode != 0
    assert not output.exists()


def test_coverage_instrumentation_is_bound_to_plan_and_native_receipts(tmp_path: Path) -> None:
    from coverage import Coverage

    pytest_args = ("-m", "not opencode_live", "--cov=tests", "--cov-report=")
    project, context_path, _context = _source_project(tmp_path, "def test_one(): assert True\n", pytest_args=pytest_args)
    plan_path = tmp_path / "instrumented-plan.json"
    instrumentation = {"PYTEST_PLUGINS": "pytest_cov.plugin"}
    collected = _run(
        project,
        str(Path(__file__).resolve().parents[1] / "scripts/plan_ci_shards.py"),
        "--context",
        str(context_path),
        "--output",
        str(plan_path),
        "--log-dir",
        str(tmp_path / "plan-logs"),
        "--duration-policy",
        "source-required-v1",
        "--",
        *pytest_args,
        env_patch={**instrumentation, "COVERAGE_FILE": str(tmp_path / "collection.coverage")},
    )
    assert collected.returncode == 0, collected.stdout + collected.stderr
    coverage_file = tmp_path / "shard.coverage"
    receipt = tmp_path / "instrumented-receipt.json"

    applied = _apply_receipt(project, plan_path, 1, receipt, env_patch={**instrumentation, "COVERAGE_FILE": str(coverage_file)})

    assert applied.returncode == 0, applied.stdout + applied.stderr
    plan, _raw = read_plan(plan_path)
    validate_receipt(json.loads(receipt.read_text(encoding="utf-8")), plan, 1, 7, 1)
    assert coverage_file.is_file()
    coverage = Coverage(data_file=str(coverage_file))
    coverage.load()
    assert any(path.endswith("tests/test_sample.py") and coverage.get_data().lines(path) for path in coverage.get_data().measured_files())
    uninstrumented = tmp_path / "uninstrumented-receipt.json"
    rejected = _apply_receipt(project, plan_path, 1, uninstrumented, env_patch=instrumentation, pytest_args=("-m", "not opencode_live"))
    assert rejected.returncode != 0
    assert "pytest selector/configuration mismatch" in rejected.stdout + rejected.stderr
    assert not uninstrumented.exists()


def test_actual_source_plan_and_all_native_shard_receipts_cover_exact_full_inventory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, context_path, context = _source_project(tmp_path, GOOD_TESTS)
    profile, collected = _inventory(project, context_path, context, monkeypatch)
    assert any("new-one" in node for node in collected) and any("new-two" in node for node in collected)
    history = tmp_path / "profile.json"
    _history(history, profile, collected, seconds=250)
    plan_path = tmp_path / "plan.json"
    created = _plan_cli(project, context_path, plan_path, history, "-m", "not opencode_live")
    assert created.returncode == 0, created.stdout + created.stderr
    plan, _raw = read_plan(plan_path)
    assert len(plan.shards) == len(collected)
    artifacts = tmp_path / "passing-artifacts"
    observed: list[str] = []
    for shard in range(1, len(plan.shards) + 1):
        receipt = artifacts / _artifact_name(7, 1, shard) / "receipt.json"
        receipt.parent.mkdir(parents=True)
        applied = _apply_receipt(project, plan_path, shard, receipt)
        assert applied.returncode == 0, applied.stdout + applied.stderr
        data = json.loads(receipt.read_text(encoding="utf-8"))
        assert data["complete"] is True
        validate_receipt(data, plan, shard, 7, 1)
        assert receipt_complete(data, plan, shard)
        assert data["collected"] == list(collected)
        assert data["selected"] == data["assigned"] == data["executed"]
        observed.extend(data["executed"])
        if any("prerequisite_skip" in node for node in data["executed"]):
            assert [report["outcome"] for report in data["reports"]] == ["skipped", "passed"]
        if any("expected_failure" in node for node in data["executed"]):
            assert any(report["outcome"] == "skipped" and report["wasxfail"] for report in data["reports"])
    assert observed == list(collected) and Counter(observed) == Counter(collected)
    _run_aggregate(monkeypatch, plan_path, artifacts)


def test_real_native_failures_and_teardown_errors_never_create_qualifying_receipts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, context_path, _context = _source_project(tmp_path, BAD_TESTS)
    plan_path = tmp_path / "plan.json"
    created = _plan_cli(project, context_path, plan_path, None, "-m", "not opencode_live")
    assert created.returncode == 0, created.stdout + created.stderr
    plan, _raw = read_plan(plan_path)
    artifacts = tmp_path / "failed-artifacts"
    receipt = artifacts / _artifact_name(7, 1, 1) / "receipt.json"
    receipt.parent.mkdir(parents=True)
    run = _apply_receipt(project, plan_path, 1, receipt)
    assert run.returncode != 0, run.stdout + run.stderr
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["complete"] is False
    assert payload["native_exit_status"] != 0
    assert any(report["phase"] == "teardown" and report["outcome"] == "failed" for report in payload["reports"])
    with pytest.raises(ValueError, match="incomplete or failed"):
        validate_receipt(payload, plan, 1, 7, 1)
    with pytest.raises(SourceQualificationError, match="incomplete or failed"):
        _run_aggregate(monkeypatch, plan_path, artifacts)


def test_wrong_producing_attempt_and_receipt_overwrite_are_native_failures(tmp_path: Path) -> None:
    project, context_path, _context = _source_project(tmp_path, "def test_one(): assert True\n")
    plan_path = tmp_path / "plan.json"
    assert _plan_cli(project, context_path, plan_path, None, "-m", "not opencode_live").returncode == 0
    wrong = _apply_receipt(project, plan_path, 1, tmp_path / "wrong.json", attempt="2")
    assert wrong.returncode != 0
    assert "actual producing Actions identity" in wrong.stderr
    wrong_event = _apply_receipt(project, plan_path, 1, tmp_path / "wrong-event.json", event_name="push")
    assert wrong_event.returncode != 0
    assert "actual producing Actions identity" in wrong_event.stderr
    receipt = tmp_path / "preexisting.json"
    receipt.write_text("user data", encoding="utf-8")
    overwritten = _apply_receipt(project, plan_path, 1, receipt)
    assert overwritten.returncode != 0
    assert receipt.read_text(encoding="utf-8") == "user data"


def test_source_duration_cache_round_trip_matches_workflow_paths_and_reuses_native_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workflow_path = Path(__file__).resolve().parents[1] / ".github/workflows/test-source-for-runtime.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    planning_steps = {step["name"]: step for step in workflow["jobs"]["source-plan"]["steps"] if "name" in step}
    aggregate_steps = {step["name"]: step for step in workflow["jobs"]["complete-source-tests"]["steps"] if "name" in step}
    restores = [
        planning_steps["Restore optional source timing profile"],
        aggregate_steps["Restore optional source duration history"],
    ]
    saved = aggregate_steps["Cache optional immutable source timing profile"]
    update = aggregate_steps["Update optional native timing snapshot"]
    stage = aggregate_steps["Stage optional source timing snapshot at restored cache path"]
    plan_command = planning_steps["Collect complete current source tests and immutable shard plan"]["run"]

    # actions/cache incorporates the path in its cache version, not only the key.
    for restore in restores:
        assert restore["with"]["path"] == saved["with"]["path"] == "${{ runner.temp }}/source-history"
        assert restore["with"]["key"] == saved["with"]["key"]
        assert restore["continue-on-error"] is True
    assert saved["if"] == "always() && steps.duration-stage.outcome == 'success'"
    assert stage["if"] == "always() && steps.duration-update.outcome == 'success'"
    assert stage["continue-on-error"] is True
    assert saved["continue-on-error"] is True
    assert '--output "$RUNNER_TEMP/source-history-new/profile.json"' in update["run"]
    assert '--duration-profile "$RUNNER_TEMP/source-history/profile.json"' in plan_command

    project, context_path, context = _source_project(tmp_path, GOOD_TESTS)
    profile, inventory = _inventory(project, context_path, context, monkeypatch)
    producer = tmp_path / "producer-run"
    restored = producer / "source-history/profile.json"
    new_snapshot = producer / "source-history-new/profile.json"
    restored.parent.mkdir(parents=True)
    new_snapshot.parent.mkdir(parents=True)
    _history(restored, profile, inventory, seconds=111)
    stale_contents = restored.read_bytes()
    _history(new_snapshot, profile, inventory, seconds=333)

    # Execute the actual workflow staging command, including replacement of restored history.
    staged = subprocess.run(
        ["bash", "-e", "-c", stage["run"]],
        cwd=project,
        env={**os.environ, "RUNNER_TEMP": str(producer)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert staged.returncode == 0, staged.stdout + staged.stderr
    assert restored.read_bytes() == new_snapshot.read_bytes() != stale_contents

    # Simulate a later Actions cache restore and invoke the real source planner.
    consumer = tmp_path / "consumer-run"
    shutil.copytree(producer / "source-history", consumer / "source-history")
    restored_history = consumer / "source-history/profile.json"
    current_plan = tmp_path / "current-plan.json"
    result = _plan_cli(project, context_path, current_plan, restored_history, "-m", "not opencode_live")
    assert result.returncode == 0, result.stdout + result.stderr
    semantic = json.loads(current_plan.read_text(encoding="utf-8"))["semantic"]
    assert semantic["inventory"] == list(inventory)
    assert semantic["duration_source"]["status"] == "matched"
    assert {estimate["provenance"] for estimate in semantic["estimates"]} == {"native_p95"}
    assert {estimate["estimated_ms"] for estimate in semantic["estimates"]} == {333_000}

    missing_plan = tmp_path / "missing-history-plan.json"
    fallback = _plan_cli(project, context_path, missing_plan, consumer / "missing/profile.json", "-m", "not opencode_live")
    assert fallback.returncode == 0, fallback.stdout + fallback.stderr
    missing_semantic = json.loads(missing_plan.read_text(encoding="utf-8"))["semantic"]
    assert missing_semantic["inventory"] == list(inventory)
    assert missing_semantic["duration_source"]["status"] == "unavailable"
    assert {estimate["provenance"] for estimate in missing_semantic["estimates"]} == {"fallback_one_second"}
