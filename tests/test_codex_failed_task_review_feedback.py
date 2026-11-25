"""Completed review feedback survives an independently failed later Cloud turn."""

import json
import subprocess
from copy import deepcopy
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from auto_coder.cloud_task_client_base import CloudTask, CloudTaskState
from auto_coder.codex_cloud_client import CodexCloudClient, CodexReviewFeedbackReadiness, PendingCodexFollowUp
from auto_coder.codex_wham_client import CodexWhamClient, WhamTask
from tests.test_codex_completed_review_feedback import BASELINE, IDENTITY, REPOSITORY, TARGET, TASK_ID, _write
from tests.test_codex_settled_review_feedback import _turn

INITIAL_USER = f"{TASK_ID}~usertrn_initial"
REVIEW_USER = f"{TASK_ID}~usertrn_review"
REVIEW_COMPLETED = f"{TASK_ID}~assttrn_review"
CI_USER = f"{TASK_ID}~usertrn_ci"
CI_FAILED = f"{TASK_ID}~assttrn_ci"


def install_failed_feedback_evidence(monkeypatch, path, repository, task_id, target, identity):
    """Install controlled provider reads around the real native completion observer."""
    baseline = f"{task_id}~assttrn_baseline"
    initial_user = f"{task_id}~usertrn_initial"
    review_user = f"{task_id}~usertrn_review"
    review_completed = f"{task_id}~assttrn_review"
    ci_user = f"{task_id}~usertrn_ci"
    ci_failed = f"{task_id}~assttrn_ci"
    record = PendingCodexFollowUp(
        task_id=task_id,
        logical_identity=identity,
        status="delivered",
        pre_send_turn_id=baseline,
        accepted_at=100,
        message_fingerprint="f" * 64,
        repair_repository=repository,
        repair_pr_number=target.pr_number,
        repair_head_branch=target.head_branch,
        repair_base_branch=target.base_branch,
        repair_head_sha="a" * 40,
    )
    _write(path, record)
    turns = [
        _turn(initial_user, "user", None, [baseline], 90, "Initial implementation"),
        _turn(baseline, "assistant", initial_user, [review_user], 100),
        _turn(review_user, "user", baseline, [review_completed], 199, "Address the review"),
        _turn(review_completed, "assistant", review_user, [ci_user], 200),
        _turn(ci_user, "user", review_completed, [ci_failed], 299, "Repair the later CI failure"),
        replace(_turn(ci_failed, "assistant", ci_user, [], 300), status="failed"),
    ]
    current = WhamTask(id=task_id, status="failed", latest_turn_status="failed", current_user_turn=turns[-2], current_assistant_turn=turns[-1])
    client = object.__new__(CodexCloudClient)
    client.repo_name = repository
    client.wham_client = CodexWhamClient()
    status = MagicMock(return_value=CloudTask(task_id=task_id, state=CloudTaskState.FAILED))
    history = MagicMock(return_value=turns)
    details = MagicMock(return_value=current)
    monkeypatch.setattr(client, "get_task", status)
    monkeypatch.setattr(client.wham_client, "get_task_turns", history)
    monkeypatch.setattr(client.wham_client, "get_task", details)
    monkeypatch.setattr(client, "send_followup", MagicMock())
    monkeypatch.setattr(client.wham_client, "send_follow_up", MagicMock())
    monkeypatch.setattr("auto_coder.codex_cloud_client._codex_followup_state_path", lambda _repository: path)
    return client, path, record, status, history, details, current


@pytest.fixture
def failed_evidence(tmp_path, monkeypatch):
    return install_failed_feedback_evidence(monkeypatch, tmp_path / "codex-followups.json", REPOSITORY, TASK_ID, TARGET, IDENTITY)


def _observe(evidence, identities=(IDENTITY,)):
    return evidence[0].observe_review_feedback_readiness(TASK_ID, identities, TARGET)


def _assert_read_only(evidence, before):
    client, path, *_rest = evidence
    assert path.read_bytes() == before
    client.send_followup.assert_not_called()
    client.wham_client.send_follow_up.assert_not_called()


