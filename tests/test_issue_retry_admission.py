"""Focused tests for the extracted Issue retry-admission service."""

from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.issue_retry_admission import IssueRetryAdmissionService, IssueRetryDisposition, IssueRetryOrigin
from auto_coder.issue_stage_routing import IssueStageRoutingStore

REPOSITORY = "owner/repository"


def _service(tmp_path, capacity=1):
    routing = IssueStageRoutingStore(tmp_path / "routing.sqlite3")
    slots = ImplementationSlotRepository(REPOSITORY, capacity, tmp_path / "slots.json")
    return IssueRetryAdmissionService(routing, slots), routing, slots


def test_manual_admission_preserves_identity_across_reopened_stores(tmp_path):
    service, routing, slots = _service(tmp_path)

    first = service.admit(REPOSITORY, 7, "generation-a", "request-a", IssueRetryOrigin.MANUAL)

    assert first.disposition is IssueRetryDisposition.OWNED
    assert first.authority is not None
    assert first.authority.request_id == "request-a"
    assert first.authority.generation == "generation-a"
    assert first.authority.ownership_reference

    reopened_routing = IssueStageRoutingStore(tmp_path / "routing.sqlite3")
    reopened_slots = ImplementationSlotRepository(REPOSITORY, 1, slots.storage_path, slots.retired_storage_path)
    replay = IssueRetryAdmissionService(reopened_routing, reopened_slots).admit(REPOSITORY, 7, "generation-a", "request-a", IssueRetryOrigin.MANUAL)

    assert replay.disposition is IssueRetryDisposition.OWNED
    assert replay.authority == first.authority
    assert reopened_slots.active_execution_ids(ImplementationOwner("issue", 7)) == (first.authority.ownership_reference,)


def test_competing_manual_request_cannot_pass_unresolved_request(tmp_path):
    service, routing, slots = _service(tmp_path)
    routing.accept_retry_request("earlier", REPOSITORY, 7, "generation-a")

    competing = service.admit(REPOSITORY, 7, "generation-a", "later", IssueRetryOrigin.MANUAL)

    assert competing.disposition is IssueRetryDisposition.DEFERRED
    assert competing.authority is not None and competing.authority.request_id == "earlier"
    assert routing.retry_request("later") is None
    assert slots.active_execution_ids(ImplementationOwner("issue", 7)) == ()


def test_stale_jules_origin_is_retired_without_mutating_history(tmp_path):
    service, routing, slots = _service(tmp_path)
    authority = routing.accept_retry_request("stale-jules-recovery-history", REPOSITORY, 7, "generation-a")

    retired = service.admit(REPOSITORY, 7, "generation-a", authority.request_id, IssueRetryOrigin.STALE_JULES)

    assert retired.disposition is IssueRetryDisposition.DEFERRED
    assert retired.authority == authority
    assert routing.retry_request(authority.request_id) == authority
    assert slots.active_execution_ids(ImplementationOwner("issue", 7)) == ()
