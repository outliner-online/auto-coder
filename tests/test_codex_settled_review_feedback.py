"""Read-only recovery when accepted historical feedback disappeared from WHAM."""

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, replace
from unittest.mock import MagicMock

import pytest

from auto_coder.cloud_task_client_base import CloudTask, CloudTaskState
from auto_coder.codex_cloud_client import CodexCloudClient, CodexReviewFeedbackReadiness, PendingCodexFollowUp
from auto_coder.codex_wham_client import CodexWhamClient, WhamTask, WhamTurn
from auto_coder.pr_repair import ExistingPrRepairTarget

REPOSITORY = "owner/repo"
TASK_ID = "task_e_6027"
TARGET = ExistingPrRepairTarget(REPOSITORY, 6027, "issue-5984", "main", "b" * 40)
INITIAL_USER = f"{TASK_ID}~usertrn_initial"
BASELINE = f"{TASK_ID}~assttrn_baseline"
CURRENT_USER = f"{TASK_ID}~usertrn_current"
CURRENT_ASSISTANT = f"{TASK_ID}~assttrn_current"
OMITTED_BASELINE = f"{TASK_ID}~assttrn_omitted"


def _key(record):
    return hashlib.sha256(f"{record.task_id}\0{record.logical_identity}".encode()).hexdigest()


def _write(path, records):
    path.write_text(json.dumps({_key(record): asdict(record) for record in records}), encoding="utf-8")


def _turn(identity, role, parent, children, created_at, text=""):
    inner = {"id": identity, "previous_turn_id": parent}
    if role == "user":
        inner["input_items"] = [{"type": "message", "role": "user", "content": [{"content_type": "text", "text": text}]}]
    raw = {"id": identity, "parent": parent, "children": children, "turn": inner}
    return WhamTurn(identity, role, "completed", str(created_at), raw)


@pytest.fixture
def settled_evidence(tmp_path, monkeypatch):
    path = tmp_path / "codex_followups.json"
    records = [
        PendingCodexFollowUp(
            task_id=TASK_ID,
            logical_identity=f"{REPOSITORY}#6027:codex-cloud:{TASK_ID}:finding-{number}",
            status="delivered",
            pre_send_turn_id=OMITTED_BASELINE,
            accepted_at=200,
            message_fingerprint="f" * 64,
            repair_repository=REPOSITORY,
            repair_pr_number=6027,
            repair_head_branch=TARGET.head_branch,
            repair_base_branch=TARGET.base_branch,
            repair_head_sha="a" * 40,
        )
        for number in range(3)
    ]
    # The POST returns after the assistant starts; only missing history needs
    # the historical ordering guard, never this proven retained baseline.
    records.append(replace(records[0], logical_identity="latest-finding", pre_send_turn_id=BASELINE, accepted_at=301, message_fingerprint=hashlib.sha256(b"Latest correction").hexdigest()))
    _write(path, records)
    turns = [
        _turn(INITIAL_USER, "user", None, [BASELINE], 90, "Initial implementation"),
        _turn(BASELINE, "assistant", INITIAL_USER, [CURRENT_USER], 100),
        _turn(CURRENT_USER, "user", BASELINE, [CURRENT_ASSISTANT], 299, "Latest correction"),
        _turn(CURRENT_ASSISTANT, "assistant", CURRENT_USER, [], 300),
    ]
    current = WhamTask(id=TASK_ID, status="completed", current_user_turn=turns[2], current_assistant_turn=turns[3], latest_turn_status="completed")
    client = object.__new__(CodexCloudClient)
    client.repo_name = REPOSITORY
    client.wham_client = CodexWhamClient()
    status = MagicMock(return_value=CloudTask(task_id=TASK_ID, state=CloudTaskState.COMPLETED))
    history = MagicMock(return_value=turns)
    details = MagicMock(return_value=current)
    monkeypatch.setattr(client, "get_task", status)
    monkeypatch.setattr(client, "send_followup", MagicMock())
    monkeypatch.setattr(client.wham_client, "get_task_turns", history)
    monkeypatch.setattr(client.wham_client, "get_task", details)
    monkeypatch.setattr(client.wham_client, "send_follow_up", MagicMock())
    monkeypatch.setattr("auto_coder.codex_cloud_client._codex_followup_state_path", lambda _repository: path)
    return client, path, records, status, history, details, current


def _observe(evidence):
    client, _path, records, *_rest = evidence
    return client.observe_review_feedback_readiness(TASK_ID, tuple(record.logical_identity for record in records), TARGET)


