"""A watch's own webhook batch settles once without moving independent clocks."""

import asyncio
import sqlite3
from unittest.mock import MagicMock

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import AutomationEngine
from auto_coder.entity_invalidation import CIWebhookDelivery, DurableInvalidationQueue, EntityIdentity
from auto_coder.util.gh_cache import GitHubClient
from tests.ci_observation_fakes import authoritative_ci_status

REPO = "owner/repo"
PR = 6060


def deliver(queue, name, now, head="head", number=PR, attempt=1):
    return queue.accept_ci_delivery(CIWebhookDelivery(REPO, name, "workflow_run", "completed", (number,), head, run_id="run", run_attempt=attempt), now=now)


def seeded(path, deadline=200):
    queue = DurableInvalidationQueue(path)
    queue.ensure_ci_watch(REPO, PR, "head", now=deadline - 300)
    assert queue.promote_due_ci_watches(REPO, now=deadline - 300) == 1
    claim = queue.claim(REPO)
    assert claim and queue.begin_processing(claim)
    assert queue.complete(claim) is False
    return queue


def deadline(queue):
    return queue._connection.execute("SELECT next_reconcile_at FROM ci_watches WHERE pr_number = ?", (PR,)).fetchone()[0]


def test_real_scheduler_receipts_0_1_1point5_only_one_fresh_evaluation(tmp_path, monkeypatch):
    path = tmp_path / "queue.db"
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(path))
    github = GitHubClient("test-token")
    metadata = []
    data = {"number": PR, "state": "open", "mergeable": True, "head": {"sha": "head", "ref": "dependabot/topic"}, "user": {"login": "dependabot[bot]"}, "body": "", "labels": []}
    monkeypatch.setattr(github, "get_pull_request_metadata_strict", lambda *_: metadata.append("read") or data.copy())
    engine = AutomationEngine(github, AutomationConfig())
    engine.invalidations = seeded(path)
    adjudication = MagicMock()
    monkeypatch.setattr(engine, "refresh_review_adjudications", adjudication)
    monkeypatch.setattr(engine, "_check_if_pr_merged_or_closed", lambda *_: False)
    downstream = MagicMock(side_effect=AssertionError("nonpassing CI cannot enter downstream processing"))
    monkeypatch.setattr("auto_coder.automation_engine.process_pull_request", downstream)
    slots = MagicMock(side_effect=AssertionError("nonpassing CI cannot acquire capacity"))
    monkeypatch.setattr(engine, "_get_implementation_slots", slots)
    observations = []
    canonical_read = authoritative_ci_status(success=False, ids=[11])

    def read(*args, **kwargs):
        result = canonical_read(*args, **kwargs)
        observations.append(result)
        return result

    monkeypatch.setattr("auto_coder.pr_processor._check_github_actions_status", read)
    for i, receipt in enumerate((0, 1, 1.5)):
        assert deliver(engine.invalidations, str(i), receipt)
    assert not deliver(engine.invalidations, "2", 1.5)

    async def scenario():
        worker = asyncio.create_task(engine._worker_loop(REPO, 0))
        try:
            for moment in (2, 3.5):
                monkeypatch.setattr("auto_coder.entity_invalidation.time.time", lambda: moment)
                await engine._drain_ci_webhooks(REPO)
                await engine._enqueue_pending_invalidations(REPO)
                for _ in range(200):
                    if engine.invalidations.pending_count(REPO) == 0:
                        break
                    await asyncio.sleep(0.005)
                assert engine.invalidations.pending_count(REPO) == 0
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())
    print(f"receipt_sequence=0,1,1.5 metadata_reads={len(metadata)} canonical_ci_reads={len(observations)} adjudications={adjudication.call_count}")
    assert len(observations) == 1
    assert len(metadata) == 2
    assert adjudication.call_count == 1
    downstream.assert_not_called()
    slots.assert_not_called()
    assert engine.invalidations._connection.execute("SELECT count(*) FROM ci_webhook_deliveries").fetchone() == (3,)
    assert engine.invalidations._connection.execute("SELECT observation_epoch FROM ci_watches").fetchone() == (3,)


