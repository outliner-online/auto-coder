"""Durable capacity scheduling; capacity never supplies processing authority."""

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from auto_coder.automation_config import AutomationConfig, CandidateProcessingResult, PRProcessingOutcome
from auto_coder.automation_engine import AutomationEngine
from auto_coder.dashboard_detail import evidence_rows
from auto_coder.entity_invalidation import DurableInvalidationQueue, EntityIdentity
from auto_coder.execution_trace import TraceCollector, get_trace_collector
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.util.gh_cache import GitHubClient
from tests.pr_automation_fakes import install_pr_history

REPO = "owner/repo"
PR = EntityIdentity(REPO, "pr", 100)


def active(queue):
    queue.invalidate(PR)
    claim = queue.claim(REPO)
    assert claim is not None and queue.begin_processing(claim)
    return claim


def test_retention_survives_restart_and_only_capacity_wakes_it(tmp_path):
    path = tmp_path / "queue.db"
    queue = DurableInvalidationQueue(path)
    claim = active(queue)
    assert queue.retain_pr_capacity_wait(claim) == 1
    queue.invalidate(PR, delivery_id="changed-head")
    assert queue.invalidate(PR, delivery_id="changed-head") is False
    assert queue.claim(REPO) is None
    assert queue.seconds_until_next_ready(REPO) is None
    reopened = DurableInvalidationQueue(path)
    reopened.recover(REPO)
    assert reopened.claim(REPO) is None
    assert reopened.wake_pr_capacity_waiters(REPO) == (PR,)
    assert reopened.wake_pr_capacity_waiters(REPO) == ()
    next_claim = reopened.claim(REPO)
    assert next_claim is not None and reopened.claim(REPO) is None


def test_inflight_head_and_independent_bounds_survive_retention(tmp_path, monkeypatch):
    monkeypatch.setattr("auto_coder.entity_invalidation.time.time", lambda: 100.0)
    queue = DurableInvalidationQueue(tmp_path / "queue.db")
    claim = active(queue)
    queue.invalidate(PR, not_before=400)
    queue.defer(claim, "governor", 500, now=100)
    monkeypatch.setattr("auto_coder.entity_invalidation.time.time", lambda: 600.0)
    claim = queue.claim(REPO)
    assert claim is not None and queue.begin_processing(claim)
    queue.invalidate(PR, delivery_id="inflight-head")
    assert queue.retain_pr_capacity_wait(claim) == claim.generation + 1
    before = queue.get_deferred(PR)
    queue.wake_pr_capacity_waiters(REPO)
    assert queue.get_deferred(PR) == before
    monkeypatch.setattr("auto_coder.entity_invalidation.time.time", lambda: 450.0)
    assert queue.claim(REPO) is None


def test_failed_transaction_keeps_processing_claim(tmp_path):
    queue = DurableInvalidationQueue(tmp_path / "queue.db")
    claim = active(queue)
    queue._connection.execute("CREATE TRIGGER reject_capacity BEFORE UPDATE OF capacity_waiting ON entity_invalidations BEGIN SELECT RAISE(ABORT, 'disk unavailable'); END")
    with pytest.raises(sqlite3.DatabaseError):
        queue.retain_pr_capacity_wait(claim)
    assert queue.claim(REPO) is None
    row = queue._connection.execute("SELECT state, claimed_generation FROM entity_invalidations").fetchone()
    assert row == ("processing", claim.generation)


def test_wake_never_shortens_a_result_retry_bound(tmp_path, monkeypatch):
    monkeypatch.setattr("auto_coder.entity_invalidation.time.time", lambda: 100.0)
    queue = DurableInvalidationQueue(tmp_path / "queue.db")
    queue.retain_pr_capacity_wait(active(queue), retry_not_before=600)
    queue.wake_pr_capacity_waiters(REPO)
    assert queue.claim(REPO) is None
    assert queue.seconds_until_next_ready(REPO) == 500


def test_non_pr_and_stale_claims_cannot_be_retained(tmp_path):
    queue = DurableInvalidationQueue(tmp_path / "queue.db")
    claim = active(queue)
    queue.complete(claim)
    with pytest.raises(RuntimeError):
        queue.retain_pr_capacity_wait(claim)
    queue.invalidate(EntityIdentity(REPO, "issue", 200))
    issue = queue.claim(REPO)
    assert issue is not None and queue.begin_processing(issue)
    with pytest.raises(ValueError):
        queue.retain_pr_capacity_wait(issue)