@pytest.mark.parametrize("native_status", ["failed", "error"])
@pytest.mark.parametrize("cli_summary", ["failed", "absent"])
def test_later_failed_ci_turn_does_not_hide_completed_review_feedback(failed_evidence, native_status, cli_summary):
    client, path, record, status, history, details, current = failed_evidence
    if cli_summary == "absent":
        status.return_value = None
    failed = replace(current.current_assistant_turn, status=native_status)
    history.return_value[-1] = failed
    details.return_value = replace(current, status=native_status, latest_turn_status=native_status, current_assistant_turn=failed)
    before = path.read_bytes()
    modified = path.stat().st_mtime_ns

    assert _observe(failed_evidence) is CodexReviewFeedbackReadiness.COMPLETED

    status.assert_called_once_with(TASK_ID)
    assert any(call.kwargs == {"require_complete": True} for call in history.call_args_list)
    assert details.call_count == 2
    assert record.repair_head_sha != TARGET.head_sha
    assert details.return_value.current_assistant_turn.status == native_status
    assert all(value["completed_turn_id"] == "" for value in json.loads(before).values())
    assert path.stat().st_mtime_ns == modified
    _assert_read_only(failed_evidence, before)


def test_nonzero_cloud_status_exit_uses_independent_native_failure_proof(failed_evidence, monkeypatch):
    client, path, _record, _status, _history, details, _current = failed_evidence
    client.api_key = None
    client.base_url = None
    client.active_tasks = {}
    monkeypatch.delattr(client, "get_task")
    command = MagicMock(return_value=subprocess.CompletedProcess(["codex", "cloud", "status", TASK_ID], 1, stdout="[ERROR] Implement Web Push device enrollment control\n", stderr=""))
    monkeypatch.setattr("auto_coder.codex_cloud_client.CommandExecutor.run_command", command)
    before = path.read_bytes()

    assert _observe(failed_evidence) is CodexReviewFeedbackReadiness.COMPLETED

    command.assert_called_once()
    assert command.call_args.args[0] == ["codex", "cloud", "status", TASK_ID]
    assert details.call_count == 2
    _assert_read_only(failed_evidence, before)


@pytest.mark.parametrize("native_status", ["completed", "in_progress", "cancelled"])
def test_absent_cli_summary_cannot_admit_other_native_states(failed_evidence, native_status):
    _client, path, _record, status, history, details, current = failed_evidence
    status.return_value = None
    assistant = replace(current.current_assistant_turn, status=native_status)
    history.return_value[-1] = assistant
    details.return_value = replace(current, status=native_status, latest_turn_status=native_status, current_assistant_turn=assistant)
    before = path.read_bytes()

    assert _observe(failed_evidence) is CodexReviewFeedbackReadiness.WAITING

    _assert_read_only(failed_evidence, before)


@pytest.mark.parametrize("second_baseline", [BASELINE, REVIEW_COMPLETED])
def test_every_selected_feedback_still_requires_a_completed_response(failed_evidence, second_baseline):
    _client, path, record, *_rest = failed_evidence
    second = replace(record, logical_identity=IDENTITY + "-second", pre_send_turn_id=second_baseline)
    _write(path, record, second)
    before = path.read_bytes()

    result = _observe(failed_evidence, (IDENTITY, second.logical_identity))

    assert result is (CodexReviewFeedbackReadiness.COMPLETED if second_baseline == BASELINE else CodexReviewFeedbackReadiness.WAITING)
    _assert_read_only(failed_evidence, before)


@pytest.mark.parametrize("baseline", [REVIEW_COMPLETED, CI_FAILED, f"{TASK_ID}~assttrn_omitted"])
def test_failed_or_missing_selected_correction_never_becomes_completed(failed_evidence, baseline):
    _client, path, record, *_rest = failed_evidence
    _write(path, replace(record, pre_send_turn_id=baseline))
    before = path.read_bytes()

    assert _observe(failed_evidence) is CodexReviewFeedbackReadiness.WAITING

    _assert_read_only(failed_evidence, before)


