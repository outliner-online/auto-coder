"""Dependabot enters real PR/CI/merge processing without feature ownership.

Only GitHub reads, wire responses and post-merge external effects are controlled;
admission, slot persistence, CI aggregation and merge receipts are production code.
"""

import asyncio
import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI

from auto_coder.automation_config import AutomationConfig, Candidate, ExplicitTargetOutcome, PRProcessingOutcome
from auto_coder.automation_engine import AutomationEngine
from auto_coder.dashboard import init_dashboard
from auto_coder.entity_invalidation import DurableInvalidationQueue, EntityIdentity
from auto_coder.execution_trace import EventKind, TraceCollector
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.merge_operation_state import EffectName, EffectState, MergeOperationIdentity, MergeOperationStore
from auto_coder.util.gh_cache import GitHubClient

REPOSITORY = "outliner-online/outliner"
NUMBER = 6012
HEAD = "d9b77f770ec67f6aa40f6189023d58bac0ffa2a5"


@pytest.fixture
def flow(tmp_path, monkeypatch):
    pr = {
        "number": NUMBER,
        "title": "Bump dependency",
        "body": "",
        "user": {"login": "dependabot[bot]"},
        "state": "open",
        "draft": False,
        "mergeable": True,
        "mergeStateStatus": "CLEAN",
        "head": {"ref": "dependabot/npm_and_yarn/dependency-2", "sha": HEAD},
        "base": {"ref": "main", "sha": "base"},
        "labels": [],
        "changed_files": 2,
        "additions": 2,
        "deletions": 2,
    }
    state = SimpleNamespace(pr=pr, ci="success", approval=True, requests=[], hold=None, entered=Event())

    def response(request):
        state.requests.append(request)
        path = request.url.path
        if path.endswith("/actions/runs"):
            return httpx.Response(200, json={"total_count": 1, "workflow_runs": [{"id": 44920295148, "workflow_id": 7, "run_attempt": 2, "path": ".github/workflows/ci.yml", "head_sha": HEAD, "status": "completed", "conclusion": state.ci, "name": "CI"}]})
        if path.endswith("/check-runs"):
            return httpx.Response(200, json={"total_count": 6, "check_runs": [{"id": n, "app": {"id": 4}, "head_sha": HEAD, "status": "completed", "conclusion": state.ci, "name": f"check-{n}"} for n in range(1, 7)]})
        if path.endswith("/reviews"):
            if request.method == "GET":
                return httpx.Response(200, json=[])
            assert request.method == "POST"
            assert json.loads(request.content)["commit_id"] == HEAD
            if not state.approval:
                return httpx.Response(403, json={"message": "Approval forbidden"})
            state.entered.set()
            if state.hold is not None:
                assert state.hold.wait(10)
            return httpx.Response(200, json={"id": 77, "state": "APPROVED", "commit_id": HEAD, "user": {"login": "independent-reviewer[bot]"}})
        if path.endswith("/merge"):
            assert request.method == "PUT"
            assert json.loads(request.content)["sha"] == HEAD
            return httpx.Response(200, json={"merged": True, "sha": "merged-head"})
        if path.endswith(f"/pulls/{NUMBER}"):
            assert request.method == "GET"
            return httpx.Response(200, json=state.pr)
        if path.endswith("/files"):
            return httpx.Response(200, json=[{"filename": "package.json", "status": "modified", "additions": 2, "deletions": 2}])
        raise AssertionError(f"Unexpected GitHub request: {request.method} {path}")

    client = GitHubClient(token="controlled-token")
    monkeypatch.setattr(client, "get_pull_request_metadata_strict", lambda *_: copy.deepcopy(state.pr))
    monkeypatch.setattr(client, "get_pull_request", lambda *_: copy.deepcopy(state.pr))
    monkeypatch.setattr(client, "get_pr_details", lambda raw: copy.deepcopy(raw))
    monkeypatch.setattr(client, "get_pr_comments", lambda *_: [])
    monkeypatch.setattr(client, "get_pr_comments_strict", lambda *_: [])
    monkeypatch.setattr(client, "get_connected_prs", lambda *_: [])
    monkeypatch.setattr(client, "get_pr_review_threads_strict", lambda *_: [])
    monkeypatch.setattr(client, "get_authenticated_user_login", lambda: "controller")
    monkeypatch.setattr(GitHubClient, "get_instance", lambda *_, **__: client)
    monkeypatch.setattr("auto_coder.util.gh_cache.get_caching_client", lambda **_: httpx.Client(transport=httpx.MockTransport(response)))
    monkeypatch.setattr("auto_coder.pr_processor.resolve_reviewer_app_identity", lambda *_: SimpleNamespace(login="independent-reviewer[bot]"))
    config = AutomationConfig()
    config.AUTO_MERGE = True
    config.pr_review_thread_gate = False
    config.MAX_CONCURRENT_IMPLEMENTATIONS = 6
    config.repo_name = REPOSITORY
    engine = AutomationEngine(client, config)
    slots = ImplementationSlotRepository(REPOSITORY, 6, tmp_path / "slots.json")
    engine.implementation_slots = slots
    for number in range(1, 7):
        assert slots.reserve(ImplementationOwner("issue", number))
    store = MergeOperationStore(tmp_path / "merge.db")
    monkeypatch.setattr("auto_coder.merge_operation_state.get_merge_operation_store", lambda: store)
    monkeypatch.setattr(TraceCollector, "_instance", None)
    state.engine, state.slots, state.config, state.store = engine, slots, config, store
    state.before = slots.storage_path.read_bytes()
    state.identity = MergeOperationIdentity("https://api.github.com", REPOSITORY, NUMBER)

    def run(**kwargs):
        return engine._process_single_candidate_unified(REPOSITORY, Candidate(type="pr", data=copy.deepcopy(state.pr), priority=0), config, **kwargs)

    state.run = run
    return state


