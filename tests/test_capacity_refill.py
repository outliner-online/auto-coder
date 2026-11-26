import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult
from auto_coder.automation_engine import AutomationEngine
from auto_coder.cloud_manager import CloudManager
from auto_coder.cloud_run import CloudRunRepository
from auto_coder.implementation_reclamation_scheduler import ReclamationObligationStore, schedule_reevaluation
from auto_coder.implementation_retirement import ImplementationPRObservation, ImplementationRetirementObservation, ProviderSessionObservation, PRTerminalState, RetirementStatus, SessionTerminalState, retire_implementation_slot
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository, ImplementationSlotUnavailable
from auto_coder.issue_retry_admission import IssueRetryAdmissionService, IssueRetryDisposition, IssueRetryOrigin
from auto_coder.retry_dispatch import RetryDispatchRepository
from auto_coder.util.gh_cache import OpenGitHubEntities, OpenGitHubIssue


def test_reclamation_propagates_provider_store_construction_failure(tmp_path):
    """REQ-002: daemon setup failure reaches the collector as unavailable evidence."""
    github = MagicMock()
    engine = AutomationEngine(github, AutomationConfig())
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    due_checks = MagicMock(return_value=0)

    with (
        patch("auto_coder.cloud_manager.CloudManager", return_value=MagicMock()),
        patch("auto_coder.cloud_run.CloudRunRepository", side_effect=RuntimeError("store unavailable")),
        patch("auto_coder.automation_engine.run_due_reclamation_checks", due_checks),
    ):
        assert asyncio.run(engine._run_due_reclamation_checks("owner/repo", slots)) == 0

    assert due_checks.call_count == 1
    assert "jules_client" not in due_checks.call_args.kwargs
    assert due_checks.call_args.kwargs["cloud_provider_stores_available"] is False


def _terminal_reclamation_github(prs, connected):
    github = MagicMock()
    github.get_connected_prs.side_effect = lambda _repo, issue, strict=False: connected[issue]
    github.get_open_pull_requests_strict.return_value = []
    github.get_pull_request_metadata_strict.side_effect = lambda _repo, number: prs[number]
    github.get_issue.side_effect = lambda _repo, number: {"number": number, "state": "open"}
    return github


def _finished_owner(slots, owner, pr_number, generation):
    execution = slots.start_execution(owner, generation=generation)
    assert execution is not None
    assert slots.record_implementation_pr(owner, pr_number)
    slots.finish_execution(owner, execution)
    incarnation = slots.owner_incarnation(owner)
    assert incarnation is not None
    return incarnation


def _run_engine_reclamation(engine, slots, jules_constructor):
    cloud_manager = MagicMock()
    cloud_manager.get_binding.return_value = None
    cloud_runs = MagicMock()
    cloud_runs.list_for_issue.return_value = []
    with (
        patch("auto_coder.cloud_manager.CloudManager", return_value=cloud_manager),
        patch("auto_coder.cloud_run.CloudRunRepository", return_value=cloud_runs),
        patch("auto_coder.codex_wham_client.CodexWhamClient", return_value=MagicMock()),
        patch("auto_coder.jules_client.JulesClient", jules_constructor),
    ):
        return asyncio.run(engine._run_due_reclamation_checks("owner/repo", slots))