def test_restart_quiet_window_epoch_and_ten_second_bound(tmp_path, monkeypatch):
    path = tmp_path / "queue.db"
    queue = seeded(path)
    for i, receipt in enumerate((0, 1, 1.5)):
        deliver(queue, str(i), receipt)
    queue = DurableInvalidationQueue(path)
    assert deadline(queue) == 3.5
    monkeypatch.setattr("auto_coder.entity_invalidation.time.time", lambda: 2)
    assert queue.seconds_until_next_ci(REPO) == 1.5
    assert queue.promote_due_ci_watches(REPO, now=2) == 0
    for i in range(2, 11):
        deliver(queue, str(i + 2), i)
    assert deadline(queue) == 10
    monkeypatch.setattr("auto_coder.entity_invalidation.time.time", lambda: 10)
    assert queue.promote_due_ci(REPO) == 1
    assert queue.promote_due_ci_watches(REPO, now=10) == 0
    assert deadline(queue) == 310
    assert queue._connection.execute("SELECT observation_epoch FROM ci_watches").fetchone() == (12,)


@pytest.mark.parametrize("independent_deadline", [-1, 2.25, 3, 200])
def test_independent_watchdog_never_moves_later(tmp_path, independent_deadline):
    queue = seeded(tmp_path / "queue.db", independent_deadline)
    for i, receipt in enumerate((0, 1, 1.5)):
        deliver(queue, str(i), receipt)
    assert deadline(queue) == min(independent_deadline, 3.5)
    assert queue.promote_due_ci_watches(REPO, now=min(independent_deadline, 3.5)) == 1


@pytest.mark.parametrize("head", ["new-head", None])
def test_unproven_head_discards_binding_without_delaying_old_trigger(tmp_path, head):
    queue = seeded(tmp_path / "queue.db")
    deliver(queue, "first", 0)
    deliver(queue, "second", 1)
    assert deadline(queue) == 3
    deliver(queue, "different", 1.5, head=head)
    assert deadline(queue) == 2
    assert queue._connection.execute("SELECT batch_first_seen, batch_watchdog_at FROM ci_watches").fetchone() == (None, None)
    deliver(queue, "matching-again", 1.75)
    assert deadline(queue) == 2


def test_explicit_recheck_closed_reopened_and_new_head_invalidate_binding(tmp_path, monkeypatch):
    queue = seeded(tmp_path / "queue.db")
    deliver(queue, "first", 0)
    deliver(queue, "second", 1)
    monkeypatch.setattr("auto_coder.entity_invalidation.time.time", lambda: 1.5)
    assert queue.schedule_ci_watch_recheck(REPO, PR, "head", 0.25)
    assert deadline(queue) == 1.75
    deliver(queue, "third", 1.6)
    assert deadline(queue) == 1.75
    queue.retire_ci_watches(REPO, PR)
    assert queue.promote_due_ci_watches(REPO, now=2) == 0
    assert queue.restore_ci_watches_for_open_lifecycle(REPO, PR, "new")
    assert queue._connection.execute("SELECT active FROM ci_watches WHERE head_sha='head'").fetchone() == (0,)
    assert queue._connection.execute("SELECT next_reconcile_at FROM ci_watches WHERE head_sha='new'").fetchone() == (1.5,)
    assert queue.promote_due_ci_watches(REPO, now=2) == 1


def test_blocked_watch_and_unbound_legacy_batch_stay_conservative(tmp_path, monkeypatch):
    queue = seeded(tmp_path / "queue.db")
    queue._connection.execute("UPDATE ci_watches SET operational_block='permission'")
    deliver(queue, "first", 0)
    deliver(queue, "second", 1)
    assert deadline(queue) == 2
    assert queue.promote_due_ci_watches(REPO, now=2) == 0
    monkeypatch.setattr("auto_coder.entity_invalidation.time.time", lambda: 3)
    assert queue.promote_due_ci(REPO) == 1
    assert deadline(queue) == 2
    queue._connection.execute("UPDATE ci_watches SET operational_block=NULL")
    deliver(queue, "new-batch", 4)
    queue._connection.execute("UPDATE ci_watches SET batch_first_seen=NULL, batch_watchdog_at=NULL")
    deliver(queue, "restored-without-proof", 5)
    assert deadline(queue) == 2