@pytest.mark.parametrize("explicit,force", [(False, False), (True, False), (True, True)])
def test_six_feature_owners_allow_real_dependabot_merge_without_slot_writes(flow, explicit, force):
    with ExitStack() as stack:
        for method in ("start_execution", "finish_execution", "reserve", "release_unbound_idle_owner", "reconcile"):
            stack.enter_context(patch.object(flow.slots, method, side_effect=AssertionError(f"Unexpected feature slot effect: {method}")))
        result = flow.run(explicit_only=explicit, force=force)
    assert result.target_outcome is ExplicitTargetOutcome.SUCCESS, (result.actions, result.error)
    assert flow.slots.storage_path.read_bytes() == flow.before
    assert flow.slots.available_normal_slots() == 0
    operation = flow.store.get(flow.identity)
    assert operation is not None
    assert operation.effect(EffectName.APPROVAL).state is EffectState.CONFIRMED_COMPLETE
    assert operation.effect(EffectName.MERGE).state is EffectState.CONFIRMED_COMPLETE
    assert [request.method for request in flow.requests if request.method != "GET"] == ["POST", "PUT"]


@pytest.mark.parametrize("author", ["human", "renovate[bot]", "some-dependabot-user", "controller[bot]"])
def test_feature_and_other_bot_prs_remain_capacity_constrained(flow, author):
    flow.pr["user"]["login"] = author
    result = flow.run()
    assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
    assert result.capacity_deferred is True
    assert "logical implementation limit" in " ".join(result.actions)
    assert not any(request.method != "GET" for request in flow.requests)


@pytest.mark.parametrize("case", ["ignore", "red", "pending", "unmergeable", "allowlist", "changed-head"])
def test_blocked_dependabot_has_no_feature_or_merge_effects(flow, monkeypatch, case):
    if case == "ignore":
        flow.config.IGNORE_DEPENDABOT_PRS = True
    elif case in ("red", "pending"):
        flow.ci = "failure" if case == "red" else None
    elif case == "unmergeable":
        flow.pr["mergeable"] = False
    elif case == "allowlist":
        flow.config.PR_ALLOWLIST = []
    else:
        reads = 0

        def metadata(*_):
            nonlocal reads
            reads += 1
            pr = copy.deepcopy(flow.pr)
            if reads > 1:
                pr["head"]["sha"] = "changed-head"
            return pr

        monkeypatch.setattr(flow.engine.github, "get_pull_request_metadata_strict", metadata)
    result = flow.run()
    assert result.target_outcome is not ExplicitTargetOutcome.SUCCESS
    assert flow.slots.storage_path.read_bytes() == flow.before
    assert not any(request.method != "GET" for request in flow.requests)
    assert flow.store.get(flow.identity) is None