def test_engine_reclamation_retires_local_owner_while_legacy_jules_stays_unknown(monkeypatch, tmp_path):
    """REQ-001/002/004/005/006: cross the daemon's real scheduled boundary."""
    monkeypatch.setenv("AUTO_CODER_ISSUE_STAGE_ROUTING_DB", str(tmp_path / "routing.sqlite3"))
    legacy = ImplementationOwner("issue", 100)
    local = ImplementationOwner("issue", 200)
    github = _terminal_reclamation_github(
        {
            201: {"number": 201, "state": "closed", "merged": False},
            202: {"number": 202, "state": "closed", "merged": False},
        },
        {100: [201], 200: [202]},
    )
    engine = AutomationEngine(github, AutomationConfig())
    slots = ImplementationSlotRepository("owner/repo", 2, tmp_path / "slots.json")
    legacy_incarnation = _finished_owner(slots, legacy, 201, "legacy-generation")
    local_incarnation = _finished_owner(slots, local, 202, "local-generation")
    assert slots.record_provider_session(legacy, "historical-jules-session")
    obligations = ReclamationObligationStore.for_slots(slots)
    assert schedule_reevaluation(legacy, slots, obligations, due_at=0)
    assert schedule_reevaluation(local, slots, obligations, due_at=0)
    jules_constructor = MagicMock(side_effect=AssertionError("scheduled reclamation constructed JulesClient"))

    assert _run_engine_reclamation(engine, slots, jules_constructor) == 1

    jules_constructor.assert_not_called()
    assert slots.owner_incarnation(legacy) == legacy_incarnation
    assert slots.owner_incarnation(local) is None
    assert slots.available_normal_slots() == 1
    assert local_incarnation in slots._read_retired()
    assert legacy_incarnation not in slots._read_retired()
    assert engine.issue_stage_routing.is_implementation_owned("owner/repo", 200, "local-generation")
    (pending,) = obligations.all()
    assert pending.owner == legacy
    assert "historical-jules-session" in (pending.last_reason or "")


@pytest.mark.parametrize("outcome", ["claimed", "indeterminate", "accepted", "completed", "accepted-historical", "definitely-not-started"])
def test_engine_reclamation_preserves_retry_only_jules_ownership(monkeypatch, tmp_path, outcome):
    """REQ-002/005/006: a stopped retry must survive missing provider projection."""
    monkeypatch.setenv("AUTO_CODER_ISSUE_STAGE_ROUTING_DB", str(tmp_path / "routing.sqlite3"))
    legacy = ImplementationOwner("issue", 100)
    local = ImplementationOwner("issue", 200)
    github = _terminal_reclamation_github(
        {201: {"number": 201, "state": "closed"}, 202: {"number": 202, "state": "closed"}},
        {100: [201], 200: [202]},
    )
    engine = AutomationEngine(github, AutomationConfig())
    slots = ImplementationSlotRepository("owner/repo", 2, tmp_path / "slots.json")
    legacy_incarnation = _finished_owner(slots, legacy, 201, "legacy-generation")
    local_incarnation = _finished_owner(slots, local, 202, "local-generation")
    admitted = IssueRetryAdmissionService(engine.issue_stage_routing, slots).admit("owner/repo", 100, "legacy-generation", "historical-retry", IssueRetryOrigin.MANUAL)
    assert admitted.disposition is IssueRetryDisposition.OWNED
    authority = admitted.authority
    assert authority is not None and authority.ownership_reference is not None
    retries = RetryDispatchRepository("owner/repo")
    _, may_create = retries.claim(authority, "jules", "jules", {"base_branch": "main"})
    assert may_create
    if outcome in {"accepted", "completed", "accepted-historical"}:
        retries.record_outcome(authority.request_id, "accepted", external_id="retry-only-jules")
        if outcome == "completed":
            retries.record_outcome(authority.request_id, "completed", external_id="retry-only-jules")
        elif outcome == "accepted-historical":
            retries.mark_historical(authority.request_id, "a newer pointer owns the Issue")
    elif outcome != "claimed":
        retries.record_outcome(authority.request_id, outcome)
    receipt = retries.get(authority.request_id)
    slots.finish_execution(legacy, authority.ownership_reference)
    assert CloudManager("owner/repo").get_binding(100) is None
    assert CloudRunRepository("owner/repo").list_for_issue(100) == []
    assert slots._read()[legacy.key].get("provider_sessions", []) == []
    obligations = ReclamationObligationStore.for_slots(slots)
    assert schedule_reevaluation(legacy, slots, obligations, due_at=0)
    assert schedule_reevaluation(local, slots, obligations, due_at=0)
    forbidden_jules = MagicMock(side_effect=AssertionError("reclamation constructed JulesClient"))
    with (
        patch("auto_coder.codex_wham_client.CodexWhamClient", return_value=MagicMock()),
        patch("auto_coder.jules_client.JulesClient", forbidden_jules),
    ):
        freed = asyncio.run(engine._run_due_reclamation_checks("owner/repo", slots))

    forbidden_jules.assert_not_called()
    assert retries.get(authority.request_id) == receipt
    assert slots.owner_incarnation(local) is None
    assert local_incarnation in slots._read_retired()
    if outcome == "definitely-not-started":
        assert freed == 2
        assert slots.owner_incarnation(legacy) is None
        assert legacy_incarnation in slots._read_retired()
        assert obligations.all() == ()
    else:
        assert freed == 1
        assert slots.owner_incarnation(legacy) == legacy_incarnation
        assert legacy_incarnation not in slots._read_retired()
        (pending,) = obligations.all()
        assert pending.owner == legacy
        assert "RETAINED_UNKNOWN" in (pending.last_reason or "")
        expected = "retry-only-jules" if receipt.external_id else "historical-retry"
        assert expected in pending.last_reason