def test_inflight_rerun_one_followup_not_before_and_other_pr_fairness(tmp_path, monkeypatch):
    queue = seeded(tmp_path / "queue.db")
    queue.invalidate(EntityIdentity(REPO, "pr", PR))
    claim = queue.claim(REPO)
    assert claim and queue.begin_processing(claim)
    for i, receipt in enumerate((0, 1, 1.5)):
        deliver(queue, str(i), receipt, attempt=2)
    queue.invalidate(EntityIdentity(REPO, "pr", PR + 1), not_before=2)
    monkeypatch.setattr("auto_coder.entity_invalidation.time.time", lambda: 2)
    assert queue.promote_due_ci_watches(REPO, now=2) == 0
    other = queue.claim(REPO)
    assert other and other.identity.number == PR + 1
    assert queue.begin_processing(other) and queue.complete(other) is False
    monkeypatch.setattr("auto_coder.entity_invalidation.time.time", lambda: 3.5)
    assert queue.promote_due_ci(REPO) == 1
    assert queue.promote_due_ci_watches(REPO, now=3.5) == 0
    assert queue.complete(claim) is True
    followup = queue.claim(REPO)
    assert followup and followup.generation == claim.generation + 1
    assert queue.begin_processing(followup)
    assert queue.complete(followup) is False
    queue.invalidate(EntityIdentity(REPO, "pr", PR), not_before=50)
    deliver(queue, "next-attempt", 4, attempt=3)
    monkeypatch.setattr("auto_coder.entity_invalidation.time.time", lambda: 6)
    assert queue.promote_due_ci(REPO) == 1
    monkeypatch.setattr("auto_coder.entity_invalidation.time.time", lambda: 6)
    assert queue.claim(REPO) is None
    assert queue.seconds_until_next_ready(REPO) == 44


def test_intake_write_failure_rolls_back_delivery_epoch_and_clock(tmp_path):
    queue = seeded(tmp_path / "queue.db")
    deliver(queue, "first", 0)
    queue._connection.execute("CREATE TRIGGER reject_watch BEFORE UPDATE ON ci_watches BEGIN SELECT RAISE(ABORT, 'write failed'); END")
    with pytest.raises(sqlite3.DatabaseError):
        deliver(queue, "second", 1)
    assert deadline(queue) == 2
    assert queue._connection.execute("SELECT count(*) FROM ci_webhook_deliveries").fetchone() == (1,)
    assert queue._connection.execute("SELECT observation_epoch FROM ci_pending_prs").fetchone() == (1,)


def test_governor_retry_deadline_survives_batch_restart_and_other_pr(tmp_path, monkeypatch):
    path = tmp_path / "queue.db"
    queue = seeded(path)
    queue.invalidate(EntityIdentity(REPO, "pr", PR), not_before=20)
    monkeypatch.setattr("auto_coder.entity_invalidation.time.time", lambda: 20)
    claim = queue.claim(REPO)
    assert claim and queue.begin_processing(claim)
    queue.defer(claim, "github-governor", 80, api_origin="https://api.github.test", now=20)
    for i, receipt in enumerate((21, 22, 22.5)):
        deliver(queue, str(i), receipt)
    queue = DurableInvalidationQueue(path)
    queue.recover(REPO)
    monkeypatch.setattr("auto_coder.entity_invalidation.time.time", lambda: 24.5)
    assert queue.promote_due_ci(REPO) == 1
    assert queue.promote_due_ci_watches(REPO, now=24.5) == 0
    assert queue.claim(REPO) is None
    queue.invalidate(EntityIdentity(REPO, "pr", PR + 1))
    other = queue.claim(REPO)
    assert other and other.identity.number == PR + 1
    assert queue.begin_processing(other) and queue.complete(other) is False
    assert queue.seconds_until_next_ready(REPO) == 55.5
    monkeypatch.setattr("auto_coder.entity_invalidation.time.time", lambda: 80)
    resumed = queue.claim(REPO)
    assert resumed and resumed.identity.number == PR
    assert queue._connection.execute("SELECT deferral_reason, deferral_api_origin FROM entity_invalidations WHERE entity_number=?", (PR,)).fetchone() == ("github-governor", "https://api.github.test")


