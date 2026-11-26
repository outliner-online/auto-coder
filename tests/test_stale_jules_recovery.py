"""Regression coverage for retired stale-Jules retry authority."""

from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.issue_retry_admission import IssueRetryAdmissionService, IssueRetryDisposition, IssueRetryOrigin
from auto_coder.issue_stage_routing import IssueStageRoutingStore
from auto_coder.stale_jules_recovery import capture_stale_jules_recovery, find_pending_stale_jules_recovery, is_recovery_request_id, recovery_request_id

REPO = "owner/repo"


def test_retired_capture_does_not_create_a_grant(tmp_path):
    routing = IssueStageRoutingStore(tmp_path / "routing.sqlite3")
    request_id = recovery_request_id(REPO, 42, 100)

    assert is_recovery_request_id(request_id)
    assert capture_stale_jules_recovery(routing, REPO, 42, 100, "generation-a") is None
    assert routing.retry_request(request_id) is None


def test_historical_pending_grant_is_inert_across_restart(tmp_path):
    path = tmp_path / "routing.sqlite3"
    routing = IssueStageRoutingStore(path)
    request_id = recovery_request_id(REPO, 42, 100)
    historical = routing.accept_retry_request(request_id, REPO, 42, "generation-a")

    reopened = IssueStageRoutingStore(path)
    slots = ImplementationSlotRepository(REPO, 1, tmp_path / "slots.json")
    admission = IssueRetryAdmissionService(reopened, slots).admit(REPO, 42, "generation-a", request_id, IssueRetryOrigin.STALE_JULES)

    assert admission.disposition is IssueRetryDisposition.DEFERRED
    assert admission.authority == historical
    assert admission.reason == "automatic stale-Jules retry authority is retired"
    assert reopened.retry_request(request_id) == historical
    assert find_pending_stale_jules_recovery(reopened, REPO, 42) is None
    assert slots.active_execution_ids(ImplementationOwner("issue", 42)) == ()


def test_manual_retry_remains_authorized_with_historical_jules_row(tmp_path):
    routing = IssueStageRoutingStore(tmp_path / "routing.sqlite3")
    slots = ImplementationSlotRepository(REPO, 1, tmp_path / "slots.json")
    historical_id = recovery_request_id(REPO, 7, 91)
    routing.accept_retry_request(historical_id, REPO, 7, "generation-a")
    routing.invalidate_retry_request(historical_id, "generation-a", reason="retired automatic origin")

    admission = IssueRetryAdmissionService(routing, slots).admit(REPO, 7, "generation-a", "manual-request", IssueRetryOrigin.MANUAL, bypass_capacity=True)

    assert admission.disposition is IssueRetryDisposition.OWNED
    assert admission.authority is not None
    assert admission.authority.request_id == "manual-request"