def test_engine_reclamation_incomplete_github_discovery_retains_capacity(monkeypatch, tmp_path):
    """REQ-003/006: incomplete strict discovery cannot cross the transaction."""
    monkeypatch.setenv("AUTO_CODER_ISSUE_STAGE_ROUTING_DB", str(tmp_path / "routing.sqlite3"))
    owner = ImplementationOwner("issue", 100)
    github = _terminal_reclamation_github(
        {201: {"number": 201, "state": "closed", "merged": False}},
        {100: RuntimeError("connected PR discovery unavailable")},
    )
    github.get_connected_prs.side_effect = RuntimeError("connected PR discovery unavailable")
    engine = AutomationEngine(github, AutomationConfig())
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    incarnation = _finished_owner(slots, owner, 201, "incomplete-generation")
    obligations = ReclamationObligationStore.for_slots(slots)
    assert schedule_reevaluation(owner, slots, obligations, due_at=0)
    jules_constructor = MagicMock(side_effect=AssertionError("scheduled reclamation constructed JulesClient"))

    assert _run_engine_reclamation(engine, slots, jules_constructor) == 0

    jules_constructor.assert_not_called()
    assert slots.owner_incarnation(owner) == incarnation
    assert slots.available_normal_slots() == 0
    assert incarnation not in slots._read_retired()
    assert not engine.issue_stage_routing.is_implementation_owned("owner/repo", 100, "incomplete-generation")
    assert len(obligations.all()) == 1


def test_external_capacity_release_refills_fresh_ranking_past_rejection(monkeypatch, tmp_path):
    """The daemon production watcher refetches, reranks, and does not stop at rejection."""
    github = MagicMock()
    github.get_open_entities_strict.return_value = OpenGitHubEntities(issues=[OpenGitHubIssue(20), OpenGitHubIssue(30)])
    snapshots = {
        20: {"number": 20, "state": "open", "created_at": "2026-01-02T00:00:00Z", "labels": [{"name": "implementation-ready"}]},
        30: {"number": 30, "state": "open", "created_at": "2026-01-03T00:00:00Z", "labels": [{"name": "implementation-ready"}, {"name": "urgent"}]},
    }
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: dict(snapshots[number])
    github.get_issue_details.side_effect = lambda issue: issue
    engine = AutomationEngine(github, AutomationConfig())
    path = tmp_path / "slots.json"
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, path)
    occupying = ImplementationOwner("issue", 10)
    assert engine.implementation_slots.reserve_new(occupying)
    attempted = []

    def process(_repo, candidate, **_kwargs):
        attempted.append(candidate.issue_number)
        if candidate.issue_number == 20:
            assert engine.implementation_slots.reserve_new(ImplementationOwner("issue", 20))
        return CandidateProcessingResult(type="issue", number=candidate.issue_number, title="", success=False, actions=[])

    monkeypatch.setattr(engine, "_process_single_candidate", process)
    monkeypatch.setattr("auto_coder.automation_engine.CAPACITY_STATE_CHECK_INTERVAL_SECONDS", 0.01)

    started = threading.Event()
    original_snapshot = engine.implementation_slots.normal_capacity_snapshot

    def snapshot_wrapper():
        result = original_snapshot()
        started.set()
        return result

    monkeypatch.setattr(engine.implementation_slots, "normal_capacity_snapshot", snapshot_wrapper)

    async def scenario():
        task = asyncio.create_task(engine._capacity_refill_loop("owner/repo"))
        assert await asyncio.to_thread(started.wait, 5)
        # A distinct repository instance represents another auto-coder process.
        ImplementationSlotRepository("owner/repo", 1, path).release(occupying)
        for _ in range(300):
            if attempted == [30, 20]:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

    assert attempted == [30, 20]
    github.get_open_entities_strict.assert_called_once_with("owner/repo")
    assert engine.implementation_slots.available_normal_slots() == 0


