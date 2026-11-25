"""Completion evidence for an ordinary review's first independent reassessment."""

import hashlib
import json
from dataclasses import asdict, replace
from unittest.mock import MagicMock

import pytest

from auto_coder.cloud_task_client_base import CloudTask, CloudTaskState
from auto_coder.codex_cloud_client import CodexCloudClient, CodexReviewFeedbackReadiness, PendingCodexFollowUp
from auto_coder.codex_wham_client import CodexWhamClient, WhamTurn
from auto_coder.pr_repair import ExistingPrRepairTarget

REPOSITORY = "owner/repo"
TASK_ID = "task_e_6019"
IDENTITY = "owner/repo#6019:codex-cloud:task_e_6019:review-finding"
BASELINE = f"{TASK_ID}~assttrn_baseline"
TARGET = ExistingPrRepairTarget(REPOSITORY, 6019, "issue-5983", "main", "b" * 40)


def _key(record):
    return hashlib.sha256(f"{record.task_id}\0{record.logical_identity}".encode()).hexdigest()


def _write(path, *records):
    path.write_text(json.dumps({_key(record): asdict(record) for record in records}), encoding="utf-8")


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    path = tmp_path / "codex_followups.json"
    record = PendingCodexFollowUp(
        task_id=TASK_ID,
        logical_identity=IDENTITY,
        status="delivered",
        pre_send_turn_id=BASELINE,
        accepted_at=100,
        message_fingerprint="f" * 64,
        repair_repository=REPOSITORY,
        repair_pr_number=6019,
        repair_head_branch=TARGET.head_branch,
        repair_base_branch=TARGET.base_branch,
        repair_head_sha="a" * 40,
    )
    _write(path, record)
    client = object.__new__(CodexCloudClient)
    client.repo_name = REPOSITORY
    client.wham_client = CodexWhamClient()
    status = MagicMock(return_value=CloudTask(task_id=TASK_ID, state=CloudTaskState.COMPLETED))
    history = MagicMock(
        return_value=[
            WhamTurn(id=f"{TASK_ID}~assttrn_after", role="assistant", status="completed", created_at="200"),
            WhamTurn(id=BASELINE, role="assistant", status="completed", created_at="100"),
        ]
    )
    monkeypatch.setattr(client, "get_task", status)
    monkeypatch.setattr(client.wham_client, "get_task_turns", history)
    monkeypatch.setattr(client.wham_client, "get_task", MagicMock(return_value=None))
    monkeypatch.setattr("auto_coder.codex_cloud_client._codex_followup_state_path", lambda _repository: path)
    return client, path, record, status, history


def test_completed_ordinary_feedback_allows_new_head_observation_without_rewriting(evidence):
    client, path, record, status, history = evidence
    second = replace(record, logical_identity=IDENTITY + "-second")
    _write(path, record, second)
    before = path.read_bytes()
    modified = path.stat().st_mtime_ns

    assert client.observe_review_feedback_readiness(TASK_ID, (IDENTITY, second.logical_identity), TARGET) is CodexReviewFeedbackReadiness.COMPLETED

    status.assert_called_once_with(TASK_ID)
    history.assert_called_once_with(TASK_ID)
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == modified
    assert json.loads(path.read_text())[_key(record)]["completed_turn_id"] == ""
    assert record.repair_head_sha != TARGET.head_sha


def test_every_distinct_selected_baseline_requires_completed_activity(evidence):
    client, path, record, _status, history = evidence
    second = replace(record, logical_identity=IDENTITY + "-second", pre_send_turn_id=f"{TASK_ID}~assttrn_after")
    _write(path, record, second)

    assert client.observe_review_feedback_readiness(TASK_ID, (IDENTITY, second.logical_identity), TARGET) is CodexReviewFeedbackReadiness.WAITING
    assert history.call_count == 2


