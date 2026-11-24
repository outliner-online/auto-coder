"""Tests for complete-graph WHAM non-delivery proof and safe settlement."""

from __future__ import annotations

import hashlib
import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from auto_coder.cli_commands_pr_repair import pr_repair_group
from auto_coder.cloud_run import CloudRun, CloudRunRepository
from auto_coder.codex_cloud_client import (
    CodexCloudClient,
    _codex_followup_state_path,
    reconcile_proven_non_delivered_followups,
)
from auto_coder.codex_followup_causality import (
    NON_DELIVERY_EVIDENCE_PREFIX,
    followup_non_delivery_evidence,
)
from auto_coder.codex_pr_attribution import CodexPrAttributionRepository
from auto_coder.codex_pr_recovery import CodexPRRecoveryStore
from auto_coder.codex_retirement_observer import collect_codex_retirement_observation
from auto_coder.codex_wham_client import (
    CodexWhamClient,
    FollowUpDeliveryOutcome,
    FollowUpDeliveryResult,
    WhamTask,
    WhamTurn,
)
from auto_coder.codex_work_accounting import (
    CodexSendProvenance,
    CodexWorkAccounting,
    CodexWorkPhase,
    WorkAccountingStatus,
)
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.pr_processor import _cloud_review_repair_state_path
from auto_coder.pr_repair import ExistingPrRepairTarget, build_existing_pr_repair_prompt
from tests.test_legacy_review_source_evidence import ordinary  # noqa: F401
from tests.test_scoped_review_followup import HEAD, PR, REPO, TASK

TASK_ID = "task_e_0123456789abcdef0123456789abcdef"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _user_turn(
    task_id: str,
    turn_suffix: str,
    text: str,
    *,
    parent: str | None,
    children: list[str],
    status: str = "completed",
    malformed_input: bool = False,
) -> WhamTurn:
    turn_id = f"{task_id}~{turn_suffix}"
    input_items = [{"type": "message", "role": "user", "content": [{"content_type": "image", "url": "https://example.invalid/x.png"}]}] if malformed_input else [{"type": "message", "role": "user", "content": [{"content_type": "text", "text": text}]}]
    raw = {
        "id": turn_id,
        "parent": parent,
        "children": list(children),
        "turn": {
            "id": turn_id,
            "previous_turn_id": parent,
            "role": "user",
            "turn_status": status,
            "input_items": input_items,
        },
    }
    return WhamTurn(id=turn_id, status=status, role="user", raw_data=raw)


def _assistant_turn(
    task_id: str,
    turn_suffix: str,
    *,
    parent: str | None,
    children: list[str],
    status: str = "completed",
) -> WhamTurn:
    turn_id = f"{task_id}~{turn_suffix}"
    raw = {
        "id": turn_id,
        "parent": parent,
        "children": list(children),
        "turn": {
            "id": turn_id,
            "previous_turn_id": parent,
            "role": "assistant",
            "turn_status": status,
        },
    }
    return WhamTurn(id=turn_id, status=status, role="assistant", raw_data=raw)


def _complete_terminal_graph(
    task_id: str = TASK_ID,
    *,
    user1_text: str = "initial prompt",
    user2_text: str = "later delivered follow-up",
    assistant2_status: str = "completed",
) -> tuple[list[WhamTurn], str, WhamTurn]:
    u1_id = f"{task_id}~usertrn_1"
    a1_id = f"{task_id}~assttrn_1"
    u2_id = f"{task_id}~usertrn_2"
    a2_id = f"{task_id}~assttrn_2"
    u1 = _user_turn(task_id, "usertrn_1", user1_text, parent=None, children=[a1_id])
    a1 = _assistant_turn(task_id, "assttrn_1", parent=u1_id, children=[u2_id], status="completed")
    u2 = _user_turn(task_id, "usertrn_2", user2_text, parent=a1_id, children=[a2_id])
    a2 = _assistant_turn(task_id, "assttrn_2", parent=u2_id, children=[], status=assistant2_status)
    return [u1, a1, u2, a2], a1_id, a2