def test_failed_refill_enumeration_remains_pending(monkeypatch, tmp_path):
    github = MagicMock()
    github.get_open_entities_strict.side_effect = [RuntimeError("temporary outage"), OpenGitHubEntities()]
    engine = AutomationEngine(github, AutomationConfig())
    path = tmp_path / "slots.json"
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, path)
    occupying = ImplementationOwner("issue", 10)
    assert engine.implementation_slots.reserve_new(occupying)
    monkeypatch.setattr("auto_coder.automation_engine.CAPACITY_STATE_CHECK_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr("auto_coder.automation_engine.REFILL_RETRY_INTERVAL_SECONDS", 0.01)

    started = threading.Event()
    original_snapshot = engine.implementation_slots.normal_capacity_snapshot

    def snapshot_wrapper():
        result = original_snapshot()
        started.set()
        return result

    monkeypatch.setattr(engine.implementation_slots, "normal_capacity_snapshot", snapshot_wrapper)

    async def scenario():
        task = asyncio.create_task(engine._capacity_refill_loop("owner/repo"))
        assert await asyncio.to_thread(started.wait, 5)
        ImplementationSlotRepository("owner/repo", 1, path).release(occupying)
        for _ in range(300):
            if github.get_open_entities_strict.call_count == 2:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

    assert github.get_open_entities_strict.call_count == 2


def test_dispatch_authority_failure_keeps_refill_pending(monkeypatch, tmp_path):
    github = MagicMock()
    github.get_open_entities_strict.return_value = OpenGitHubEntities(issues=[OpenGitHubIssue(20)])
    snapshot = {"number": 20, "state": "open", "labels": [{"name": "implementation-ready"}]}
    github.get_issue_dispatch_snapshot_strict.return_value = snapshot
    github.get_issue_details.side_effect = lambda issue: issue
    engine = AutomationEngine(github, AutomationConfig())
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    attempts = 0

    def process(_repo, _candidate, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return CandidateProcessingResult(type="issue", number=20, error="authoritative read failed", refill_retry_required=True)
        assert engine.implementation_slots.reserve_new(ImplementationOwner("issue", 20))
        return CandidateProcessingResult(type="issue", number=20, success=True)

    monkeypatch.setattr(engine, "_process_single_candidate", process)
    assert asyncio.run(engine._refill_normal_implementation_slots("owner/repo")) is False
    assert asyncio.run(engine._refill_normal_implementation_slots("owner/repo")) is True
    assert attempts == 2


def test_unexpected_candidate_fault_pauses_only_actual_issue_and_continues(monkeypatch, tmp_path):
    """REQ-001/002/004/006/008: isolate a fault without replay or global stop."""
    github = MagicMock()
    github.get_open_entities_strict.return_value = OpenGitHubEntities(issues=[OpenGitHubIssue(20), OpenGitHubIssue(30)])
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: {
        "number": number,
        "state": "open",
        "labels": [{"name": "implementation-ready"}],
    }
    github.get_issue_details.side_effect = lambda issue: issue
    engine = AutomationEngine(github, AutomationConfig())
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 2, tmp_path / "slots.json")
    attempted = []

    def process(_repo, candidate, **_kwargs):
        attempted.append(candidate.issue_number)
        if candidate.issue_number == 20:
            raise RuntimeError("effect state is unknown")
        return CandidateProcessingResult(type="issue", number=candidate.issue_number, success=True)

    monkeypatch.setattr(engine, "_process_single_candidate", process)

    assert asyncio.run(engine._refill_normal_implementation_slots("owner/repo")) is True
    assert attempted == [20, 30]
    fault = engine.get_status()["refill_faults"]
    assert fault == [
        {
            "repository": "owner/repo",
            "target": 20,
            "phase": "candidate_dispatch",
            "exception_class": "RuntimeError",
            "disposition": "intervention_required",
            "retry_not_before": None,
        }
    ]

    paused = engine._process_single_candidate_unified(
        "owner/repo",
        Candidate(type="issue", data={"number": 20}, priority=0),
        engine.config,
    )
    assert paused.target_outcome is not None
    assert paused.error and "paused" in paused.error


def test_capacity_fault_clears_only_after_valid_store_observation(monkeypatch, tmp_path):
    """REQ-003/008/009: missing evidence is unavailable, never synthetic capacity."""
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    observations = [RuntimeError("corrupt image"), (1, (4, 9))]

    def observe():
        value = observations.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(slots, "normal_capacity_snapshot", observe)

    assert asyncio.run(engine._observe_refill_capacity("owner/repo", slots, "capacity_observation")) is None
    fault = engine.get_status()["refill_faults"][0]
    assert fault["disposition"] == "capacity_unavailable"
    assert fault["retry_not_before"] is not None
    assert engine._refill_admission_paused("owner/repo", 99)

    assert asyncio.run(engine._observe_refill_capacity("owner/repo", slots, "capacity_observation")) == (1, (4, 9))
    assert engine.get_status()["refill_faults"] == []


def test_refill_uses_validated_snapshot_instead_of_unsafe_capacity_precheck(monkeypatch, tmp_path):
    """REQ-002: a legacy read failure cannot become a permanent control fault."""
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    engine.implementation_slots = slots
    monkeypatch.setattr(
        slots,
        "available_normal_slots",
        MagicMock(side_effect=ImplementationSlotUnavailable("temporary unreadable image")),
    )
    engine.github.get_open_entities_strict.return_value = OpenGitHubEntities()

    assert asyncio.run(engine._refill_normal_implementation_slots("owner/repo")) is True
    engine.github.get_open_entities_strict.assert_called_once_with("owner/repo")
    assert engine.get_status()["refill_faults"] == []
    slots.available_normal_slots.assert_not_called()


def test_post_dispatch_capacity_failure_preserves_refill_opportunity(monkeypatch, tmp_path):
    """REQ-003: recovery against an unchanged image performs a fresh pass."""
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    engine.implementation_slots = slots
    snapshots = [
        (0, (1, 1)),
        (1, (2, 2)),
        ImplementationSlotUnavailable("post-dispatch read failed"),
        (1, (2, 2)),
        (1, (2, 2)),
    ]

    def observe():
        value = snapshots.pop(0) if snapshots else (1, (2, 2))
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(slots, "normal_capacity_snapshot", observe)
    refill = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "_refill_normal_implementation_slots", refill)
    monkeypatch.setattr("auto_coder.automation_engine.CAPACITY_STATE_CHECK_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr("auto_coder.automation_engine.REFILL_RETRY_INTERVAL_SECONDS", 1)

    async def scenario():
        task = asyncio.create_task(engine._capacity_refill_loop("owner/repo"))
        for _ in range(300):
            if refill.await_count == 2:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

    assert refill.await_count == 2
    assert engine.get_status()["refill_faults"] == []


def test_refill_initialization_fault_keeps_service_paused_until_cancel(monkeypatch):
    """REQ-001/002/009: unusable shared state pauses without ending the task."""
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    monkeypatch.setattr(
        engine,
        "_get_implementation_slots",
        MagicMock(side_effect=RuntimeError("store initialization failed")),
    )
    monkeypatch.setattr("auto_coder.automation_engine.CAPACITY_STATE_CHECK_INTERVAL_SECONDS", 0.01)

    async def scenario():
        task = asyncio.create_task(engine._capacity_refill_loop("owner/repo"))
        for _ in range(100):
            if engine.get_status()["refill_faults"]:
                break
            await asyncio.sleep(0.01)
        assert not task.done()
        task.cancel()
        results = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)

    asyncio.run(scenario())
    assert engine.get_status()["refill_faults"] == [
        {
            "repository": "owner/repo",
            "target": None,
            "phase": "service_initialization",
            "exception_class": "RuntimeError",
            "disposition": "intervention_required",
            "retry_not_before": None,
        }
    ]


