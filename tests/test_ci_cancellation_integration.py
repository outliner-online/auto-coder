"""Webhook, configuration, PR entry and dashboard cancellation recovery regressions."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from auto_coder import pr_processor
from auto_coder.automation_config import AutomationConfig, ProcessedPRResult, PRProcessingOutcome
from auto_coder.entity_invalidation import DurableInvalidationQueue
from auto_coder.execution_trace import Outcome, TraceCollector, get_trace_collector
from auto_coder.llm_backend_config import get_ci_cancellation_recovery_from_config
from auto_coder.webhook_server import create_app
from tests.test_ci_cancellation_recovery import HEAD, POLICY, REPOSITORY, NativeAPI
from tests.test_dashboard_observability import _mounted_detail


def _intake(path: Path):
    engine = SimpleNamespace(invalidations=DurableInvalidationQueue(path), github=MagicMock(), _invalidation_wake_event=None)
    with patch("auto_coder.webhook_server.init_dashboard"):
        app = create_app(engine, REPOSITORY)
    return engine, TestClient(app)


def _job_event(client, *, head=HEAD, native_id=1, run_id=100, attempt=1, conclusion="cancelled", delivery="job"):
    return client.post(
        "/hooks/github",
        json={"repository": {"full_name": REPOSITORY}, "action": "completed", "workflow_job": {"id": native_id, "run_id": run_id, "run_attempt": attempt, "head_sha": head, "conclusion": conclusion}},
        headers={"X-GitHub-Event": "workflow_job", "X-GitHub-Delivery": delivery},
    )


def _run_event(client, *, head=HEAD):
    return client.post(
        "/hooks/github",
        json={"repository": {"full_name": REPOSITORY}, "action": "completed", "workflow_run": {"id": 100, "workflow_id": 20, "run_attempt": 1, "head_sha": head, "conclusion": "failure", "pull_requests": [{"number": 7}]}},
        headers={"X-GitHub-Event": "workflow_run", "X-GitHub-Delivery": "run"},
    )


def test_native_job_webhook_retains_candidate_without_any_request_and_survives_restart(tmp_path):
    path = tmp_path / "queue.sqlite3"
    engine, client = _intake(path)
    with client:
        # Terminal-run and job deliveries may arrive in either order.
        assert _run_event(client).status_code == 200
        assert _job_event(client).status_code == 200
        assert _job_event(client).status_code == 200
    engine.github.get_pull_request_numbers_for_commit.assert_not_called()
    reopened = DurableInvalidationQueue(path)
    assert reopened.ci_recovery_signal_state(REPOSITORY, HEAD) == (True, True)
    assert reopened.ci_recovery_signal_state("another/repo", HEAD) == (False, False)
    assert reopened.ci_recovery_signal_state(REPOSITORY, "another-head") == (True, False)
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT run_id, run_attempt, job_id, conclusion FROM ci_webhook_deliveries WHERE event_type = 'workflow_job'").fetchall()
    assert rows == [("100", 1, "1", "cancelled")]


@pytest.mark.parametrize("native_id,conclusion", [(None, "cancelled"), (True, "cancelled"), (1, "failure"), (1, "timed_out")])
def test_missing_native_identity_and_other_conclusions_are_not_cancelled_candidates(tmp_path, native_id, conclusion):
    path = tmp_path / "queue.sqlite3"
    _, client = _intake(path)
    with client:
        assert _run_event(client).status_code == 200
        assert _job_event(client, native_id=native_id, conclusion=conclusion).status_code == 200
    assert DurableInvalidationQueue(path).ci_recovery_signal_state(REPOSITORY, HEAD)[1] is False


@pytest.mark.parametrize("run_id,attempt", [(None, 1), (True, 1), (0, 1), (100, None), (100, 0), (100, True)])
def test_missing_run_or_attempt_never_becomes_a_native_cancellation_candidate(tmp_path, run_id, attempt):
    path = tmp_path / "queue.sqlite3"
    _, client = _intake(path)
    with client:
        assert _run_event(client).status_code == 200
        assert _job_event(client, run_id=run_id, attempt=attempt).status_code == 200
    assert DurableInvalidationQueue(path).ci_recovery_signal_state(REPOSITORY, HEAD)[1] is False


def test_existing_delivery_schema_upgrades_without_fabricating_a_candidate(tmp_path):
    path = tmp_path / "queue.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE ci_webhook_deliveries (repository TEXT NOT NULL, delivery_id TEXT NOT NULL, event_type TEXT NOT NULL, action TEXT, head_sha TEXT, workflow_id TEXT, run_id TEXT, run_attempt INTEGER, received_at REAL NOT NULL, PRIMARY KEY(repository, delivery_id))")
        connection.execute("INSERT INTO ci_webhook_deliveries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (REPOSITORY, "legacy", "workflow_job", "completed", HEAD, None, "100", 1, 0))
    queue = DurableInvalidationQueue(path)
    assert queue.ci_recovery_signal_state(REPOSITORY, HEAD) == (False, False)
    _, client = _intake(path)
    with client:
        assert _job_event(client).status_code == 200
        assert _run_event(client).status_code == 200
    assert DurableInvalidationQueue(path).ci_recovery_signal_state(REPOSITORY, HEAD) == (True, True)


def test_repository_scoped_configuration_enables_only_explicit_workflows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / ".auto-coder"
    directory.mkdir()
    config_file = directory / "config.toml"
    config_file.write_text(
        '[[repository."owner/repo".ci_cancellation_recovery.workflows]]\n' 'workflow_path = ".github/workflows/tests.yml"\n' 'root_job_names = ["Test A", "Test B"]\n' 'aggregate_job_names = ["Required aggregate"]\n' "multi_root_verified = true\n",
        encoding="utf-8",
    )
    assert get_ci_cancellation_recovery_from_config(str(config_file), REPOSITORY) == (POLICY,)
    assert get_ci_cancellation_recovery_from_config(str(config_file), "another/repo") == ()
    assert AutomationConfig(repo_name=REPOSITORY).CI_CANCELLATION_RECOVERY == (POLICY,)


@pytest.mark.parametrize(
    "configuration",
    [
        'workflows = "yes"',
        '[[workflows]]\nworkflow_path = "deploy.yml"\nroot_job_names = ["Test A"]',
        '[[workflows]]\nworkflow_path = ".github/workflows/tests.yml"\nroot_job_names = ["Test A", "Test A"]',
        '[[workflows]]\nworkflow_path = ".github/workflows/tests.yml"\nroot_job_names = ["Test A"]\naggregate_job_names = ["Test A"]',
        '[[workflows]]\nworkflow_path = ".github/workflows/tests.yml"\nroot_job_names = ["Test A"]\nmulti_root_verified = "true"',
    ],
)
def test_malformed_recovery_configuration_fails_before_any_effect(tmp_path, configuration):
    path = tmp_path / "config.toml"
    path.write_text("[ci_cancellation_recovery]\n" + configuration.replace("[[workflows]]", "[[ci_cancellation_recovery.workflows]]") + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        get_ci_cancellation_recovery_from_config(str(path), REPOSITORY)


@pytest.mark.parametrize("lost_response", [False, True])
def test_real_pr_entry_selectively_recovers_and_shows_handoff_without_merge(tmp_path, monkeypatch, lost_response):
    database = tmp_path / "queue.sqlite3"
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(database))
    monkeypatch.setenv("AUTO_CODER_CI_RECOVERY_DB", str(tmp_path / "claims.sqlite3"))
    _, client = _intake(database)
    with client:
        assert _job_event(client).status_code == 200
        assert _run_event(client).status_code == 200
    api = NativeAPI()
    if lost_response:
        api.post_exception = TimeoutError("response unavailable")
    monkeypatch.setattr("auto_coder.ci_cancellation_recovery.get_ghapi_client", lambda *_args, **_kwargs: api)
    config = AutomationConfig()
    config.CI_CANCELLATION_RECOVERY = (POLICY,)
    monkeypatch.setattr(pr_processor, "_reject_unsafe_codex_cloud_pr", lambda *_args: pr_processor.UnsafeCodexCloudPRResult(authoritative_pr_data=api.metadata))
    review = MagicMock()
    repair = MagicMock()
    monkeypatch.setattr(pr_processor, "_coordinate_pr_review", review)
    monkeypatch.setattr(pr_processor, "_dispatch_codex_ci_failure_repair", repair)
    monkeypatch.setattr(TraceCollector, "_instance", None)
    collector = get_trace_collector()
    result = ProcessedPRResult(api.metadata)
    with collector.start_execution(REPOSITORY, "pr", 7, origin="durable-invalidation-worker"):
        actions = pr_processor._handle_pr_merge(SimpleNamespace(token="credential"), REPOSITORY, api.metadata, config, {}, result)
    expected = "indeterminate" if lost_response else "accepted"
    assert result.outcome is PRProcessingOutcome.DEFERRED
    assert any(expected in action for action in actions)
    assert api.posts == [(f"/repos/{REPOSITORY}/actions/jobs/1/rerun", {"enable_debug_logging": False})]
    review.assert_not_called()
    repair.assert_not_called()
    event = next(event for event in collector.get_snapshot(repository=REPOSITORY, item_type="pr", item_number=7).events if event.stage_id == "pr.ci-cancellation-recovery")
    assert event.outcome == (Outcome.DEFERRED.value if lost_response else Outcome.ACCEPTED_HANDOFF.value)
    assert event.facts["ci_success"] is False
    assert event.facts["claim_id"]
    assert event.facts["job_id"] == 1
    with patch("auto_coder.dashboard.ui") as ui:
        assert "cancelled CI recovery" in _mounted_detail(ui, "pr", 7)
    # A durable watch, not a new per-PR poller, owns missing-notification recovery.
    assert DurableInvalidationQueue(database).seconds_until_next_ci(REPOSITORY) is not None


def test_unverified_webhook_delivery_never_enables_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "queue.sqlite3"))
    api = NativeAPI()
    config = AutomationConfig()
    config.CI_CANCELLATION_RECOVERY = (POLICY,)
    monkeypatch.setattr(pr_processor, "_reject_unsafe_codex_cloud_pr", lambda *_args: pr_processor.UnsafeCodexCloudPRResult(authoritative_pr_data=api.metadata))
    monkeypatch.setattr(pr_processor, "_is_pr_review_thread_gate_enabled", lambda *_args: True)
    monkeypatch.setattr(pr_processor, "retry_pending_stale_review_thread_rollbacks", lambda *_args: ["existing-review-gate"])
    send = MagicMock()
    monkeypatch.setattr("auto_coder.ci_cancellation_recovery.recover_cancelled_jobs", send)
    actions = pr_processor._handle_pr_merge(SimpleNamespace(token="credential"), REPOSITORY, api.metadata, config, {})
    send.assert_not_called()
    assert any("webhook_delivery_unverified" in action for action in actions)
    assert any("existing-review-gate" in action for action in actions)