def test_followup_non_delivery_evidence_proves_complete_terminal_graph_with_later_successor():
    turns, baseline_id, current_turn = _complete_terminal_graph()
    missing_fp = _sha256("never delivered 429 prompt")

    first = followup_non_delivery_evidence(
        TASK_ID,
        baseline_id,
        missing_fp,
        turns,
        current_assistant_turn_id=current_turn.id,
        current_assistant_turn_status=current_turn.status,
    )
    second = followup_non_delivery_evidence(
        TASK_ID,
        baseline_id,
        missing_fp,
        turns,
        current_assistant_turn_id=current_turn.id,
        current_assistant_turn_status=current_turn.status,
    )

    assert first is not None
    assert first == second
    assert first.startswith(NON_DELIVERY_EVIDENCE_PREFIX)
    suffix = first[len(NON_DELIVERY_EVIDENCE_PREFIX) :]
    assert len(suffix) == 24
    assert all(ch in "0123456789abcdef" for ch in suffix)


def test_followup_non_delivery_evidence_rejects_matching_user_turn():
    turns, baseline_id, current_turn = _complete_terminal_graph(user2_text="the follow-up actually arrived")
    delivered_fp = _sha256("the follow-up actually arrived")

    evidence = followup_non_delivery_evidence(
        TASK_ID,
        baseline_id,
        delivered_fp,
        turns,
        current_assistant_turn_id=current_turn.id,
        current_assistant_turn_status=current_turn.status,
    )
    assert evidence is None


@pytest.mark.parametrize("active_status", ["running", "queued", "in_progress", "pending"])
def test_followup_non_delivery_evidence_rejects_non_terminal_turns(active_status):
    turns, baseline_id, current_turn = _complete_terminal_graph(assistant2_status=active_status)
    missing_fp = _sha256("never delivered 429 prompt")

    assert (
        followup_non_delivery_evidence(
            TASK_ID,
            baseline_id,
            missing_fp,
            turns,
            current_assistant_turn_id=current_turn.id,
            current_assistant_turn_status="completed",
        )
        is None
    )

    terminal_turns, baseline_id, terminal_current = _complete_terminal_graph(assistant2_status="completed")
    assert (
        followup_non_delivery_evidence(
            TASK_ID,
            baseline_id,
            missing_fp,
            terminal_turns,
            current_assistant_turn_id=terminal_current.id,
            current_assistant_turn_status=active_status,
        )
        is None
    )


def test_followup_non_delivery_evidence_rejects_unreadable_user_text():
    u1_id = f"{TASK_ID}~usertrn_1"
    a1_id = f"{TASK_ID}~assttrn_1"
    u2_id = f"{TASK_ID}~usertrn_2"
    a2_id = f"{TASK_ID}~assttrn_2"
    turns = [
        _user_turn(TASK_ID, "usertrn_1", "initial", parent=None, children=[a1_id]),
        _assistant_turn(TASK_ID, "assttrn_1", parent=u1_id, children=[u2_id]),
        _user_turn(TASK_ID, "usertrn_2", "later", parent=a1_id, children=[a2_id], malformed_input=True),
        _assistant_turn(TASK_ID, "assttrn_2", parent=u2_id, children=[]),
    ]
    assert (
        followup_non_delivery_evidence(
            TASK_ID,
            a1_id,
            _sha256("missing"),
            turns,
            current_assistant_turn_id=a2_id,
            current_assistant_turn_status="completed",
        )
        is None
    )


@pytest.mark.parametrize(
    "defect",
    [
        "children_mismatch",
        "orphan_parent",
        "cycle",
        "no_successor",
        "foreign_turn_id",
        "duplicate_turn_id",
        "invalid_fingerprint",
        "missing_current_turn",
    ],
)
def test_followup_non_delivery_evidence_fails_closed_on_graph_defects(defect):
    turns, baseline_id, current_turn = _complete_terminal_graph()
    fingerprint = _sha256("never delivered")
    current_id = current_turn.id

    if defect == "children_mismatch":
        turns[1].raw_data["children"] = []
    elif defect == "orphan_parent":
        turns[2].raw_data["parent"] = f"{TASK_ID}~assttrn_missing"
        turns[2].raw_data["turn"]["previous_turn_id"] = f"{TASK_ID}~assttrn_missing"
    elif defect == "cycle":
        turns[0].raw_data["parent"] = turns[3].id
        turns[0].raw_data["turn"]["previous_turn_id"] = turns[3].id
        turns[3].raw_data["children"] = [turns[0].id]
    elif defect == "no_successor":
        turns = [
            _user_turn(TASK_ID, "usertrn_1", "initial", parent=None, children=[baseline_id]),
            _assistant_turn(TASK_ID, "assttrn_1", parent=f"{TASK_ID}~usertrn_1", children=[]),
        ]
        current_id = baseline_id
    elif defect == "foreign_turn_id":
        other_task = "task_e_ffffffffffffffffffffffffffffffff"
        turns[3] = _assistant_turn(other_task, "assttrn_2", parent=turns[2].id, children=[])
        turns[2].raw_data["children"] = [turns[3].id]
        current_id = turns[3].id
    elif defect == "duplicate_turn_id":
        turns.append(turns[-1])
    elif defect == "invalid_fingerprint":
        fingerprint = "not-a-sha256"
    elif defect == "missing_current_turn":
        current_id = f"{TASK_ID}~assttrn_999"

    assert (
        followup_non_delivery_evidence(
            TASK_ID,
            baseline_id,
            fingerprint,
            turns,
            current_assistant_turn_id=current_id,
            current_assistant_turn_status="completed",
        )
        is None
    )