def test_nested_child_fault_preserves_child_pause_and_sibling_progress(monkeypatch, tmp_path):
    """REQ-002: recursive dispatch attributes an escaped fault to the child."""
    parent = {
        "number": 10,
        "title": "Parent",
        "body": "## Objective\nCoordinate children.",
        "state": "open",
        "labels": [{"name": "implementation-ready"}],
    }
    child = {
        "number": 11,
        "title": "Child",
        "body": "Parent-Issue: #10\n## Requirements\nREQ-001: Implement child.",
        "state": "open",
        "labels": [],
    }
    sibling = {
        "number": 12,
        "title": "Independent",
        "body": "## Requirements\nREQ-001: Implement sibling.",
        "state": "open",
        "labels": [{"name": "implementation-ready"}],
    }
    snapshots = {10: parent, 11: child, 12: sibling}
    github = MagicMock()
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: dict(snapshots[number])
    github.get_direct_sub_issues_strict.side_effect = lambda _repo, number: [dict(child)] if number == 10 else []
    github.get_issue_details.side_effect = lambda value: value
    github.get_open_entities_strict.return_value = OpenGitHubEntities(issues=[OpenGitHubIssue(11), OpenGitHubIssue(12)])
    engine = AutomationEngine(github, AutomationConfig())
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 2, tmp_path / "slots.json")
    engine._is_issue_author_allowed = MagicMock(return_value=True)
    engine._defer_initial_issue_stabilization = MagicMock(return_value=False)
    engine._is_issue_specification_validation_enabled = MagicMock(return_value=False)
    engine._is_issue_decomposition_validation_enabled = MagicMock(return_value=False)
    attempted = []

    def reserved(_repo, candidate, *_args, **_kwargs):
        attempted.append(candidate.issue_number)
        if candidate.issue_number == 11:
            raise RuntimeError("child effect outcome is unknown")
        return CandidateProcessingResult(type="issue", number=candidate.issue_number, success=True)

    monkeypatch.setattr(engine, "_process_single_candidate_reserved", reserved)

    try:
        engine._process_single_candidate_unified(
            "owner/repo",
            Candidate("issue", dict(parent), 0, issue_number=10),
            engine.config,
        )
    except RuntimeError as exc:
        engine._record_candidate_refill_exception("owner/repo", 10, exc)
    else:
        raise AssertionError("nested child fault did not escape candidate dispatch")

    fault = engine.get_status()["refill_faults"][0]
    assert fault["target"] == 11
    assert engine._refill_admission_paused("owner/repo", 11)
    assert not engine._refill_admission_paused("owner/repo", 10)

    assert asyncio.run(engine._refill_normal_implementation_slots("owner/repo")) is True
    assert attempted == [11, 12]