def test_pruned_historical_batch_admits_only_current_task_observation(settled_evidence):
    client, path, records, status, history, details, _current = settled_evidence
    before = path.read_bytes()
    modified = path.stat().st_mtime_ns

    assert _observe(settled_evidence) is CodexReviewFeedbackReadiness.CURRENT_TASK_SETTLED

    assert records[-1].accepted_at > float(history.return_value[-1].created_at)
    assert all(turn.id != OMITTED_BASELINE for turn in history.return_value)
    status.assert_called_once_with(TASK_ID)
    assert details.call_count == 2
    assert history.call_args.kwargs == {"require_complete": True}
    client.send_followup.assert_not_called()
    client.wham_client.send_follow_up.assert_not_called()
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == modified
    assert all(record["completed_turn_id"] == "" for record in json.loads(before).values())


@pytest.mark.parametrize("accepted_at", [0, -1, None, "200", True, float("nan"), float("inf"), float("-inf"), 300, 301])
def test_omitted_request_requires_real_prior_acceptance(settled_evidence, accepted_at):
    _client, path, records, *_rest = settled_evidence
    _write(path, [replace(records[0], accepted_at=accepted_at), *records[1:]])
    before = path.read_bytes()

    assert _observe(settled_evidence) is CodexReviewFeedbackReadiness.WAITING
    assert path.read_bytes() == before


def test_retained_baseline_without_completed_successor_still_waits(settled_evidence):
    _client, path, records, *_rest = settled_evidence
    _write(path, [*records[:-1], replace(records[-1], pre_send_turn_id=CURRENT_ASSISTANT)])

    assert _observe(settled_evidence) is CodexReviewFeedbackReadiness.WAITING


@pytest.mark.parametrize("same_owner", [True, False])
def test_unknown_delivery_blocks_only_owning_task_before_provider_reads(settled_evidence, same_owner):
    _client, path, records, status, history, details, _current = settled_evidence
    uncertain = replace(records[0], logical_identity="unconfirmed", task_id=TASK_ID if same_owner else "task_e_other", status="indeterminate")
    _write(path, [*records, uncertain])

    assert _observe(settled_evidence) is (CodexReviewFeedbackReadiness.WAITING if same_owner else CodexReviewFeedbackReadiness.CURRENT_TASK_SETTLED)
    if same_owner:
        status.assert_not_called()
        history.assert_not_called()
        details.assert_not_called()


@pytest.mark.parametrize(
    "damage",
    ["absent", "foreign-task", "no-assistant", "foreign-assistant", "no-user", "foreign-user", "current-running", "latest-running", "queued-user", "current-not-in-graph", "current-time-changed", "current-user-time-changed"],
)
def test_current_native_authority_is_required(settled_evidence, damage):
    _client, _path, _records, _status, _history, details, current = settled_evidence
    if damage == "absent":
        changed = None
    elif damage == "foreign-task":
        changed = replace(current, id="task_e_other")
    elif damage == "no-assistant":
        changed = replace(current, current_assistant_turn=None)
    elif damage == "foreign-assistant":
        changed = replace(current, current_assistant_turn=replace(current.current_assistant_turn, id="task_e_other~assttrn_current"))
    elif damage == "no-user":
        changed = replace(current, current_user_turn=None)
    elif damage == "foreign-user":
        changed = replace(current, current_user_turn=replace(current.current_user_turn, id="task_e_other~usertrn_current"))
    elif damage == "current-running":
        changed = replace(current, current_assistant_turn=replace(current.current_assistant_turn, status="in_progress"))
    elif damage == "latest-running":
        changed = replace(current, latest_turn_status="in_progress")
    elif damage == "queued-user":
        changed = replace(current, current_user_turn=replace(current.current_user_turn, status="queued"))
    elif damage == "current-not-in-graph":
        changed = replace(current, current_assistant_turn=replace(current.current_assistant_turn, id=f"{TASK_ID}~assttrn_unobserved"))
    elif damage == "current-time-changed":
        changed = replace(current, current_assistant_turn=replace(current.current_assistant_turn, created_at="301"))
    else:
        changed = replace(current, current_user_turn=replace(current.current_user_turn, created_at="301"))
    details.return_value = changed

    assert _observe(settled_evidence) is CodexReviewFeedbackReadiness.WAITING