@pytest.mark.parametrize(
    "change",
    [
        {"status": "indeterminate"},
        {"status": "not_delivered"},
        {"pre_send_turn_id": ""},
        {"pre_send_turn_id": None},
        {"pre_send_turn_id": "task_e_foreign~assttrn_baseline"},
        {"repair_repository": "owner/other"},
        {"repair_pr_number": 6020},
        {"repair_pr_number": True},
        {"repair_head_branch": "replacement-branch"},
        {"repair_base_branch": "release"},
        {"repair_head_sha": ""},
    ],
)
def test_missing_native_authority_stays_waiting_without_provider_reads(evidence, change):
    client, path, record, status, history = evidence
    _write(path, replace(record, **change))
    before = path.read_bytes()

    assert client.observe_review_feedback_readiness(TASK_ID, (IDENTITY,), TARGET) is CodexReviewFeedbackReadiness.WAITING

    status.assert_not_called()
    history.assert_not_called()
    assert path.read_bytes() == before


@pytest.mark.parametrize("damage", ["absent", "missing", "foreign-owner", "identity", "wrong-key", "extra-field", "scalar", "list", "json", "duplicate-key", "duplicate-field"])
def test_malformed_or_ambiguous_native_receipts_do_not_authorize(evidence, damage):
    client, path, record, status, history = evidence
    document = {_key(record): asdict(record)}
    if damage == "absent":
        path.unlink()
    elif damage == "missing":
        path.write_text("{}")
    elif damage == "foreign-owner":
        document[_key(record)]["task_id"] = "task_e_foreign"
        path.write_text(json.dumps(document))
    elif damage == "identity":
        document[_key(record)]["logical_identity"] = "different-feedback"
        path.write_text(json.dumps(document))
    elif damage == "wrong-key":
        path.write_text(json.dumps({"wrong-key": asdict(record)}))
    elif damage == "extra-field":
        document[_key(record)]["unexpected"] = True
        path.write_text(json.dumps(document))
    elif damage == "scalar":
        path.write_text(json.dumps({_key(record): None}))
    elif damage == "list":
        path.write_text("[]")
    elif damage == "json":
        path.write_text("{broken")
    elif damage == "duplicate-key":
        pair = json.dumps(_key(record)) + ":" + json.dumps(asdict(record))
        path.write_text("{" + pair + "," + pair + "}")
    else:
        serialized = json.dumps(document)
        path.write_text(serialized.replace('"status": "delivered"', '"status": "indeterminate", "status": "delivered"'))

    assert client.observe_review_feedback_readiness(TASK_ID, (IDENTITY,), TARGET) is CodexReviewFeedbackReadiness.WAITING
    status.assert_not_called()
    history.assert_not_called()


@pytest.mark.parametrize("same_owner", [True, False])
def test_unselected_indeterminate_receipt_blocks_only_its_owning_task(evidence, same_owner):
    client, path, record, status, _history = evidence
    uncertain = replace(record, logical_identity="another-request", status="indeterminate", task_id=TASK_ID if same_owner else "task_e_other")
    _write(path, record, uncertain)

    assert client.observe_review_feedback_readiness(TASK_ID, (IDENTITY,), TARGET) is (CodexReviewFeedbackReadiness.WAITING if same_owner else CodexReviewFeedbackReadiness.COMPLETED)
    assert status.call_count == (0 if same_owner else 1)


@pytest.mark.parametrize("state", [CloudTaskState.RUNNING, CloudTaskState.QUEUED, CloudTaskState.PAUSED, CloudTaskState.FAILED, CloudTaskState.UNKNOWN, "completed"])
def test_task_must_be_authoritatively_completed(evidence, state):
    client, _path, _record, status, history = evidence
    status.return_value = CloudTask(task_id=TASK_ID, state=state)

    assert client.observe_review_feedback_readiness(TASK_ID, (IDENTITY,), TARGET) is CodexReviewFeedbackReadiness.WAITING
    history.assert_not_called()