def test_ownership_failure_does_not_abort_other_refill_candidates(monkeypatch, tmp_path):
    github = MagicMock()
    github.get_open_entities_strict.return_value = OpenGitHubEntities(issues=[OpenGitHubIssue(20), OpenGitHubIssue(30)])
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: {"number": number, "state": "open", "labels": [{"name": "implementation-ready"}]}
    github.get_issue_details.side_effect = lambda issue: issue
    engine = AutomationEngine(github, AutomationConfig())
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    attempted = []

    def implementation(_repo, candidate, *_args):
        attempted.append(candidate.issue_number)
        if candidate.issue_number == 20 and attempted.count(20) == 1:
            raise ImplementationSlotUnavailable("Timed out acquiring runtime lock 'owner.lock'")
        return CandidateProcessingResult(type="issue", number=candidate.issue_number, success=True)

    monkeypatch.setattr(engine, "_process_single_candidate_unified_impl", implementation)
    assert asyncio.run(engine._refill_normal_implementation_slots("owner/repo")) is False
    assert attempted == [20, 30]
    assert asyncio.run(engine._refill_normal_implementation_slots("owner/repo")) is True
    assert attempted == [20, 30, 20, 30]


def test_release_during_refill_causes_second_fresh_enumeration(monkeypatch, tmp_path):
    github = MagicMock()
    github.get_open_entities_strict.side_effect = [
        OpenGitHubEntities(issues=[OpenGitHubIssue(20)]),
        OpenGitHubEntities(issues=[OpenGitHubIssue(30)]),
    ]
    snapshots = {
        20: {"number": 20, "state": "open", "labels": [{"name": "implementation-ready"}]},
        30: {"number": 30, "state": "open", "labels": [{"name": "implementation-ready"}]},
    }
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: snapshots[number]
    github.get_issue_details.side_effect = lambda issue: issue
    engine = AutomationEngine(github, AutomationConfig())
    path = tmp_path / "slots.json"
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, path)
    original = ImplementationOwner("issue", 10)
    assert engine.implementation_slots.reserve_new(original)
    dispatch_started = threading.Event()
    dispatch_can_finish = threading.Event()

    def process(_repo, candidate, **_kwargs):
        owner = ImplementationOwner("issue", candidate.issue_number)
        assert engine.implementation_slots.reserve_new(owner)
        if candidate.issue_number == 20:
            dispatch_started.set()
            assert dispatch_can_finish.wait(5)
        return CandidateProcessingResult(type="issue", number=candidate.issue_number, success=True)

    monkeypatch.setattr(engine, "_process_single_candidate", process)
    monkeypatch.setattr("auto_coder.automation_engine.CAPACITY_STATE_CHECK_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr("auto_coder.automation_engine.REFILL_RETRY_INTERVAL_SECONDS", 0.01)

    started = threading.Event()
    original_snapshot = engine.implementation_slots.normal_capacity_snapshot

    def snapshot_wrapper():
        result = original_snapshot()
        started.set()
        return result

    monkeypatch.setattr(engine.implementation_slots, "normal_capacity_snapshot", snapshot_wrapper)

    async def scenario():
        task = asyncio.create_task(engine._capacity_refill_loop("owner/repo"))
        assert await asyncio.to_thread(started.wait, 5)
        external = ImplementationSlotRepository("owner/repo", 1, path)
        external.release(original)
        assert await asyncio.to_thread(dispatch_started.wait, 5)
        external.release(ImplementationOwner("issue", 20))
        dispatch_can_finish.set()
        for _ in range(300):
            if github.get_open_entities_strict.call_count >= 2 and engine.implementation_slots.available_normal_slots() == 0:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert github.get_open_entities_strict.call_count == 2
    assert ImplementationOwner("issue", 30) in engine.implementation_slots.active_owners()


def test_fill_and_release_between_ordinary_samples_triggers_refill(monkeypatch, tmp_path):
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    path = tmp_path / "slots.json"
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, path)
    refill = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "_refill_normal_implementation_slots", refill)
    monkeypatch.setattr("auto_coder.automation_engine.CAPACITY_STATE_CHECK_INTERVAL_SECONDS", 0.05)

    started = threading.Event()
    original_snapshot = engine.implementation_slots.normal_capacity_snapshot

    def snapshot_wrapper():
        result = original_snapshot()
        started.set()
        return result

    monkeypatch.setattr(engine.implementation_slots, "normal_capacity_snapshot", snapshot_wrapper)

    async def scenario():
        task = asyncio.create_task(engine._capacity_refill_loop("owner/repo"))
        assert await asyncio.to_thread(started.wait, 5)
        external = ImplementationSlotRepository("owner/repo", 1, path)
        transient = ImplementationOwner("issue", 40)
        assert external.reserve_new(transient)
        external.release(transient)
        for _ in range(300):
            if refill.await_count:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    refill.assert_awaited_once_with("owner/repo")