@pytest.mark.parametrize("damage", ["empty", "duplicate", "foreign", "unknown-role", "missing-parent", "contradictory-parent", "contradictory-children", "cycle", "unreadable-user", "other-active-assistant", "wrong-current-parent", "ambiguous-current-response"])
def test_current_graph_must_be_complete_coherent_and_idle(settled_evidence, damage):
    _client, _path, _records, _status, history, _details, _current = settled_evidence
    turns = deepcopy(history.return_value)
    if damage == "empty":
        turns = []
    elif damage == "duplicate":
        turns.append(turns[-1])
    elif damage == "foreign":
        turns[0] = replace(turns[0], id="task_e_other~usertrn_initial")
    elif damage == "unknown-role":
        turns[0] = replace(turns[0], role="unknown")
    elif damage == "missing-parent":
        turns[-1].raw_data["parent"] = f"{TASK_ID}~usertrn_missing"
        turns[-1].raw_data["turn"]["previous_turn_id"] = f"{TASK_ID}~usertrn_missing"
    elif damage == "contradictory-parent":
        turns[-1].raw_data["parent"] = INITIAL_USER
    elif damage == "contradictory-children":
        turns[-2].raw_data["children"] = []
    elif damage == "cycle":
        turns[0].raw_data["parent"] = CURRENT_ASSISTANT
        turns[0].raw_data["turn"]["previous_turn_id"] = CURRENT_ASSISTANT
        turns[-1].raw_data["children"] = [INITIAL_USER]
    elif damage == "unreadable-user":
        turns[-2].raw_data["turn"].pop("input_items")
    elif damage == "other-active-assistant":
        turns[1] = replace(turns[1], status="in_progress")
    elif damage == "wrong-current-parent":
        turns[-1].raw_data["parent"] = INITIAL_USER
        turns[-1].raw_data["turn"]["previous_turn_id"] = INITIAL_USER
        turns[0].raw_data["children"].append(CURRENT_ASSISTANT)
        turns[-2].raw_data["children"] = []
    else:
        sibling = f"{TASK_ID}~assttrn_sibling"
        turns[-2].raw_data["children"].append(sibling)
        turns.append(_turn(sibling, "assistant", CURRENT_USER, [], 300))
    history.return_value = turns

    assert _observe(settled_evidence) is CodexReviewFeedbackReadiness.WAITING


@pytest.mark.parametrize("created_at", [None, "nan", "inf", "-inf", "invalid"])
def test_current_turn_without_finite_timestamp_cannot_order_historical_receipts(settled_evidence, created_at):
    _client, _path, _records, _status, history, details, current = settled_evidence
    assistant = replace(current.current_assistant_turn, created_at=created_at)
    history.return_value = [*history.return_value[:-1], assistant]
    details.return_value = replace(current, current_assistant_turn=assistant)

    assert _observe(settled_evidence) is CodexReviewFeedbackReadiness.WAITING


def test_complete_history_failure_cannot_reuse_partial_history(settled_evidence):
    _client, _path, _records, _status, history, _details, _current = settled_evidence
    turns = history.return_value

    def read(_task_id, *, require_complete=False):
        if require_complete:
            raise ValueError("Incomplete provider history")
        return turns

    history.side_effect = read
    assert _observe(settled_evidence) is CodexReviewFeedbackReadiness.WAITING


@pytest.mark.parametrize("change", ["task", "assistant", "user", "latest", "missing"])
def test_current_native_observation_is_rechecked(settled_evidence, change):
    _client, _path, _records, _status, _history, details, current = settled_evidence
    changed = current
    if change == "task":
        changed = replace(current, id="task_e_other")
    elif change == "assistant":
        changed = replace(current, current_assistant_turn=replace(current.current_assistant_turn, id=f"{TASK_ID}~assttrn_next"))
    elif change == "user":
        changed = replace(current, current_user_turn=replace(current.current_user_turn, id=f"{TASK_ID}~usertrn_next"))
    elif change == "latest":
        changed = replace(current, latest_turn_status="pending")
    else:
        changed = None
    details.side_effect = [current, changed]

    assert _observe(settled_evidence) is CodexReviewFeedbackReadiness.WAITING


@pytest.mark.parametrize("change", ["owned-insert", "owned-edit", "owned-remove", "other-task"])
def test_receipt_stability_is_rechecked_after_current_state(settled_evidence, change):
    _client, path, records, _status, _history, details, current = settled_evidence
    reads = 0

    def read(_task_id):
        nonlocal reads
        reads += 1
        if reads == 2:
            if change == "owned-insert":
                _write(path, [*records, replace(records[0], logical_identity="new-request")])
            elif change == "owned-edit":
                _write(path, [replace(records[0], accepted_at=201), *records[1:]])
            elif change == "owned-remove":
                _write(path, records[1:])
            else:
                _write(path, [*records, replace(records[0], task_id="task_e_other", logical_identity="new-request")])
        return current

    details.side_effect = read
    assert _observe(settled_evidence) is (CodexReviewFeedbackReadiness.CURRENT_TASK_SETTLED if change == "other-task" else CodexReviewFeedbackReadiness.WAITING)