def test_wham_prove_follow_up_not_delivered_requires_complete_turns_and_real_types():
    turns, baseline_id, current_turn = _complete_terminal_graph()
    missing_fp = _sha256("missing follow-up")
    wham = CodexWhamClient()
    live_task = WhamTask(id=TASK_ID, status="completed", current_assistant_turn=current_turn)

    with patch.object(wham, "get_task", return_value=live_task) as get_task, patch.object(wham, "get_task_turns", return_value=turns) as get_turns:
        evidence = wham.prove_follow_up_not_delivered(TASK_ID, baseline_id, missing_fp)
        assert evidence is not None and evidence.startswith(NON_DELIVERY_EVIDENCE_PREFIX)
        get_task.assert_called_once_with(TASK_ID)
        get_turns.assert_called_once_with(TASK_ID, require_complete=True)

    # Incomplete pagination (require_complete=True raises ValueError) fails closed.
    with patch.object(wham, "get_task", return_value=live_task), patch.object(wham, "get_task_turns", side_effect=ValueError("has_more")):
        assert wham.prove_follow_up_not_delivered(TASK_ID, baseline_id, missing_fp) is None

    # Wrong task ID or missing current_assistant_turn fails closed.
    wrong_task = WhamTask(id="task_e_ffffffffffffffffffffffffffffffff", status="completed", current_assistant_turn=current_turn)
    with patch.object(wham, "get_task", return_value=wrong_task), patch.object(wham, "get_task_turns", return_value=turns):
        assert wham.prove_follow_up_not_delivered(TASK_ID, baseline_id, missing_fp) is None

    # Mocked non-WhamTask objects fail closed.
    with patch.object(wham, "get_task", return_value=MagicMock(id=TASK_ID)), patch.object(wham, "get_task_turns", return_value=turns):
        assert wham.prove_follow_up_not_delivered(TASK_ID, baseline_id, missing_fp) is None