def test_required_approval_refusal_is_deferred_without_merge_or_slots(flow):
    flow.approval = False
    result = flow.run()
    assert result.target_outcome is ExplicitTargetOutcome.DEFERRED, (result.actions, result.error)
    assert flow.slots.storage_path.read_bytes() == flow.before
    operation = flow.store.get(flow.identity)
    assert operation is not None
    assert operation.effect(EffectName.APPROVAL).state is not EffectState.CONFIRMED_COMPLETE
    assert operation.effect(EffectName.MERGE).state is not EffectState.CONFIRMED_COMPLETE
    assert not any(request.method == "PUT" for request in flow.requests)


def test_same_pr_cannot_overlap_even_explicit_force(flow, _use_real_sleep):
    flow.hold = Event()
    other = AutomationEngine(flow.engine.github, flow.config)
    other.implementation_slots = ImplementationSlotRepository(REPOSITORY, 6, flow.slots.storage_path)
    with ThreadPoolExecutor(max_workers=1) as workers:
        first = workers.submit(flow.run)
        try:
            assert flow.entered.wait(10)
            second = other._process_single_candidate_unified(REPOSITORY, Candidate(type="pr", data=copy.deepcopy(flow.pr), priority=0), flow.config, explicit_only=True, force=True)
            assert second.target_outcome is ExplicitTargetOutcome.DEFERRED
            assert "already being processed" in second.target_reason
        finally:
            flow.hold.set()
        assert first.result(timeout=10).target_outcome is ExplicitTargetOutcome.SUCCESS
    assert [request.method for request in flow.requests if request.method != "GET"] == ["POST", "PUT"]
    assert flow.slots.storage_path.read_bytes() == flow.before


@pytest.mark.parametrize("retained", ["execution", "provider", "idle"])
def test_old_dependabot_owner_is_preserved_and_live_work_is_never_overlapped(flow, retained):
    owner = ImplementationOwner("pr", NUMBER)
    execution = flow.slots.start_execution(owner, bypass_capacity=True)
    assert execution is not None
    if retained != "execution":
        flow.slots.finish_execution(owner, execution)
    if retained == "provider":
        assert flow.slots.record_provider_session(owner, "existing-task")
    before = flow.slots.storage_path.read_bytes()
    result = flow.run(explicit_only=True, force=True)
    assert flow.slots.storage_path.read_bytes() == before
    if retained == "idle":
        assert result.target_outcome is ExplicitTargetOutcome.SUCCESS, (result.actions, result.error)
    else:
        assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
        assert "retained" in result.target_reason
        assert not any(request.method != "GET" for request in flow.requests)


def test_first_slotless_pr_does_not_need_an_existing_slot_store(flow, tmp_path):
    flow.engine.implementation_slots = ImplementationSlotRepository(REPOSITORY, 6, tmp_path / "new-repo" / "slots.json")
    result = flow.run()
    assert result.target_outcome is ExplicitTargetOutcome.SUCCESS, (result.actions, result.error)
    assert not flow.engine.implementation_slots.storage_path.exists()


@pytest.mark.parametrize("force", [False, True])
def test_explicit_public_entry_keeps_dependabot_slotless(flow, force):
    result = flow.engine.process_single(REPOSITORY, "pr", NUMBER, explicit_only=True, force=force)
    assert result["target_outcome"] == "success", result
    assert flow.slots.storage_path.read_bytes() == flow.before
    assert flow.store.get(flow.identity).effect(EffectName.MERGE).state is EffectState.CONFIRMED_COMPLETE


def test_durable_queued_worker_refreshes_and_processes_dependabot_at_full_capacity(flow):
    async def scenario():
        await flow.engine.invalidate_entity(REPOSITORY, "pr", NUMBER)
        worker = asyncio.create_task(flow.engine._worker_loop(REPOSITORY, 0, "pr"))
        try:
            for _ in range(400):
                if flow.engine.invalidations.pending_count(REPOSITORY) == 0:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("Durable PR worker did not complete its evaluation")
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())
    operation = flow.store.get(flow.identity)
    assert operation is not None
    assert operation.effect(EffectName.MERGE).state is EffectState.CONFIRMED_COMPLETE
    assert flow.slots.storage_path.read_bytes() == flow.before