@pytest.mark.parametrize("task", [None, CloudTask(task_id="task_e_other", state=CloudTaskState.COMPLETED), {"task_id": TASK_ID, "state": "completed"}])
def test_task_observation_must_have_exact_typed_identity(evidence, task):
    client, _path, _record, status, history = evidence
    status.return_value = task

    assert client.observe_review_feedback_readiness(TASK_ID, (IDENTITY,), TARGET) is CodexReviewFeedbackReadiness.WAITING
    history.assert_not_called()


@pytest.mark.parametrize("history_kind", ["baseline-only", "running-after", "foreign-after", "missing-baseline", "missing-time"])
def test_old_or_unrelated_completion_cannot_stand_for_selected_followup(evidence, history_kind):
    client, _path, _record, _status, history = evidence
    turns = history.return_value
    if history_kind == "baseline-only":
        turns = turns[1:]
    elif history_kind == "running-after":
        turns = [replace(turns[0], status="running"), turns[1]]
    elif history_kind == "foreign-after":
        turns = [replace(turns[0], id="task_e_other~assttrn_after"), turns[1]]
    elif history_kind == "missing-baseline":
        turns = turns[:1]
    else:
        turns = [turns[0], replace(turns[1], created_at=None)]
    history.return_value = turns

    assert client.observe_review_feedback_readiness(TASK_ID, (IDENTITY,), TARGET) is CodexReviewFeedbackReadiness.WAITING


@pytest.mark.parametrize("completed", [None, "", BASELINE, "task_e_other~assttrn_after", True, f"{TASK_ID}~"])
def test_provider_completion_identity_cannot_be_missing_or_baseline(evidence, monkeypatch, completed):
    client, _path, _record, _status, _history = evidence
    monkeypatch.setattr(client.wham_client, "resolve_completed_assistant_turn_after", lambda *_args: completed)

    assert client.observe_review_feedback_readiness(TASK_ID, (IDENTITY,), TARGET) is CodexReviewFeedbackReadiness.WAITING


@pytest.mark.parametrize("change", ["owned-insert", "owned-edit", "owned-remove", "other-task"])
def test_rechecks_owning_receipts_after_provider_observations(evidence, change):
    client, path, record, status, _history = evidence

    def observe(_task_id):
        if change == "owned-insert":
            _write(path, record, replace(record, logical_identity="new-request"))
        elif change == "owned-edit":
            _write(path, replace(record, accepted_at=101))
        elif change == "owned-remove":
            path.write_text("{}")
        else:
            _write(path, record, replace(record, task_id="task_e_other", logical_identity="new-request"))
        return CloudTask(task_id=TASK_ID, state=CloudTaskState.COMPLETED)

    status.side_effect = observe
    assert client.observe_review_feedback_readiness(TASK_ID, (IDENTITY,), TARGET) is (CodexReviewFeedbackReadiness.COMPLETED if change == "other-task" else CodexReviewFeedbackReadiness.WAITING)


@pytest.mark.parametrize("boundary", ["status", "history"])
def test_provider_observation_failure_defers_without_receipt_changes(evidence, boundary):
    client, path, _record, status, history = evidence
    before = path.read_bytes()
    (status if boundary == "status" else history).side_effect = RuntimeError("provider unavailable")

    assert client.observe_review_feedback_readiness(TASK_ID, (IDENTITY,), TARGET) is CodexReviewFeedbackReadiness.WAITING
    assert path.read_bytes() == before


@pytest.mark.parametrize("identities", [(), (IDENTITY, IDENTITY), ("",)])
def test_empty_or_ambiguous_selection_never_reads_provider(evidence, identities):
    client, _path, _record, status, history = evidence

    assert client.observe_review_feedback_readiness(TASK_ID, identities, TARGET) is CodexReviewFeedbackReadiness.WAITING
    status.assert_not_called()
    history.assert_not_called()