def test_multiple_connections_coalesce_and_claim_only_once(tmp_path):
    path = tmp_path / "queue.db"
    queue = DurableInvalidationQueue(path)
    claim = active(queue)
    others = [DurableInvalidationQueue(path) for _ in range(4)]
    with ThreadPoolExecutor() as pool:
        list(pool.map(lambda q: q.invalidate(PR, delivery_id="same-event"), others))
    assert queue.retain_pr_capacity_wait(claim) == 2
    with ThreadPoolExecutor() as pool:
        wakes = list(pool.map(lambda q: q.wake_pr_capacity_waiters(REPO), others))
    assert sum(map(len, wakes)) == 1
    with ThreadPoolExecutor() as pool:
        claims = list(pool.map(lambda q: q.claim(REPO), others))
    assert sum(c is not None for c in claims) == 1


@pytest.mark.parametrize("free_before_retention", [False, True])
def test_real_capacity_observer_wakes_retained_sqlite_work(tmp_path, monkeypatch, free_before_retention):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "queue.db"))
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setattr("auto_coder.automation_engine.CAPACITY_STATE_CHECK_INTERVAL_SECONDS", 0.01)
    engine = AutomationEngine(GitHubClient("test-token"), AutomationConfig())
    slots = ImplementationSlotRepository(REPO, 1, tmp_path / "slots.json")
    occupying = ImplementationOwner("issue", 7)
    slots.reserve_new(occupying)
    monkeypatch.setattr(engine, "_get_implementation_slots", lambda *_: slots)
    monkeypatch.setattr(engine, "_run_due_reclamation_checks", AsyncMock(return_value=0))
    monkeypatch.setattr(engine, "_refill_normal_implementation_slots", AsyncMock(return_value=True))
    claim = active(engine.invalidations)

    async def scenario():
        observer = asyncio.create_task(engine._capacity_refill_loop(REPO))
        try:
            if free_before_retention:
                slots.release(occupying)
                await asyncio.sleep(0.05)
            engine.invalidations.retain_pr_capacity_wait(claim)
            if not free_before_retention:
                await asyncio.sleep(0.05)
                assert engine.invalidations.claim(REPO) is None
                slots.release(occupying)
            for _ in range(200):
                if engine.invalidations._connection.execute("SELECT capacity_waiting FROM entity_invalidations").fetchone() == (0,):
                    break
                await asyncio.sleep(0.005)
            assert engine.invalidations.claim(REPO) is not None
        finally:
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)

    asyncio.run(scenario())


