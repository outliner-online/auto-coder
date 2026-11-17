"""Retry reconstruction requires correlated durable PR handoff evidence."""

from dataclasses import replace
from pathlib import Path

import pytest

from auto_coder.cloud_run import CloudRun, CloudRunRepository
from auto_coder.codex_pr_recovery import CodexPRRecoveryStore, RecoveryOutcome
from auto_coder.codex_retirement_observer import _settlement
from auto_coder.codex_wham_client import WhamTask, WhamTurn
from auto_coder.codex_work_accounting import CodexWorkAccounting, CodexWorkPhase
from auto_coder.codex_work_reconstruction import production_codex_reconstructor
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.issue_stage_routing import ImplementationRetryRequest
from auto_coder.retry_dispatch import RetryDispatchRepository

REPOSITORY = "owner/repo"
OWNER = ImplementationOwner("issue", 2334)
TASK = "task_e_retry_handoff"
REQUEST = "implementation-retry-request"


def _prepare(tmp_path: Path, monkeypatch, missing: str = ""):
    monkeypatch.setenv("HOME", str(tmp_path))
    slots = ImplementationSlotRepository(REPOSITORY, 2)
    assert slots.reserve(OWNER, implementation_pr=None if missing == "slot-pr" else 92)
    authority = ImplementationRetryRequest(
        request_id=REQUEST,
        repository=REPOSITORY,
        target_number=OWNER.number,
        generation="generation-1",
        attempt_id="attempt-id",
        status="owned",
        ownership_reference="execution-1",
    )
    dispatch = RetryDispatchRepository(REPOSITORY)
    _, may_create = dispatch.claim(authority, "codex-cloud", "codex-cloud", {})
    assert may_create
    dispatch.allocate_numeric_attempt(REQUEST, [0])
    dispatch.record_outcome(REQUEST, "accepted", external_id=TASK)
    if missing != "dispatch-tracking":
        dispatch.mark_tracking_complete(REQUEST)
        if missing != "dispatch-handoff":
            dispatch.mark_handoff_complete(REQUEST)
    run = CloudRun(
        REPOSITORY,
        OWNER.number,
        1,
        "codex-cloud",
        TASK,
        "codex-cloud",
        "environment-1",
        "main",
        launch_identity=REQUEST,
        submission_outcome="accepted",
    )
    if missing == "request":
        run.launch_identity = "unrelated-request"
    elif missing == "attempt":
        run.attempt = 2
    elif missing == "task":
        run.task_id = "task_e_unrelated"
    elif missing == "repository":
        run.repo_name = "foreign/repository"
    elif missing == "accepted-run":
        run.submission_outcome = "indeterminate"
    CloudRunRepository(REPOSITORY).save(run)
    recovery = CodexPRRecoveryStore()
    if missing != "recovery":
        observed = replace(run, repo_name=REPOSITORY, task_id=TASK)
        assert recovery.save_observation(observed, RecoveryOutcome.PR_OBSERVED, pr_number=93 if missing == "recovery-pr" else 92)
        if missing != "recovery-handoff":
            assert recovery.mark_handoff(observed)
    return slots, run, recovery


def _retry(reconstructor):
    return next(operation for operation in reconstructor.readers["retry-dispatch"]().operations if operation.kind == "retry")


def test_retry_recovers_correlated_publication_without_claiming_execution(tmp_path, monkeypatch):
    slots, _run, _recovery = _prepare(tmp_path, monkeypatch)
    reconstructor = production_codex_reconstructor(REPOSITORY, OWNER.number)
    operation = _retry(reconstructor)

    assert operation.publication_complete is True
    assert operation.tracking_complete is True
    assert operation.phase is CodexWorkPhase.ACCEPTED
    assert operation.execution_complete is False
    assert operation.settlement_evidence_id is None
    assert not operation.settled
    # READY alone and an unfinished native assistant still cannot settle it.
    assert _settlement(operation, WhamTask(TASK, status="READY"), []) is None
    running = WhamTurn(f"{TASK}~assistant", "assistant", "in_progress")
    assert _settlement(operation, WhamTask(TASK, current_assistant_turn=running), [running]) is None
    terminal = replace(running, status="completed")
    certificate = _settlement(operation, WhamTask(TASK, current_assistant_turn=terminal), [terminal])
    assert certificate is not None
    assert certificate.matched_request_id == REQUEST
    assert certificate.assistant_turn_id == terminal.id
    assert certificate.task_id == TASK

    incarnation = slots.owner_incarnation(OWNER)
    assert incarnation is not None
    accounting = CodexWorkAccounting(slots, reconstructor.consistency_ids)
    snapshot = accounting.reconcile_from_receipt(OWNER, incarnation, reconstructor.reconstruct())
    assert not snapshot.releasable
    assert not next(item for item in snapshot.operations if item.kind == "retry").settled


@pytest.mark.parametrize(
    "missing",
    [
        "dispatch-tracking",
        "dispatch-handoff",
        "recovery",
        "recovery-handoff",
        "slot-pr",
        "recovery-pr",
        "request",
        "attempt",
        "task",
        "repository",
        "accepted-run",
    ],
)
def test_retry_does_not_infer_publication_from_incomplete_or_foreign_evidence(tmp_path, monkeypatch, missing):
    _prepare(tmp_path, monkeypatch, missing)
    operation = _retry(production_codex_reconstructor(REPOSITORY, OWNER.number))

    assert operation.publication_complete is False
    assert operation.tracking_complete is False
    assert operation.execution_complete is False
    assert operation.settlement_evidence_id is None