def test_available_normal_slots_excludes_emergency_capacity(tmp_path):
    slots = ImplementationSlotRepository("owner/repo", 2, tmp_path / "slots.json")
    assert slots.available_normal_slots() == 2
    assert slots.reserve_new(ImplementationOwner("issue", 1))
    assert slots.available_normal_slots() == 1
    execution = slots.start_execution(ImplementationOwner("issue", 2), allow_urgent_emergency=True)
    assert execution is not None
    assert slots.available_normal_slots() == 0
    emergency = slots.start_execution(ImplementationOwner("issue", 3), allow_urgent_emergency=True)
    assert emergency is not None
    assert slots.available_normal_slots() == 0


@pytest.mark.parametrize("captured_predecessor", [False, True])
def test_engine_reclamation_does_not_revive_retired_retry_work(monkeypatch, tmp_path, captured_predecessor):
    """Retired responsibility must not block a later incarnation of the Issue."""
    monkeypatch.setenv("AUTO_CODER_ISSUE_STAGE_ROUTING_DB", str(tmp_path / "routing.sqlite3"))
    owner = ImplementationOwner("issue", 100)
    github = _terminal_reclamation_github({201: {"number": 201, "state": "closed"}, 203: {"number": 203, "state": "closed"}}, {100: [203]})
    engine = AutomationEngine(github, AutomationConfig())
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    old_incarnation = _finished_owner(slots, owner, 201, "old-generation")
    admission = IssueRetryAdmissionService(engine.issue_stage_routing, slots)
    authority = admission.admit("owner/repo", 100, "old-generation", "old-retry", IssueRetryOrigin.MANUAL).authority
    assert authority is not None and authority.ownership_reference is not None
    retries = RetryDispatchRepository("owner/repo")
    retries.claim(authority, "jules", "jules", {})
    retries.record_outcome(authority.request_id, "accepted", external_id="retired-jules")
    assert slots.record_provider_session(owner, "retired-jules")
    slots.finish_execution(owner, authority.ownership_reference)
    old_observation = ImplementationRetirementObservation(
        "owner/repo",
        owner,
        old_incarnation,
        slots._read()[owner.key]["activity_revision"],
        implementation_prs=(ImplementationPRObservation(201, PRTerminalState.CLOSED),),
        provider_sessions=(ProviderSessionObservation("retired-jules", "jules", SessionTerminalState.ENDED),),
    )
    assert retire_implementation_slot(slots, old_observation, engine.issue_stage_routing).status is RetirementStatus.RELEASED
    new_incarnation = _finished_owner(slots, owner, 203, "new-generation")
    if captured_predecessor:
        successor = admission.admit("owner/repo", 100, "new-generation", "new-retry", IssueRetryOrigin.MANUAL).authority
        assert successor is not None and successor.ownership_reference is not None
        retries.claim(successor, "local", "codex", {}, predecessor=("jules", "retired-jules", "jules"))
        retries.record_outcome(successor.request_id, "definitely-not-started")
        slots.finish_execution(owner, successor.ownership_reference)
    obligations = ReclamationObligationStore.for_slots(slots)
    assert schedule_reevaluation(owner, slots, obligations, due_at=0)
    forbidden_jules = MagicMock(side_effect=AssertionError("reclamation constructed JulesClient"))
    assert _run_engine_reclamation(engine, slots, forbidden_jules) == 1
    forbidden_jules.assert_not_called()
    assert slots.owner_incarnation(owner) is None
    assert {old_incarnation, new_incarnation}.issubset(slots._read_retired())
    assert obligations.all() == ()