def test_settle_proven_non_delivery_settles_legacy_and_provenance_operations(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(tmp_path / "runtime"))
    slots = ImplementationSlotRepository("acme/widgets", 1)
    owner = ImplementationOwner("issue", 42)
    assert slots.reserve(owner)
    incarnation = slots.owner_incarnation(owner)
    assert incarnation is not None
    accounting = CodexWorkAccounting(slots)
    accounting.initialize_fresh(owner, incarnation)

    baseline = f"{TASK_ID}~assttrn_1"
    fp_legacy = _sha256("legacy prompt")
    fp_prov = _sha256("provenance prompt")
    evidence = f"{NON_DELIVERY_EVIDENCE_PREFIX}{'a' * 24}"

    # 1. Legacy DELIVERY_UNKNOWN follow-up (accepted=True, task_id set, no send_provenance).
    legacy_prov = CodexSendProvenance(
        fingerprint=fp_legacy,
        logical_identities=("finding-1", "finding-2"),
        outcome="reserved",
    )
    accounting.register(
        owner,
        incarnation,
        logical_operation_id="follow-up:legacy",
        kind="follow-up",
        source_request_id="finding-1,finding-2",
        causal_baseline=baseline,
        task_id=TASK_ID,
        send_provenance=legacy_prov,
    )
    accounting.transition(
        owner,
        incarnation,
        "follow-up:legacy",
        CodexWorkPhase.DELIVERY_UNKNOWN,
        evidence_id="wham:429",
        evidence_causal_baseline=baseline,
        evidence_source_request_id="finding-1,finding-2",
        task_id=TASK_ID,
    )
    with slots._state_lock():
        records = slots._read()
        records[owner.key][CodexWorkAccounting._FIELD]["operations"]["follow-up:legacy"].pop("send_provenance", None)
        slots._write(records)

    settled_snapshot = accounting.settle_proven_non_delivery(
        owner,
        incarnation,
        "follow-up:legacy",
        evidence_id=evidence,
        source_request_id="finding-1,finding-2",
        causal_baseline=baseline,
        task_id=TASK_ID,
        request_fingerprint=fp_legacy,
    )
    assert settled_snapshot.accounting_status is WorkAccountingStatus.COMPLETE
    legacy_op = next(op for op in settled_snapshot.operations if op.logical_operation_id == "follow-up:legacy")
    assert legacy_op.phase is CodexWorkPhase.SETTLED
    assert legacy_op.settled is True
    assert legacy_op.accepted is False
    assert legacy_op.definite_non_delivery is True
    assert legacy_op.task_id is None
    assert legacy_op.causal_baseline == baseline
    assert legacy_op.settlement_evidence_id == evidence
    assert legacy_op.send_provenance is None

    # Re-reading from disk parses cleanly through _parse_operation.
    reloaded = accounting.snapshot(owner, incarnation)
    assert reloaded.accounting_status is WorkAccountingStatus.COMPLETE
    assert reloaded.releasable is True

    # 2. Provenance-bearing DELIVERY_UNKNOWN follow-up updates send_provenance.outcome to not_delivered.
    provenance = CodexSendProvenance(
        fingerprint=fp_prov,
        logical_identities=("finding-3",),
        pr_number=100,
        head_sha=HEAD,
        head_branch="work",
        base_branch="main",
        outcome="reserved",
    )
    accounting.register(
        owner,
        incarnation,
        logical_operation_id="follow-up:prov",
        kind="follow-up",
        source_request_id="finding-3",
        causal_baseline=baseline,
        task_id=TASK_ID,
        send_provenance=provenance,
    )
    accounting.record_send_outcome(
        owner,
        incarnation,
        "follow-up:prov",
        source_request_id="finding-3",
        causal_baseline=baseline,
        task_id=TASK_ID,
        outcome="unknown",
    )
    accounting.transition(
        owner,
        incarnation,
        "follow-up:prov",
        CodexWorkPhase.DELIVERY_UNKNOWN,
        evidence_id="wham:429",
        evidence_causal_baseline=baseline,
        evidence_source_request_id="finding-3",
        task_id=TASK_ID,
    )
    prov_snapshot = accounting.settle_proven_non_delivery(
        owner,
        incarnation,
        "follow-up:prov",
        evidence_id=evidence,
        source_request_id="finding-3",
        causal_baseline=baseline,
        task_id=TASK_ID,
        request_fingerprint=fp_prov,
    )
    prov_op = next(op for op in prov_snapshot.operations if op.logical_operation_id == "follow-up:prov")
    assert prov_op.phase is CodexWorkPhase.SETTLED
    assert prov_op.accepted is False
    assert prov_op.definite_non_delivery is True
    assert prov_op.send_provenance is not None
    assert prov_op.send_provenance.outcome == "not_delivered"


