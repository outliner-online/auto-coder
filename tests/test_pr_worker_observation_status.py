"""Worker observations do not turn a no-effect CI gate into business success."""

import asyncio
import sqlite3
from unittest.mock import MagicMock

import pytest

from auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult, ExplicitTargetOutcome, PRProcessingOutcome
from auto_coder.automation_engine import AutomationEngine
from auto_coder.dashboard import prepare_log_rows
from auto_coder.entity_invalidation import CIWebhookDelivery, DurableInvalidationQueue, EntityIdentity
from auto_coder.trace_logger import get_trace_logger

REPO = "owner/repo"


async def wait_for(predicate):
    for _ in range(300):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("worker did not reach its expected checkpoint")


@pytest.mark.parametrize("kind", ["skipped", "deferred", "evaluated"])
def test_worker_classifies_pr_evaluation_without_claiming_effects(tmp_path, monkeypatch, kind):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "queue.db"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    seen = []
    monkeypatch.setattr(engine, "_create_candidate_from_single", lambda _repo, _kind, n, *_a, **_k: Candidate("pr", {"number": n, "head": {"sha": "head"}}, 0))

    def process(_repo, candidate, **_kwargs):
        seen.append(candidate.data["number"])
        return CandidateProcessingResult(
            type="pr",
            number=6010,
            success=True,
            target_outcome=ExplicitTargetOutcome.SKIPPED if kind == "skipped" else None,
            outcome=PRProcessingOutcome.DEFERRED if kind == "deferred" else PRProcessingOutcome.SUCCESS,
            target_reason="dependency-bot PR CI is not passing",
            actions=["CI observation checked"],
        )

    monkeypatch.setattr(engine, "_process_single_candidate", process)
    monkeypatch.setattr(engine, "_check_if_pr_merged_or_closed", lambda *_: False)

    async def scenario():
        await engine.invalidate_entity(REPO, "pr", 6010)
        worker = asyncio.create_task(engine._worker_loop(REPO, 0))
        try:
            await wait_for(lambda: seen and engine.invalidations.pending_count(REPO) == 0)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())
    events = [e for e in get_trace_logger().get_logs(item_type="pr", item_number=6010) if e["category"] == "Worker"]
    event = events[-1]
    assert "successfully processed" not in event["message"]
    assert event["details"]["outcome"] == kind
    assert event["details"]["actions"] == ["CI observation checked"]
    row = prepare_log_rows([event])[0]
    assert row["message"] == event["message"]
    assert kind in row["details"]
    if kind == "skipped":
        assert event["details"]["reason"] == "dependency-bot PR CI is not passing"


def test_real_ci_bursts_restart_inflight_followup_and_other_pr_fairness(tmp_path, monkeypatch):
    path = tmp_path / "queue.db"
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(path))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    processed = []
    seen_heads = []
    head = ["old"]

    def notify(number, delivery, now):
        return engine.invalidations.accept_ci_delivery(CIWebhookDelivery(REPO, delivery, "check_run", "completed", (number,), head[0]), now=now)

    for i in range(50):
        assert notify(6011, f"shard-{i}", 0)
    assert notify(6011, "shard-0", 0) is False
    # The existing 2s/10s debounce and its SQLite obligation survive restart.
    engine.invalidations = DurableInvalidationQueue(path)
    engine.invalidations.recover(REPO)
    monkeypatch.setattr("auto_coder.entity_invalidation.time.time", lambda: 20.0)
    assert engine.invalidations.promote_due_ci(REPO) == 1
    monkeypatch.setattr(engine, "_create_candidate_from_single", lambda _r, _k, n, *_a, **_kw: Candidate("pr", {"number": n, "head": {"sha": head[0]}}, 0))

    def process(_repo, candidate, **_kw):
        n = candidate.data["number"]
        processed.append(n)
        seen_heads.append(candidate.data["head"]["sha"])
        if len(processed) == 1:
            head[0] = "new"
            engine.invalidations.invalidate(EntityIdentity(REPO, "pr", 6011), delivery_id="head-or-review-change")
            engine.invalidations.invalidate(EntityIdentity(REPO, "pr", 6012), delivery_id="other-pr")
        return CandidateProcessingResult(type="pr", number=n, success=True, target_outcome=ExplicitTargetOutcome.SKIPPED, target_reason="CI gate has no passing evidence")

    monkeypatch.setattr(engine, "_process_single_candidate", process)
    monkeypatch.setattr(engine, "_check_if_pr_merged_or_closed", lambda *_: False)

    async def scenario():
        await engine._enqueue_pending_invalidations(REPO)
        worker = asyncio.create_task(engine._worker_loop(REPO, 0))
        try:
            await wait_for(lambda: len(processed) == 3 and engine.invalidations.pending_count(REPO) == 0)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())
    assert processed.count(6011) == 2 and processed.count(6012) == 1
    assert seen_heads[0] == "old" and seen_heads[1:] == ["new", "new"]
    for n in (6011, 6012):
        events = [e for e in get_trace_logger().get_logs(item_type="pr", item_number=n) if e["category"] == "Worker"]
        assert events and all("successfully processed" not in e["message"] for e in events)


