"""Future wire provenance uses the existing owner fence; no mixed eligibility."""

import hashlib
import json
from unittest.mock import patch

import pytest

from auto_coder.cloud_provider_instructions import CloudTaskOperation, prepare_cloud_task
from auto_coder.codex_cloud_client import CodexCloudClient, _codex_followup_state_path
from auto_coder.codex_wham_client import FollowUpDeliveryOutcome, FollowUpDeliveryResult
from auto_coder.codex_work_accounting import CodexSendProvenance, CodexWorkAccounting
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.pr_processor import _cloud_review_repair_state_path
from auto_coder.pr_repair import ExistingPrRepairTarget, build_existing_pr_repair_prompt
from tests.test_legacy_review_source_evidence import ordinary  # noqa: F401
from tests.test_scoped_review_followup import HEAD, PR, REPO, TASK


def _accounting():
    return CodexWorkAccounting(ImplementationSlotRepository(REPO, 1))


def test_ordinary_wire_membership_is_durable_before_post_and_acceptance_survives_restart(ordinary):
    run, old, new, wham, _github, incarnation = ordinary
    observed = []

    def transport(**kwargs):
        snapshot = _accounting().snapshot(ImplementationOwner("issue", 9), incarnation)
        reserved = [operation for operation in snapshot.operations if operation.send_provenance is not None and operation.send_provenance.outcome == "reserved"]
        assert len(reserved) == 1
        operation = reserved[0]
        provenance = operation.send_provenance
        assert provenance.fingerprint == hashlib.sha256(kwargs["prompt"].encode()).hexdigest()
        assert provenance.scope == "wildcard"
        assert provenance.pr_number == PR and provenance.head_sha == HEAD
        assert operation.source_request_id == ",".join(provenance.logical_identities)
        native = json.loads(_codex_followup_state_path(REPO).read_text())
        assert {item["logical_identity"] for item in native.values() if item["message_fingerprint"] == provenance.fingerprint} == set(provenance.logical_identities)
        observed.append(operation)
        return FollowUpDeliveryResult(FollowUpDeliveryOutcome.DELIVERED, 200)

    wham.send_follow_up.side_effect = transport
    run((old,))
    first = next(operation for operation in _accounting().snapshot(ImplementationOwner("issue", 9), incarnation).operations if operation.kind == "follow-up")
    assert first.send_provenance.outcome == "accepted"
    run((old, new))
    run((old, new))  # Rebuild production clients/readers, no extra emission.
    assert wham.send_follow_up.call_count == 2
    operations = _accounting().snapshot(ImplementationOwner("issue", 9), incarnation).operations
    assert first in operations
    assert len(observed) == 2 and sum(operation.kind == "follow-up" for operation in operations) == 2
    assert all(operation.send_provenance.outcome == "accepted" for operation in operations if operation.kind == "follow-up")
    assert json.loads(_cloud_review_repair_state_path(REPO).read_text())["pending_feedback"] == []


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("force", [False, True])
def test_unknown_wildcard_holds_broad_continuation_across_restart_without_backfill(ordinary, legacy, force):
    run, old, _new, wham, _github, incarnation = ordinary
    run((old,))
    slots = ImplementationSlotRepository(REPO, 1)
    if legacy:
        # Explicit pre-provenance accounting shape, not a fabricated unsent
        # finding: the old ordinary request has actually entered transport.
        with slots._state_lock():
            records = slots._read()
            for operation in records[ImplementationOwner("issue", 9).key][CodexWorkAccounting._FIELD]["operations"].values():
                operation.pop("send_provenance", None)
            slots._write(records)
    before = _accounting().snapshot(ImplementationOwner("issue", 9), incarnation).operations
    native = _codex_followup_state_path(REPO).read_bytes()
    target = ExistingPrRepairTarget(REPO, PR, "work-branch", "main", HEAD)
    message = build_existing_pr_repair_prompt(target, "Existing authorized broad continuation")
    for _restart in range(2):
        client = CodexCloudClient(repo_name=REPO)
        assert not client.continue_if_paused(TASK, prompt=message, force=force, repair_target=target)
        prepared = prepare_cloud_task(message, recipient="codex-cloud", operation=CloudTaskOperation.CONTINUATION, no_edit=False).prepared_task
        assert client.consume_followup_duplicate_guard(TASK, (hashlib.sha256(prepared.encode()).hexdigest(),))
    wham.send_follow_up.assert_called_once()
    assert _codex_followup_state_path(REPO).read_bytes() == native
    assert _accounting().snapshot(ImplementationOwner("issue", 9), incarnation).operations == before


def test_native_rejection_provenance_survives_bookkeeping_error_and_allows_other_request(ordinary):
    run, old, new, wham, _github, incarnation = ordinary
    wham.send_follow_up.return_value = FollowUpDeliveryResult(FollowUpDeliveryOutcome.NOT_DELIVERED, 400)
    with patch("auto_coder.codex_work_fence.CodexWorkFence.record_delivery", side_effect=RuntimeError("bookkeeping failed after native rejection")):
        actions = run((old,))
    assert any("rejected follow-up delivery" in action for action in actions)
    first = next(operation for operation in _accounting().snapshot(ImplementationOwner("issue", 9), incarnation).operations if operation.kind == "follow-up")
    assert first.send_provenance.outcome == "not_delivered"
    assert first.settled is False  # No invented execution/handoff settlement.
    wham.send_follow_up.return_value = FollowUpDeliveryResult(FollowUpDeliveryOutcome.DELIVERED, 200)
    run((new,))
    assert wham.send_follow_up.call_count == 2
    assert first in _accounting().snapshot(ImplementationOwner("issue", 9), incarnation).operations