def test_settle_proven_non_delivery_rejects_accepted_mismatched_or_unproven(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(tmp_path / "runtime"))
    slots = ImplementationSlotRepository("acme/widgets", 1)
    owner = ImplementationOwner("issue", 43)
    assert slots.reserve(owner)
    incarnation = slots.owner_incarnation(owner)
    assert incarnation is not None
    accounting = CodexWorkAccounting(slots)
    accounting.initialize_fresh(owner, incarnation)

    baseline = f"{TASK_ID}~assttrn_1"
    fp = _sha256("accepted follow-up")
    evidence = f"{NON_DELIVERY_EVIDENCE_PREFIX}{'b' * 24}"

    # Accepted follow-up must never be reclassified as non-delivered.
    accounting.register(
        owner,
        incarnation,
        logical_operation_id="follow-up:accepted",
        kind="follow-up",
        source_request_id="finding-a",
        causal_baseline=baseline,
        task_id=TASK_ID,
        send_provenance=CodexSendProvenance(fingerprint=fp, logical_identities=("finding-a",), outcome="reserved"),
    )
    accounting.transition(
        owner,
        incarnation,
        "follow-up:accepted",
        CodexWorkPhase.ACCEPTED,
        evidence_id="wham:200",
        evidence_causal_baseline=baseline,
        evidence_source_request_id="finding-a",
        task_id=TASK_ID,
    )
    with pytest.raises(ValueError, match="reserved or delivery_unknown"):
        accounting.settle_proven_non_delivery(
            owner,
            incarnation,
            "follow-up:accepted",
            evidence_id=evidence,
            source_request_id="finding-a",
            causal_baseline=baseline,
            task_id=TASK_ID,
            request_fingerprint=fp,
        )

    # Initial creation op must never be settled by follow-up non-delivery evidence.
    accounting.register(
        owner,
        incarnation,
        logical_operation_id="initial:1",
        kind="initial-creation",
        source_request_id="issue:43",
        causal_baseline=None,
        task_id=TASK_ID,
    )
    with pytest.raises(ValueError, match="Only follow-up operations"):
        accounting.settle_proven_non_delivery(
            owner,
            incarnation,
            "initial:1",
            evidence_id=evidence,
            source_request_id="issue:43",
            causal_baseline=baseline,
            task_id=TASK_ID,
            request_fingerprint=fp,
        )

    # Delivery-unknown follow-up still rejects bad evidence prefix and identity mismatches.
    accounting.register(
        owner,
        incarnation,
        logical_operation_id="follow-up:unknown",
        kind="follow-up",
        source_request_id="finding-u",
        causal_baseline=baseline,
        task_id=TASK_ID,
        send_provenance=CodexSendProvenance(fingerprint=fp, logical_identities=("finding-u",), outcome="reserved"),
    )
    with pytest.raises(ValueError, match="non-delivery evidence identity"):
        accounting.settle_proven_non_delivery(
            owner,
            incarnation,
            "follow-up:unknown",
            evidence_id="manual-override",
            source_request_id="finding-u",
            causal_baseline=baseline,
            task_id=TASK_ID,
            request_fingerprint=fp,
        )
    with pytest.raises(ValueError, match="does not match"):
        accounting.settle_proven_non_delivery(
            owner,
            incarnation,
            "follow-up:unknown",
            evidence_id=evidence,
            source_request_id="finding-u",
            causal_baseline=baseline,
            task_id=TASK_ID,
            request_fingerprint=_sha256("different prompt"),
        )


def test_retirement_observer_accepts_settled_proven_non_delivery_follow_up(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(tmp_path / "runtime"))
    repo = "acme/widgets"
    slots = ImplementationSlotRepository(repo, 1)
    owner = ImplementationOwner("issue", 44)
    assert slots.reserve(owner)
    incarnation = slots.owner_incarnation(owner)
    assert incarnation is not None

    accounting = CodexWorkAccounting(slots)
    accounting.initialize_fresh(owner, incarnation)
    slots.record_implementation_pr(owner, 200)

    runs = CloudRunRepository(repo)
    run = CloudRun(
        repo_name=repo,
        issue_number=44,
        attempt=1,
        provider="codex-cloud",
        task_id=TASK_ID,
        pull_request_numbers=[200],
    )
    runs.save(run)

    accounting.register(
        owner,
        incarnation,
        logical_operation_id=f"initial:{TASK_ID}",
        kind="initial-creation",
        source_request_id="issue:44",
        task_id=TASK_ID,
    )
    accounting.transition(
        owner,
        incarnation,
        f"initial:{TASK_ID}",
        CodexWorkPhase.SETTLED,
        evidence_id="pr:200",
        evidence_source_request_id="issue:44",
        task_id=TASK_ID,
        execution_complete=True,
        publication_complete=True,
        tracking_complete=True,
    )

    turns, baseline_id, current_turn = _complete_terminal_graph()
    fp = _sha256("never delivered 429")
    evidence = followup_non_delivery_evidence(
        TASK_ID,
        baseline_id,
        fp,
        turns,
        current_assistant_turn_id=current_turn.id,
        current_assistant_turn_status=current_turn.status,
    )
    assert evidence is not None
    accounting.register(
        owner,
        incarnation,
        logical_operation_id="follow-up:429",
        kind="follow-up",
        source_request_id="comment:1",
        causal_baseline=baseline_id,
        task_id=TASK_ID,
        send_provenance=CodexSendProvenance(fingerprint=fp, logical_identities=("comment:1",), outcome="reserved"),
    )
    accounting.settle_proven_non_delivery(
        owner,
        incarnation,
        "follow-up:429",
        evidence_id=evidence,
        source_request_id="comment:1",
        causal_baseline=baseline_id,
        task_id=TASK_ID,
        request_fingerprint=fp,
    )
    snapshot = accounting.snapshot(owner, incarnation)

    wham = MagicMock(spec=CodexWhamClient)
    wham.get_task.return_value = WhamTask(id=TASK_ID, status="completed", current_assistant_turn=current_turn)
    wham.get_task_turns.return_value = turns

    github = MagicMock()
    github.get_connected_prs.return_value = [200]
    github.get_open_pull_requests_strict.return_value = []
    github.get_pull_request_metadata_strict.return_value = {"number": 200, "state": "closed", "merged": True}

    observation = collect_codex_retirement_observation(
        repo,
        owner,
        incarnation,
        slots,
        snapshot,
        runs,
        github,
        wham=wham,
        attributions=CodexPrAttributionRepository(repo),
        recovery=CodexPRRecoveryStore(),
    )
    assert observation.incomplete_reasons == ()
    assert observation.conclusive is True