def test_due_batch_services_same_due_watch_once_without_losing_epoch(tmp_path, monkeypatch):
    path = tmp_path / "queue.db"
    queue = DurableInvalidationQueue(path)
    identity = EntityIdentity(REPO, "pr", 6030)
    queue.ensure_ci_watch(REPO, 6030, "head", now=0)
    assert queue.promote_due_ci_watches(REPO, now=0) == 1
    queue.invalidate(identity)
    claim = queue.claim(REPO)
    assert claim is not None and queue.begin_processing(claim)
    for i in range(50):
        assert queue.accept_ci_delivery(CIWebhookDelivery(REPO, f"new-shard-{i}", "check_run", "completed", (6030,), "head"), now=10)
    # Every nonduplicate delivery is retained and advances the observation epoch.
    assert queue._connection.execute("SELECT observation_epoch FROM ci_pending_prs").fetchone() == (50,)
    queue = DurableInvalidationQueue(path)
    monkeypatch.setattr("auto_coder.entity_invalidation.time.time", lambda: 12.0)
    assert queue.promote_due_ci(REPO) == 1
    assert queue.promote_due_ci_watches(REPO, now=12) == 0
    assert queue._connection.execute("SELECT generation FROM entity_invalidations").fetchone() == (claim.generation + 1,)
    assert queue._connection.execute("SELECT observation_epoch, last_attempt_at, next_reconcile_at FROM ci_watches").fetchone() == (50, 12, 312)
    assert queue._connection.execute("SELECT count(*) FROM ci_delivery_targets WHERE applied = 1").fetchone() == (50,)
    assert queue.complete(claim) is True
    assert queue.promote_due_ci_watches(REPO, now=311) == 0
    assert queue.promote_due_ci_watches(REPO, now=312) == 1


def test_own_batch_watch_waits_for_quiet_and_next_rerun_is_preserved(tmp_path, monkeypatch):
    queue = DurableInvalidationQueue(tmp_path / "queue.db")
    queue.ensure_ci_watch(REPO, 6031, "head", now=0)
    queue.promote_due_ci_watches(REPO, now=0)
    queue.accept_ci_delivery(CIWebhookDelivery(REPO, "first", "check_run", "completed", (6031,), "head"), now=10)
    queue.accept_ci_delivery(CIWebhookDelivery(REPO, "second", "check_run", "completed", (6031,), "head"), now=11)
    monkeypatch.setattr("auto_coder.entity_invalidation.time.time", lambda: 12.0)
    assert queue.promote_due_ci(REPO) == 0  # quiet window is still 13
    assert queue.promote_due_ci_watches(REPO, now=12) == 0  # same batch is still settling
    monkeypatch.setattr("auto_coder.entity_invalidation.time.time", lambda: 13.0)
    assert queue.promote_due_ci(REPO) == 1
    assert queue._connection.execute("SELECT next_reconcile_at FROM ci_watches").fetchone() == (313,)  # single observation at its quiet deadline
    queue.accept_ci_delivery(CIWebhookDelivery(REPO, "rerun", "workflow_run", "completed", (6031,), "head", run_id="new-run", run_attempt=2), now=14)
    monkeypatch.setattr("auto_coder.entity_invalidation.time.time", lambda: 16.0)
    assert queue.promote_due_ci(REPO) == 1
    assert queue._connection.execute("SELECT observation_epoch FROM ci_watches").fetchone() == (3,)
    assert queue._connection.execute("SELECT run_id, run_attempt FROM ci_webhook_deliveries WHERE delivery_id = 'rerun'").fetchone() == ("new-run", 2)


def test_failed_combined_promotion_rolls_back_and_retains_raw_delivery(tmp_path, monkeypatch):
    queue = DurableInvalidationQueue(tmp_path / "queue.db")
    queue.ensure_ci_watch(REPO, 6032, "head", now=0)
    queue.accept_ci_delivery(CIWebhookDelivery(REPO, "delivery", "check_run", "completed", (6032,), "head"), now=10)
    queue._connection.execute("CREATE TRIGGER reject_watch BEFORE UPDATE ON ci_watches BEGIN SELECT RAISE(ABORT, 'write failed'); END")
    monkeypatch.setattr("auto_coder.entity_invalidation.time.time", lambda: 12.0)
    with pytest.raises(sqlite3.DatabaseError):
        queue.promote_due_ci(REPO)
    assert queue.pending_count(REPO) == 0
    assert queue._connection.execute("SELECT observation_epoch FROM ci_pending_prs").fetchone() == (1,)
    assert queue._connection.execute("SELECT applied FROM ci_delivery_targets").fetchone() == (0,)
    assert queue._connection.execute("SELECT count(*) FROM ci_webhook_deliveries").fetchone() == (1,)