@pytest.mark.parametrize("change", ["scope", "outcome", "members", "target", "fingerprint"])
def test_provenance_has_no_bounded_eligibility_escape(change):
    fields = dict(fingerprint="a" * 64, logical_identities=("source",))
    fields.update({"scope": "bounded"} if change == "scope" else {"outcome": "not_attempted"} if change == "outcome" else {"logical_identities": ("source", "source")} if change == "members" else {"pr_number": PR} if change == "target" else {"fingerprint": "new semantics"})
    with pytest.raises(ValueError):
        CodexSendProvenance(**fields)


def test_existing_positive_native_reconciliation_resolves_only_exact_future_membership(ordinary):
    run, old, new, wham, _github, incarnation = ordinary
    run((old,))
    original = next(operation for operation in _accounting().snapshot(ImplementationOwner("issue", 9), incarnation).operations if operation.kind == "follow-up")
    assert original.send_provenance.outcome == "unknown"
    # Existing reconciliation API supplies positive provider evidence; the
    # implementation adds no new getter or polling pass.
    wham.reconcile_follow_up.return_value = True
    wham.send_follow_up.return_value = FollowUpDeliveryResult(FollowUpDeliveryOutcome.DELIVERED, 200)
    actions = run((old, new))
    assert wham.send_follow_up.call_count == 2, actions
    operations = _accounting().snapshot(ImplementationOwner("issue", 9), incarnation).operations
    recovered = next(operation for operation in operations if operation.logical_operation_id == original.logical_operation_id)
    assert recovered.send_provenance.outcome == "accepted"
    assert recovered.send_provenance.logical_identities == original.send_provenance.logical_identities
    assert recovered.send_provenance.fingerprint == original.send_provenance.fingerprint


@pytest.mark.parametrize("crash", ["before_wire", "after_wire"])
def test_owner_reservation_survives_crash_and_cannot_be_forced_on_restart(ordinary, crash):
    run, old, _new, wham, _github, incarnation = ordinary
    if crash == "before_wire":
        with patch("auto_coder.codex_cloud_client._save_pending_followups", side_effect=OSError("crash after owner reservation")):
            run((old,))
        wham.send_follow_up.assert_not_called()
    else:
        wham.send_follow_up.side_effect = RuntimeError("wire entered; process outcome lost")
        run((old,))
        wham.send_follow_up.assert_called_once()
    original = _accounting().snapshot(ImplementationOwner("issue", 9), incarnation).operations
    assert any(operation.send_provenance is not None and operation.send_provenance.outcome == "reserved" for operation in original)
    client = CodexCloudClient(repo_name=REPO)
    assert not client.send_followup(TASK, "Existing authorized continuation", ("fresh-wire-source",), force=True)
    assert client.consume_followup_duplicate_guard(TASK, ("fresh-wire-source",))
    assert wham.send_follow_up.call_count == (0 if crash == "before_wire" else 1)
    assert _accounting().snapshot(ImplementationOwner("issue", 9), incarnation).operations == original


@pytest.mark.parametrize("force", [False, True])
def test_new_broad_continuation_remains_available_without_ambiguous_work(ordinary, force):
    run, old, _new, wham, _github, incarnation = ordinary
    wham.send_follow_up.return_value = FollowUpDeliveryResult(FollowUpDeliveryOutcome.DELIVERED, 200)
    run((old,))
    target = ExistingPrRepairTarget(REPO, PR, "work-branch", "main", HEAD)
    message = build_existing_pr_repair_prompt(target, "Existing authorized broad CI continuation")
    assert CodexCloudClient(repo_name=REPO).continue_if_paused(TASK, prompt=message, force=force, repair_target=target)
    assert wham.send_follow_up.call_count == 2
    operations = _accounting().snapshot(ImplementationOwner("issue", 9), incarnation).operations
    assert all(operation.send_provenance.outcome == "accepted" for operation in operations if operation.kind == "follow-up")
    # A forced reuse of an already recorded wire attempt cannot overwrite its
    # accepted provenance with an unrecorded second attempt.
    assert not CodexCloudClient(repo_name=REPO).continue_if_paused(TASK, prompt=message, force=True, repair_target=target)
    assert wham.send_follow_up.call_count == 2


def test_accepted_fingerprint_without_whole_membership_cannot_release_wildcard(ordinary):
    run, old, new, wham, _github, incarnation = ordinary
    run((old,))
    original = next(operation for operation in _accounting().snapshot(ImplementationOwner("issue", 9), incarnation).operations if operation.kind == "follow-up")
    path = _codex_followup_state_path(REPO)
    records = json.loads(path.read_text())
    # Explicit deficient durable inventory: the same fingerprint/turn now has
    # another member, so it cannot certify the original exact send operation.
    extra = dict(next(iter(records.values())), logical_identity="unproven-extra-member", status="delivered")
    for record in records.values():
        record["status"] = "delivered"
    records[hashlib.sha256(f"{TASK}\0unproven-extra-member".encode()).hexdigest()] = extra
    path.write_text(json.dumps(records))
    actions = run((old, new))
    wham.send_follow_up.assert_called_once()
    assert any("local duplicate guard" in action for action in actions), actions
    retained = next(operation for operation in _accounting().snapshot(ImplementationOwner("issue", 9), incarnation).operations if operation.logical_operation_id == original.logical_operation_id)
    assert retained == original