def test_send_followup_auto_settles_proven_non_delivered_legacy_overlap_and_preserves_other_records(ordinary):
    run, old, _new, wham, _github, incarnation = ordinary
    # Produce an ambiguous follow-up, then strip send_provenance to reproduce
    # the exact pre-provenance legacy DELIVERY_UNKNOWN state.
    run((old,))
    slots = ImplementationSlotRepository(REPO, 1)
    owner = ImplementationOwner("issue", 9)
    with slots._state_lock():
        records = slots._read()
        for operation in records[owner.key][CodexWorkAccounting._FIELD]["operations"].values():
            operation.pop("send_provenance", None)
        slots._write(records)

    accounting = CodexWorkAccounting(slots)
    before_ops = accounting.snapshot(owner, incarnation).operations
    ambiguous = next(op for op in before_ops if op.kind == "follow-up")
    assert ambiguous.phase is CodexWorkPhase.DELIVERY_UNKNOWN
    assert ambiguous.send_provenance is None
    assert ambiguous.causal_baseline is not None
    assert ambiguous.request_fingerprint is not None

    # Add an unrelated delivered journal entry and an unrelated pending review
    # item to verify settlement touches ONLY the proven-non-delivered op.
    followup_path = _codex_followup_state_path(REPO)
    native_before = json.loads(followup_path.read_text(encoding="utf-8"))
    unrelated_key = hashlib.sha256(f"{TASK}\0unrelated-delivered".encode("utf-8")).hexdigest()
    native_before[unrelated_key] = {
        "task_id": TASK,
        "message_fingerprint": _sha256("unrelated delivered prompt"),
        "pre_send_turn_id": f"{TASK}~assttrn_0",
        "status": FollowUpDeliveryOutcome.DELIVERED.value,
        "logical_identity": "unrelated-delivered",
        "accepted_at": 1700000000.0,
    }
    followup_path.write_text(json.dumps(native_before, indent=2, sort_keys=True), encoding="utf-8")

    review_path = _cloud_review_repair_state_path(REPO)
    review_before = json.loads(review_path.read_text(encoding="utf-8"))
    review_before["pending_feedback"] = sorted(set(review_before.get("pending_feedback", [])) | {"unrelated-pending-id"})
    review_path.write_text(json.dumps(review_before, indent=2, sort_keys=True), encoding="utf-8")

    # Step 1: Without complete WHAM proof, a new follow-up remains blocked.
    wham.prove_follow_up_not_delivered.return_value = None
    target = ExistingPrRepairTarget(REPO, PR, "work-branch", "main", HEAD)
    next_message = build_existing_pr_repair_prompt(target, "Fix adversarial validation test oracle gap")
    blocked_client = CodexCloudClient(repo_name=REPO)
    assert not blocked_client.send_existing_pr_followup(TASK, next_message, target, ("adversarial-gap-1",))
    assert blocked_client.consume_followup_duplicate_guard(TASK, ("adversarial-gap-1",))

    # Step 2: Dry-run with real complete-graph proof mutates zero files.
    baseline_suffix = ambiguous.causal_baseline.split("~", 1)[1]
    u0_id = f"{TASK}~usertrn_0"
    a0_id = f"{TASK}~{baseline_suffix}"
    u1_id = f"{TASK}~usertrn_later"
    a1_id = f"{TASK}~assttrn_later"
    later_turn = _assistant_turn(TASK, "assttrn_later", parent=u1_id, children=[], status="completed")
    complete_turns = [
        _user_turn(TASK, "usertrn_0", "initial task", parent=None, children=[a0_id]),
        _assistant_turn(TASK, baseline_suffix, parent=u0_id, children=[u1_id], status="completed"),
        _user_turn(TASK, "usertrn_later", "unrelated later prompt", parent=a0_id, children=[a1_id]),
        later_turn,
    ]
    real_evidence = followup_non_delivery_evidence(
        TASK,
        ambiguous.causal_baseline,
        ambiguous.request_fingerprint,
        complete_turns,
        current_assistant_turn_id=later_turn.id,
        current_assistant_turn_status="completed",
    )
    assert real_evidence is not None
    wham.prove_follow_up_not_delivered.side_effect = lambda tid, base, fp: (
        followup_non_delivery_evidence(
            tid,
            base,
            fp,
            complete_turns,
            current_assistant_turn_id=later_turn.id,
            current_assistant_turn_status="completed",
        )
    )

    slots_bytes = slots.storage_path.read_bytes()
    followup_bytes = followup_path.read_bytes()
    review_bytes = review_path.read_bytes()
    dry_entries = reconcile_proven_non_delivered_followups(REPO, TASK, wham, apply=False)
    assert len(dry_entries) == 1
    assert dry_entries[0].operation_id == ambiguous.logical_operation_id
    assert dry_entries[0].proven_not_delivered is True
    assert dry_entries[0].applied is False
    assert dry_entries[0].evidence_id == real_evidence
    assert dry_entries[0].reason == "dry-run"
    assert slots.storage_path.read_bytes() == slots_bytes
    assert followup_path.read_bytes() == followup_bytes
    assert review_path.read_bytes() == review_bytes

    # Step 3: Sending the new follow-up auto-settles the proven-non-delivered
    # legacy overlap, cleans only its native/pending entries, and delivers.
    wham.resolve_latest_assistant_turn.return_value = later_turn.id
    wham.send_follow_up.return_value = FollowUpDeliveryResult(FollowUpDeliveryOutcome.DELIVERED, 200)
    client = CodexCloudClient(repo_name=REPO)
    assert client.send_existing_pr_followup(TASK, next_message, target, ("adversarial-gap-1",))

    after_ops = {op.logical_operation_id: op for op in accounting.snapshot(owner, incarnation).operations}
    settled_old = after_ops[ambiguous.logical_operation_id]
    assert settled_old.phase is CodexWorkPhase.SETTLED
    assert settled_old.accepted is False
    assert settled_old.definite_non_delivery is True
    assert settled_old.settlement_evidence_id == real_evidence

    new_ops = {op.source_request_id: op for op in after_ops.values() if op.kind == "follow-up" and op.logical_operation_id != ambiguous.logical_operation_id}
    assert set(new_ops) == {"adversarial-gap-1", "unrelated-delivered"}
    assert new_ops["adversarial-gap-1"].phase is CodexWorkPhase.ACCEPTED
    assert new_ops["adversarial-gap-1"].send_provenance is not None
    assert new_ops["adversarial-gap-1"].send_provenance.outcome == "accepted"
    assert new_ops["unrelated-delivered"].phase is CodexWorkPhase.ACCEPTED

    native_after = json.loads(followup_path.read_text(encoding="utf-8"))
    assert unrelated_key in native_after
    assert all(item["message_fingerprint"] != ambiguous.request_fingerprint for item in native_after.values())
    new_key = hashlib.sha256(f"{TASK}\0adversarial-gap-1".encode("utf-8")).hexdigest()
    assert native_after[new_key]["status"] == FollowUpDeliveryOutcome.DELIVERED.value

    review_after = json.loads(review_path.read_text(encoding="utf-8"))
    assert review_after["pending_feedback"] == ["unrelated-pending-id"]