def test_inflight_new_head_and_review_invalidation_survive_restart(tmp_path, monkeypatch):
    path = tmp_path / "queue.db"
    queue = seeded(path)
    queue.invalidate(EntityIdentity(REPO, "pr", PR))
    claim = queue.claim(REPO)
    assert claim and queue.begin_processing(claim)
    deliver(queue, "old", 0)
    deliver(queue, "new", 1, head="new")
    queue.invalidate(EntityIdentity(REPO, "pr", PR), delivery_id="review-change")
    queue = DurableInvalidationQueue(path)
    queue.recover(REPO)
    monkeypatch.setattr("auto_coder.entity_invalidation.time.time", lambda: 3)
    assert queue.promote_due_ci(REPO) == 1
    assert queue.promote_due_ci_watches(REPO, now=3) == 0
    recovered = queue.claim(REPO)
    assert recovered and recovered.generation > claim.generation
    assert queue.begin_processing(recovered)
    assert queue.complete(recovered) is False
    assert queue.pending_count(REPO) == 0
    assert queue._connection.execute("SELECT count(*) FROM ci_webhook_deliveries").fetchone() == (2,)


def test_old_schema_pending_batch_is_not_retroactively_bound(tmp_path):
    path = tmp_path / "queue.db"
    queue = seeded(path)
    deliver(queue, "first", 0)
    deliver(queue, "second", 1)
    # Represent the prior schema/deadline, without manufacturing a new binding.
    with queue._connection:
        queue._connection.execute("UPDATE ci_watches SET next_reconcile_at=2")
        queue._connection.execute("ALTER TABLE ci_watches DROP COLUMN batch_first_seen")
        queue._connection.execute("ALTER TABLE ci_watches DROP COLUMN batch_watchdog_at")
    queue = DurableInvalidationQueue(path)
    deliver(queue, "third", 1.5)
    assert deadline(queue) == 2
    assert queue._connection.execute("SELECT observation_epoch, batch_first_seen, batch_watchdog_at FROM ci_watches").fetchone() == (3, None, None)


@pytest.mark.parametrize("producer", ["ensure", "lifecycle"])
def test_independent_watch_producer_cannot_leave_stale_batch_binding(tmp_path, producer):
    queue = seeded(tmp_path / "queue.db")
    deliver(queue, "first", 0)
    deliver(queue, "second", 1)
    assert deadline(queue) == 3
    if producer == "ensure":
        queue.ensure_ci_watch(REPO, PR, "head", now=1.5)
    else:
        assert queue.restore_ci_watches_for_open_lifecycle(REPO, PR, "head")
    assert deadline(queue) == 2
    deliver(queue, "third", 1.5)
    assert deadline(queue) == 2
    assert queue.promote_due_ci_watches(REPO, now=2) == 1


def test_same_head_next_run_attempt_gets_new_quiet_batch(tmp_path, monkeypatch):
    queue = seeded(tmp_path / "queue.db")
    for i, receipt in enumerate((0, 1, 1.5)):
        deliver(queue, str(i), receipt)
    monkeypatch.setattr("auto_coder.entity_invalidation.time.time", lambda: 3.5)
    assert queue.promote_due_ci(REPO) == 1
    for i, receipt in enumerate((4, 5, 5.5)):
        deliver(queue, f"rerun-{i}", receipt, attempt=2)
    assert deadline(queue) == 7.5
    assert queue.promote_due_ci_watches(REPO, now=6) == 0
    monkeypatch.setattr("auto_coder.entity_invalidation.time.time", lambda: 7.5)
    assert queue.promote_due_ci(REPO) == 1
    assert queue.promote_due_ci_watches(REPO, now=7.5) == 0
    assert queue._connection.execute("SELECT observation_epoch FROM ci_watches").fetchone() == (6,)
    assert queue._connection.execute("SELECT run_attempt FROM ci_webhook_deliveries WHERE delivery_id='rerun-2'").fetchone() == (2,)