def test_capacity_deferral_is_not_worker_completion(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(TraceCollector, "_instance", None)
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "queue.db"))
    github = GitHubClient("test-token")
    engine = AutomationEngine(github, AutomationConfig())
    heads = []
    head = ["old"]
    install_pr_history(github, lambda _repo, _number: head[0])
    monkeypatch.setattr(github, "get_pull_request_metadata_strict", lambda *_: {"number": 100, "state": "open", "head": {"sha": head[0]}, "user": {"login": "dependabot[bot]"}})

    def process(_repo, candidate, **_kwargs):
        heads.append(candidate.data["head"]["sha"])
        return CandidateProcessingResult(type="pr", number=100, success=False, outcome=PRProcessingOutcome.DEFERRED, capacity_deferred=True)

    monkeypatch.setattr(engine, "_process_single_candidate", process)

    async def scenario():
        await engine.invalidate_entity(REPO, "pr", 100)
        worker = asyncio.create_task(engine._worker_loop(REPO, 0))
        try:
            for _ in range(200):
                if engine.invalidations.pending_count(REPO) and engine.invalidations._connection.execute("SELECT capacity_waiting FROM entity_invalidations").fetchone() == (1,):
                    break
                await asyncio.sleep(0.005)
            assert heads == ["old"]
            assert engine.invalidations.pending_count(REPO) == 1
            assert engine.invalidations.claim(REPO) is None
            head[0] = "new"
            await engine.invalidate_entity(REPO, "pr", 100)
            assert engine.invalidations.claim(REPO) is None
            assert await engine._wake_pr_capacity_waiters(REPO) == 1
            await engine._enqueue_pending_invalidations(REPO)
            for _ in range(200):
                if heads == ["old", "new"]:
                    break
                await asyncio.sleep(0.005)
            assert heads == ["old", "new"]
            for _ in range(200):
                if engine.invalidations._connection.execute("SELECT capacity_waiting FROM entity_invalidations").fetchone() == (1,):
                    break
                await asyncio.sleep(0.005)
            assert engine.invalidations.claim(REPO) is None
            events = [e for e in get_trace_collector().get_snapshot(repository=REPO, item_type="pr", item_number=100).events if e.stage_id == "pr.capacity-wait" and e.kind == "stage-result"]
            assert [e.outcome for e in events] == ["deferred", "completed", "deferred"]
            assert all(e.facts["effect"] == "scheduling-only" for e in events)
            assert [row["outcome"] for row in evidence_rows(events)] == ["deferred", "completed", "deferred"]
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("diagnostic_fails", [False, True])
def test_worker_persistence_failure_keeps_claim_and_reports_no_success(tmp_path, monkeypatch, diagnostic_fails):
    monkeypatch.setattr(TraceCollector, "_instance", None)
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "queue.db"))
    github = GitHubClient("test-token")
    engine = AutomationEngine(github, AutomationConfig())
    monkeypatch.setattr(github, "get_pull_request_metadata_strict", lambda *_: {"number": 100, "state": "open", "head": {"sha": "head"}, "user": {"login": "dependabot[bot]"}})
    monkeypatch.setattr(engine, "_process_single_candidate", lambda *_a, **_k: CandidateProcessingResult(type="pr", number=100, success=False, outcome=PRProcessingOutcome.DEFERRED, capacity_deferred=True))
    engine.invalidations._connection.execute("CREATE TRIGGER reject_capacity BEFORE UPDATE OF capacity_waiting ON entity_invalidations WHEN NEW.capacity_waiting = 1 BEGIN SELECT RAISE(ABORT, 'disk unavailable'); END")
    if diagnostic_fails:

        def fail(*_a, **_k):
            raise OSError("diagnostic failure")

        monkeypatch.setattr(get_trace_collector(), "record_event", fail)

    async def scenario():
        await engine.invalidate_entity(REPO, "pr", 100)
        await asyncio.wait_for(engine._worker_loop(REPO, 0), timeout=2)

    asyncio.run(scenario())
    assert engine.invalidations.pending_count(REPO) == 1
    assert engine.invalidations.claim(REPO) is None
    assert engine.invalidations._connection.execute("SELECT state FROM entity_invalidations").fetchone() == ("processing",)
    if not diagnostic_fails:
        events = [e for e in get_trace_collector().get_snapshot(repository=REPO, item_type="pr", item_number=100).events if e.stage_id == "pr.capacity-wait" and e.kind == "stage-result"]
        assert [e.outcome for e in events] == ["failed"]
        assert evidence_rows(events)[0]["outcome"] == "failed"


def test_woken_worker_rechecks_closed_pr_instead_of_processing_old_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "queue.db"))
    github = GitHubClient("test-token")
    engine = AutomationEngine(github, AutomationConfig())
    calls = []
    metadata = {"number": 100, "state": "open", "head": {"sha": "old"}, "user": {"login": "dependabot[bot]"}}
    monkeypatch.setattr(github, "get_pull_request_metadata_strict", lambda *_: dict(metadata))

    def process(*_a, **_k):
        calls.append("capacity-deferred")
        return CandidateProcessingResult(type="pr", number=100, success=False, outcome=PRProcessingOutcome.DEFERRED, capacity_deferred=True)

    monkeypatch.setattr(engine, "_process_single_candidate", process)

    async def scenario():
        await engine.invalidate_entity(REPO, "pr", 100)
        worker = asyncio.create_task(engine._worker_loop(REPO, 0))
        try:
            for _ in range(200):
                if engine.invalidations._connection.execute("SELECT capacity_waiting FROM entity_invalidations").fetchone() == (1,):
                    break
                await asyncio.sleep(0.005)
            assert calls == ["capacity-deferred"]
            metadata.update(state="closed", head={"sha": "new"})
            await engine._wake_pr_capacity_waiters(REPO)
            await engine._enqueue_pending_invalidations(REPO)
            for _ in range(200):
                if engine.invalidations.pending_count(REPO) == 0:
                    break
                await asyncio.sleep(0.005)
            assert engine.invalidations.pending_count(REPO) == 0
            assert calls == ["capacity-deferred"]
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())