async def _wait_for_worker(predicate):
    for _ in range(400):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Durable PR worker did not reach the expected state")


def test_active_execution_retries_but_provider_membership_alone_does_not(flow):
    owner = ImplementationOwner("pr", NUMBER)
    execution = flow.slots.start_execution(owner, bypass_capacity=True)
    assert flow.slots.record_provider_session(owner, "retained-session")
    before = flow.slots.storage_path.read_bytes()
    active = flow.run()
    assert active.retry_not_before is not None
    assert flow.slots.storage_path.read_bytes() == before
    flow.slots.finish_execution(owner, execution)
    before = flow.slots.storage_path.read_bytes()
    membership_only = flow.run()
    assert membership_only.retry_not_before is None
    assert "retained provider membership" in membership_only.target_reason
    assert flow.slots.storage_path.read_bytes() == before
    assert flow.slots.has_provider_sessions(owner)
    assert not any(request.method != "GET" for request in flow.requests)


@pytest.mark.parametrize("hold", ["execution", "lock", "initial-refresh", "serialized-refresh", "serialized-policy-refresh"])
def test_first_green_event_retries_durably_after_temporary_hold_without_webhook(flow, tmp_path, monkeypatch, hold):
    clock = [time.time()]
    monkeypatch.setattr("auto_coder.automation_engine.time.time", lambda: clock[0])
    path = tmp_path / "retry.db"
    engine = flow.engine
    engine.invalidations = DurableInvalidationQueue(path)
    identity = EntityIdentity(REPOSITORY, "pr", NUMBER)
    owner = ImplementationOwner("pr", NUMBER)
    execution = flow.slots.start_execution(owner, bypass_capacity=True) if hold == "execution" else None
    lock = ExitStack()
    if hold == "lock":
        lock.enter_context(flow.slots.serialize(owner))
    reads = []
    fail_at = {"initial-refresh": 2, "serialized-refresh": 4, "serialized-policy-refresh": 5}.get(hold)

    def metadata(*_):
        reads.append(clock[0])
        if len(reads) == fail_at:
            raise RuntimeError("temporarily unavailable authoritative metadata")
        return copy.deepcopy(flow.pr)

    monkeypatch.setattr(engine.github, "get_pull_request_metadata_strict", metadata)

    async def scenario():
        await engine.invalidate_entity(REPOSITORY, "pr", NUMBER)
        worker = asyncio.create_task(engine._worker_loop(REPOSITORY, 0, "pr"))
        scheduler = asyncio.create_task(engine._invalidation_loop(REPOSITORY))
        try:
            await _wait_for_worker(lambda: engine.invalidations.get_deferred(identity) is not None and engine.active_workers.get(0) is None)
            deferred = engine.invalidations.get_deferred(identity)
            assert deferred.retry_not_before == clock[0] + 60
            assert deferred.api_origin == "pr-processing-cycle"
            assert engine.invalidations.pending_count(REPOSITORY) == 1
            assert engine.invalidations._connection.execute("SELECT capacity_waiting FROM entity_invalidations").fetchone() == (0,)
            assert flow.store.get(flow.identity) is None
            assert not any(request.method != "GET" for request in flow.requests)
            assert engine.invalidations._connection.execute("SELECT COUNT(*) FROM ci_watches").fetchone() == (0,)
            before_reads = len(reads)
            for _ in range(3):
                await engine._enqueue_pending_invalidations(REPOSITORY)
            assert len(reads) == before_reads
            if execution is not None:
                flow.slots.finish_execution(owner, execution)
            lock.close()
            after_release = flow.slots.storage_path.read_bytes()
            # Reopen persisted work; neither another webhook nor a capacity
            # wake is needed, and restarting must not erase the retry bound.
            engine.invalidations = DurableInvalidationQueue(path)
            engine.invalidations.recover(REPOSITORY)
            clock[0] += 59
            assert engine._invalidation_wake_event is not None
            engine._invalidation_wake_event.set()
            await asyncio.sleep(0.02)
            assert len(reads) == before_reads
            assert engine.queue.qsize() == 0
            clock[0] += 1
            engine._invalidation_wake_event.set()
            await _wait_for_worker(lambda: engine.invalidations.pending_count(REPOSITORY) == 0)
            assert len(reads) > before_reads
            assert flow.store.get(flow.identity).effect(EffectName.MERGE).state is EffectState.CONFIRMED_COMPLETE
            assert flow.slots.storage_path.read_bytes() == after_release
        finally:
            lock.close()
            worker.cancel()
            scheduler.cancel()
            await asyncio.gather(worker, scheduler, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("hold", ["provider-membership", "policy", "identity"])
def test_non_timed_slotless_holds_are_observable_without_retry_loop(flow, tmp_path, monkeypatch, hold):
    engine = flow.engine
    engine.invalidations = DurableInvalidationQueue(tmp_path / "no-retry.db")
    identity = EntityIdentity(REPOSITORY, "pr", NUMBER)
    if hold == "provider-membership":
        owner = ImplementationOwner("pr", NUMBER)
        execution = flow.slots.start_execution(owner, bypass_capacity=True)
        flow.slots.finish_execution(owner, execution)
        assert flow.slots.record_provider_session(owner, "retained-session")
    elif hold == "policy":
        flow.config.IGNORE_DEPENDABOT_PRS = True
    reads = []

    def metadata(*_):
        reads.append(True)
        pr = copy.deepcopy(flow.pr)
        if hold == "identity" and len(reads) >= 4:
            pr["user"]["login"] = "human"
        return pr

    monkeypatch.setattr(engine.github, "get_pull_request_metadata_strict", metadata)
    before = flow.slots.storage_path.read_bytes()

    async def scenario():
        await engine.invalidate_entity(REPOSITORY, "pr", NUMBER)
        worker = asyncio.create_task(engine._worker_loop(REPOSITORY, 0, "pr"))
        try:
            await _wait_for_worker(lambda: engine.invalidations.pending_count(REPOSITORY) == 0)
            assert engine.invalidations.get_deferred(identity) is None
            read_count = len(reads)
            await engine._enqueue_pending_invalidations(REPOSITORY)
            assert len(reads) == read_count
            assert engine.queue.qsize() == 0
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())
    assert flow.slots.storage_path.read_bytes() == before
    assert not any(request.method != "GET" for request in flow.requests)
    if hold == "provider-membership":
        events = TraceCollector().get_snapshot(repository=REPOSITORY, item_type="pr", item_number=NUMBER).events
        admission = [event for event in events if event.kind == EventKind.STAGE_RESULT.value and event.stage_id == "pr.implementation-admission"]
        assert admission[-1].facts["retry_not_before"] is None
        assert "retained provider membership" in admission[-1].facts["reason"]