def test_reconcile_aborts_if_any_native_journal_member_is_delivered(ordinary):
    run, old, new, wham, _github, incarnation = ordinary
    run((old, new))
    slots = ImplementationSlotRepository(REPO, 1)
    owner = ImplementationOwner("issue", 9)
    ambiguous = next(op for op in CodexWorkAccounting(slots).snapshot(owner, incarnation).operations if op.kind == "follow-up")

    # Mark ONLY one of the two matched entries in codex_followups.json as
    # delivered while leaving the other indeterminate (mixed state).
    followup_path = _codex_followup_state_path(REPO)
    native = json.loads(followup_path.read_text(encoding="utf-8"))
    matched_keys = [key for key, item in native.items() if item["message_fingerprint"] == ambiguous.request_fingerprint]
    assert len(matched_keys) == 2
    native[matched_keys[0]]["status"] = FollowUpDeliveryOutcome.DELIVERED.value
    followup_path.write_text(json.dumps(native, indent=2, sort_keys=True), encoding="utf-8")

    wham.prove_follow_up_not_delivered.return_value = f"{NON_DELIVERY_EVIDENCE_PREFIX}{'c' * 24}"
    followup_bytes = followup_path.read_bytes()

    # Dry-run leaves slots and follow-up journal untouched and refuses settlement.
    slots_bytes = slots.storage_path.read_bytes()
    dry_entries = reconcile_proven_non_delivered_followups(REPO, TASK, wham, apply=False)
    assert len(dry_entries) == 1
    assert dry_entries[0].proven_not_delivered is False
    assert dry_entries[0].applied is False
    assert dry_entries[0].reason == "native journal already marks a member delivered"
    assert slots.storage_path.read_bytes() == slots_bytes
    assert followup_path.read_bytes() == followup_bytes

    # Apply mode also refuses settlement and never removes the journal entries.
    apply_entries = reconcile_proven_non_delivered_followups(REPO, TASK, wham, apply=True)
    assert len(apply_entries) == 1
    assert apply_entries[0].proven_not_delivered is False
    assert apply_entries[0].applied is False
    assert apply_entries[0].reason == "native journal already marks a member delivered"
    assert followup_path.read_bytes() == followup_bytes
    still_ambiguous = next(op for op in CodexWorkAccounting(slots).snapshot(owner, incarnation).operations if op.logical_operation_id == ambiguous.logical_operation_id)
    assert still_ambiguous.phase is CodexWorkPhase.DELIVERY_UNKNOWN