@pytest.mark.parametrize(
    "damage",
    ["absent", "foreign-task", "no-assistant", "no-user", "cancelled", "current-completed", "current-running", "latest-completed", "latest-absent", "queued-user", "running-user", "current-time-changed", "user-time-changed"],
)
def test_failed_cli_summary_requires_current_native_idle_failure(failed_evidence, damage):
    _client, path, _record, _status, history, details, current = failed_evidence
    changed = current
    if damage == "absent":
        changed = None
    elif damage == "foreign-task":
        changed = replace(current, id="task_e_other")
    elif damage == "no-assistant":
        changed = replace(current, current_assistant_turn=None)
    elif damage == "no-user":
        changed = replace(current, current_user_turn=None)
    elif damage in {"cancelled", "current-completed", "current-running"}:
        native_status = {"cancelled": "cancelled", "current-completed": "completed", "current-running": "in_progress"}[damage]
        assistant = replace(current.current_assistant_turn, status=native_status)
        history.return_value[-1] = assistant
        changed = replace(current, status=native_status, latest_turn_status=native_status, current_assistant_turn=assistant)
    elif damage == "latest-completed":
        changed = replace(current, latest_turn_status="completed")
    elif damage == "latest-absent":
        changed = replace(current, latest_turn_status="")
    elif damage in {"queued-user", "running-user"}:
        user = replace(current.current_user_turn, status="queued" if damage == "queued-user" else "running")
        history.return_value[-2] = user
        changed = replace(current, current_user_turn=user)
    elif damage == "current-time-changed":
        changed = replace(current, current_assistant_turn=replace(current.current_assistant_turn, created_at="301"))
    else:
        changed = replace(current, current_user_turn=replace(current.current_user_turn, created_at="301"))
    details.return_value = changed
    before = path.read_bytes()

    assert _observe(failed_evidence) is CodexReviewFeedbackReadiness.WAITING

    _assert_read_only(failed_evidence, before)


@pytest.mark.parametrize("damage", ["missing-current", "contradictory-parent", "active-sibling", "duplicate", "new-request"])
def test_incomplete_or_active_native_history_prevents_review_admission(failed_evidence, damage):
    _client, path, _record, _status, history, _details, _current = failed_evidence
    turns = deepcopy(history.return_value)
    if damage == "missing-current":
        turns = turns[:-1]
    elif damage == "contradictory-parent":
        turns[-1].raw_data["parent"] = REVIEW_USER
    elif damage == "active-sibling":
        sibling = f"{TASK_ID}~assttrn_active"
        turns[-2].raw_data["children"].append(sibling)
        turns.append(replace(_turn(sibling, "assistant", CI_USER, [], 301), status="in_progress"))
    elif damage == "duplicate":
        turns.append(turns[-1])
    else:
        request = f"{TASK_ID}~usertrn_queued"
        turns[-1].raw_data["children"].append(request)
        turns.append(replace(_turn(request, "user", CI_FAILED, [], 301, "New work"), status="queued"))
    history.return_value = turns
    before = path.read_bytes()

    assert _observe(failed_evidence) is CodexReviewFeedbackReadiness.WAITING

    _assert_read_only(failed_evidence, before)


def test_changed_current_execution_during_completion_observation_defers(failed_evidence):
    _client, path, _record, _status, _history, details, current = failed_evidence
    details.side_effect = [current, replace(current, latest_turn_status="in_progress")]
    before = path.read_bytes()

    assert _observe(failed_evidence) is CodexReviewFeedbackReadiness.WAITING

    assert details.call_count == 2
    _assert_read_only(failed_evidence, before)


@pytest.mark.parametrize("same_owner", [True, False])
def test_receipts_are_rechecked_after_failed_current_task_observation(failed_evidence, same_owner):
    _client, path, record, _status, _history, details, current = failed_evidence
    changed = replace(record, logical_identity="another-request", task_id=TASK_ID if same_owner else "task_e_other")

    def read_current(_task_id):
        if details.call_count == 2:
            _write(path, record, changed)
        return current

    _write(path, record, changed)
    expected = path.read_bytes()
    _write(path, record)
    details.side_effect = read_current

    assert _observe(failed_evidence) is (CodexReviewFeedbackReadiness.WAITING if same_owner else CodexReviewFeedbackReadiness.COMPLETED)
    assert details.call_count == 2
    _assert_read_only(failed_evidence, expected)


@pytest.mark.parametrize("boundary", ["details", "history", "recheck"])
def test_failed_task_provider_read_errors_preserve_receipts(failed_evidence, boundary):
    _client, path, _record, _status, history, details, current = failed_evidence
    if boundary == "details":
        details.side_effect = RuntimeError("native task unavailable")
    elif boundary == "history":
        history.side_effect = RuntimeError("complete graph unavailable")
    else:
        details.side_effect = [current, RuntimeError("native recheck unavailable")]
    before = path.read_bytes()

    assert _observe(failed_evidence) is CodexReviewFeedbackReadiness.WAITING

    _assert_read_only(failed_evidence, before)