@patch("auto_coder.dashboard.ui")
def test_slotless_admission_reaches_mounted_detail(mock_ui, flow):
    result = flow.run()
    assert result.target_outcome is ExplicitTargetOutcome.SUCCESS, (result.actions, result.error)
    events = TraceCollector().get_snapshot(repository=REPOSITORY, item_type="pr", item_number=NUMBER).events
    admission = [event for event in events if event.kind == EventKind.STAGE_RESULT.value and event.stage_id == "pr.implementation-admission"]
    assert len(admission) == 1
    assert admission[0].facts["capacity_class"] == "slotless-dependabot"
    assert admission[0].facts["feature_slot_reserved"] is False
    pages = {}
    mock_ui.page.side_effect = lambda path: lambda function: pages.setdefault(path, function)
    init_dashboard(FastAPI(), flow.engine, REPOSITORY)
    pages["/detail/{item_type}/{item_number}"](item_type="pr", item_number=NUMBER)
    diagram = mock_ui.mermaid.call_args[0][0]
    assert "implementation admission" in diagram
    assert "completed" in diagram


def test_red_dependabot_preserves_existing_no_repair_policy_when_auto_merge_disabled(flow):
    flow.config.AUTO_MERGE_DEPENDABOT_PRS = False
    flow.ci = "failure"
    result = flow.run()
    assert result.outcome is not PRProcessingOutcome.SUCCESS, (result.actions, result.error)
    assert "dependabot" in " ".join(result.actions).lower()
    assert flow.slots.storage_path.read_bytes() == flow.before
    assert not any(request.method != "GET" for request in flow.requests)