def test_cli_pr_repair_reconcile_codex_followups_dry_run_and_apply(ordinary):
    run, old, _new, wham, _github, incarnation = ordinary
    run((old,))
    slots = ImplementationSlotRepository(REPO, 1)
    owner = ImplementationOwner("issue", 9)
    ambiguous = next(op for op in CodexWorkAccounting(slots).snapshot(owner, incarnation).operations if op.kind == "follow-up")
    evidence = f"{NON_DELIVERY_EVIDENCE_PREFIX}{'d' * 24}"
    wham.prove_follow_up_not_delivered.return_value = evidence

    runner = CliRunner()
    slots_bytes = slots.storage_path.read_bytes()

    with patch("auto_coder.codex_wham_client.CodexWhamClient", return_value=wham):
        dry = runner.invoke(
            pr_repair_group,
            ["reconcile-codex-followups", "--repo", REPO, "--pr", str(PR), "--json"],
        )
        assert dry.exit_code == 0, dry.output
        dry_payload = json.loads(dry.output)
        assert dry_payload["apply"] is False
        assert dry_payload["tasks"] == [TASK]
        assert len(dry_payload["entries"]) == 1
        assert dry_payload["entries"][0]["operation_id"] == ambiguous.logical_operation_id
        assert dry_payload["entries"][0]["proven_not_delivered"] is True
        assert dry_payload["entries"][0]["applied"] is False
        assert dry_payload["entries"][0]["evidence_id"] == evidence
        assert slots.storage_path.read_bytes() == slots_bytes

        applied = runner.invoke(
            pr_repair_group,
            ["reconcile-codex-followups", "--repo", REPO, "--pr", str(PR), "--apply", "--json"],
        )
        assert applied.exit_code == 0, applied.output
        applied_payload = json.loads(applied.output)
        assert applied_payload["apply"] is True
        assert applied_payload["entries"][0]["proven_not_delivered"] is True
        assert applied_payload["entries"][0]["applied"] is True
        assert applied_payload["entries"][0]["reason"] == "settled"

        # Follow-up dry-run finds no remaining ambiguous operations.
        after = runner.invoke(
            pr_repair_group,
            ["reconcile-codex-followups", "--repo", REPO, "--pr", str(PR), "--json"],
        )
        assert after.exit_code == 0, after.output
        assert json.loads(after.output)["entries"] == []


def test_cli_reconcile_discovers_task_from_slot_when_cloud_run_pr_list_empty(ordinary):
    run, old, _new, wham, _github, incarnation = ordinary
    run((old,))
    runs = CloudRunRepository(REPO)
    for item in runs.list_all():
        if item.task_id == TASK:
            item.pull_request_numbers = []
            runs.save(item)
    evidence = f"{NON_DELIVERY_EVIDENCE_PREFIX}{'e' * 24}"
    wham.prove_follow_up_not_delivered.return_value = evidence

    runner = CliRunner()
    with patch("auto_coder.codex_wham_client.CodexWhamClient", return_value=wham):
        dry = runner.invoke(
            pr_repair_group,
            ["reconcile-codex-followups", "--repo", REPO, "--pr", str(PR), "--json"],
        )
    assert dry.exit_code == 0, dry.output
    payload = json.loads(dry.output)
    assert payload["tasks"] == [TASK]
    assert len(payload["entries"]) == 1
    assert payload["entries"][0]["proven_not_delivered"] is True
    assert payload["entries"][0]["evidence_id"] == evidence