def test_retry_source_token_includes_recovery_and_owned_pr_membership(tmp_path, monkeypatch):
    slots, run, recovery = _prepare(tmp_path, monkeypatch, "recovery")
    reconstructor = production_codex_reconstructor(REPOSITORY, OWNER.number)
    before = reconstructor.readers["retry-dispatch"]()
    assert not before.operations[0].publication_complete
    assert recovery.save_observation(run, RecoveryOutcome.PR_OBSERVED, pr_number=92)
    assert recovery.mark_handoff(run)
    completed = reconstructor.readers["retry-dispatch"]()
    assert completed.consistency_id != before.consistency_id
    assert completed.operations[0].publication_complete
    assert slots.reserve(OWNER, implementation_pr=93)
    assert reconstructor.readers["retry-dispatch"]().consistency_id != completed.consistency_id


def test_retry_refresh_recovers_handoff_without_settling_existing_operation(tmp_path, monkeypatch):
    slots, run, recovery = _prepare(tmp_path, monkeypatch, "recovery")
    reconstructor = production_codex_reconstructor(REPOSITORY, OWNER.number)
    incarnation = slots.owner_incarnation(OWNER)
    assert incarnation is not None
    accounting = CodexWorkAccounting(slots, reconstructor.consistency_ids)
    before = accounting.reconcile_from_receipt(OWNER, incarnation, reconstructor.reconstruct())
    retry = next(item for item in before.operations if item.kind == "retry")
    assert not retry.publication_complete
    assert recovery.save_observation(run, RecoveryOutcome.PR_OBSERVED, pr_number=92)
    assert recovery.mark_handoff(run)

    refreshed = accounting.reconcile_from_receipt(OWNER, incarnation, reconstructor.reconstruct())
    retry = next(item for item in refreshed.operations if item.kind == "retry")
    assert retry.publication_complete and retry.tracking_complete
    assert not retry.execution_complete and not retry.settled
    assert retry.settlement_evidence_id is None
    assert not refreshed.releasable


@pytest.mark.parametrize(
    ("pr_state", "assistant_status", "missing", "released"),
    [
        ("closed", "completed", "", True),
        ("open", "completed", "", False),
        ("closed", "in_progress", "", False),
        ("closed", "READY", "", False),
        ("closed", "completed", "recovery-handoff", False),
    ],
)
def test_retry_due_reclamation_uses_real_reconstruction_and_terminal_evidence(tmp_path, monkeypatch, pr_state, assistant_status, missing, released):
    from auto_coder.codex_work_reconstruction import reconstruct_active_codex_work
    from auto_coder.implementation_reclamation_scheduler import ReclamationObligationStore, run_due_reclamation_checks, schedule_reevaluation

    slots, _run, _recovery = _prepare(tmp_path, monkeypatch, missing)
    reconstruct_active_codex_work(REPOSITORY, slots)
    incarnation = slots.owner_incarnation(OWNER)
    assert incarnation is not None
    request = WhamTurn(f"{TASK}~user", "user", "completed")
    assistant = WhamTurn(f"{TASK}~assistant", "assistant", assistant_status)
    turns = [request, assistant]

    class ProviderReader:
        def get_task(self, task_id):
            assert task_id == TASK
            return WhamTask(TASK, current_user_turn=request, current_assistant_turn=assistant, latest_turn_status=assistant_status, turns=turns, environment_id="environment-1")

        def get_task_turns(self, task_id, *, require_complete=False):
            assert task_id == TASK and require_complete
            return turns

    class GitHubReader:
        def get_connected_prs(self, repository, issue, strict=False):
            assert (repository, issue, strict) == (REPOSITORY, OWNER.number, True)
            return []

        def get_open_pull_requests_strict(self, repository):
            assert repository == REPOSITORY
            return []

        def get_pull_request_metadata_strict(self, repository, number):
            assert (repository, number) == (REPOSITORY, 92)
            return {"number": 92, "state": pr_state, "merged": False}

    obligations = ReclamationObligationStore.for_slots(slots)
    assert schedule_reevaluation(OWNER, slots, obligations, due_at=100.0)
    count = run_due_reclamation_checks(slots, obligations, github_client=GitHubReader(), cloud_run_store=CloudRunRepository(REPOSITORY), codex_wham_client=ProviderReader(), now=100.0)
    assert count == int(released)
    if released:
        assert slots.owner_incarnation(OWNER) is None
        retired = slots._read_retired()[incarnation]
        retry = next(item for item in retired["codex_work_accounting"]["operations"].values() if item["kind"] == "retry")
        assert retry["source_request_id"] == REQUEST
        assert retry["task_id"] == TASK
        assert retry["settlement_evidence_id"] == f"codex-retirement:{assistant.id}"
        assert retired["implementation_prs"] == [92]
        assert obligations.all() == ()
    else:
        assert slots.owner_incarnation(OWNER) == incarnation
        assert len(obligations.all()) == 1
