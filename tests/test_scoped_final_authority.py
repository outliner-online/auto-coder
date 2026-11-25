"""A real competing process holds the owner lock after outer source approval."""

import json
import multiprocessing
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from auto_coder.codex_cloud_client import _codex_followup_state_path
from auto_coder.codex_wham_client import FollowUpDeliveryOutcome, FollowUpDeliveryResult
from auto_coder.codex_work_accounting import CodexWorkAccounting
from auto_coder.codex_work_fence import CodexWorkFence
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.pr_processor import _apply_review_adjudication_effects, _cloud_review_repair_state_path
from auto_coder.util.gh_cache import PullRequestRepairMetadata
from tests.test_legacy_review_source_evidence import ordinary  # noqa: F401
from tests.test_scoped_review_followup import HEAD, PR, REPO, _input


def _hold_owner(storage_path, acquired, release):
    from pathlib import Path

    slots = ImplementationSlotRepository(REPO, 1, storage_path=Path(storage_path))
    with slots.serialize(ImplementationOwner("issue", 9)):
        acquired.set()
        if not release.wait(15):
            raise RuntimeError("Test owner holder was not released")


# Real owner-lock contention needs the production retry backoff.
@pytest.mark.usefixtures("_use_real_sleep")
@pytest.mark.timeout(30)
@pytest.mark.parametrize("change", ["unchanged", "head", "decision", "source_unavailable", "ambiguous_bookkeeping", "accepted_bookkeeping"])
def test_scoped_production_entry_rechecks_after_actual_owner_lock_wait(ordinary, change):
    import auto_coder.runtime_locks as locks

    _run, old, new, wham, github, incarnation = ordinary
    threads = [old, new]
    decision = _input(threads, incarnation, "REQ-001")
    state = {"head": HEAD, "unavailable": False}
    data = github.get_pull_request_metadata_strict.return_value

    def metadata(*_args):
        if state["unavailable"]:
            raise OSError("Controlled remote authority read unavailable")
        return {**data, "head": {**data["head"], "sha": state["head"]}}

    github.get_pull_request_metadata_strict.side_effect = metadata
    github.get_pull_request_repair_metadata_strict.side_effect = lambda *_args: PullRequestRepairMetadata("work-branch", state["head"], "main")
    wham.send_follow_up.return_value = FollowUpDeliveryResult(FollowUpDeliveryOutcome.INDETERMINATE if change == "ambiguous_bookkeeping" else FollowUpDeliveryOutcome.DELIVERED, 502 if change == "ambiguous_bookkeeping" else 200)
    at_boundary = threading.Event()
    waiting = threading.Event()
    context = multiprocessing.get_context("spawn")
    acquired, release = context.Event(), context.Event()
    slots = ImplementationSlotRepository(REPO, 1)
    holder = context.Process(target=_hold_owner, args=(str(slots.storage_path), acquired, release))
    execute = CodexWorkFence.execute
    flock = locks.fcntl.flock

    def gated_execute(fence, identity, send, *args, **kwargs):
        # All outer producer checks/reconstruction have already returned. Delay
        # only the actual native owner boundary, leaving its file lock real.
        assert kwargs.get("final_authority_check") is not None
        at_boundary.set()
        assert acquired.wait(10)
        return execute(fence, identity, send, *args, **kwargs)

    def observed_flock(fd, operation):
        try:
            return flock(fd, operation)
        except BlockingIOError:
            if acquired.is_set():
                waiting.set()
            raise

    stages = []
    delivery = CodexWorkFence.record_delivery

    def bookkeeping(*args, **kwargs):
        if change in {"ambiguous_bookkeeping", "accepted_bookkeeping"}:
            raise RuntimeError("Controlled bookkeeping exception after wire")
        return delivery(*args, **kwargs)

    with (
        patch("auto_coder.pr_processor.get_pr_review_allowlist_from_config", return_value=[7]),
        patch("auto_coder.pr_processor.get_review_adjudicator_allowlist_from_config", return_value=[8]),
        patch("auto_coder.pr_processor._record_pr_stage", side_effect=lambda *args: stages.append(args)),
        patch.object(CodexWorkFence, "execute", gated_execute),
        patch.object(CodexWorkFence, "record_delivery", bookkeeping),
        patch.object(locks.fcntl, "flock", observed_flock),
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        future = executor.submit(_apply_review_adjudication_effects, REPO, PR, data, github)
        try:
            assert at_boundary.wait(10), "Real source never reached the native boundary"
            holder.start()
            assert acquired.wait(10)
            assert waiting.wait(10), "Sender never competed on the real owner file lock"
            if change == "head":
                state["head"] = "c" * 40
            elif change == "decision":
                old.comments[-1].body += "\nRemote-only decision edit"
            elif change == "source_unavailable":
                state["unavailable"] = True
            release.set()
            actions, _ = future.result(timeout=10)
        finally:
            release.set()
            if holder.pid is not None:
                holder.join(10)
                assert holder.exitcode == 0
    path = _cloud_review_repair_state_path(REPO)
    row = json.loads(path.read_text())["scoped_review_followups"][decision.decision_id]
    inventory = CodexWorkAccounting(slots).snapshot(ImplementationOwner("issue", 9), incarnation)
    own = [item for item in inventory.operations if item.source_request_id == row["snapshot"]["identity"]]
    if change in {"head", "decision", "source_unavailable"}:
        wham.send_follow_up.assert_not_called()
        assert row["state"] == "not_sent", actions
        assert row["snapshot"]["identity"] not in json.loads(path.read_text())["pending_feedback"]
        assert own == []  # Refused before native/accounting reservation.
        assert not _codex_followup_state_path(REPO).exists()
    elif change == "unchanged":
        wham.send_follow_up.assert_called_once()
        assert row["state"] == "accepted", actions
        assert own[0].send_provenance.outcome == "accepted"
        assert own[0].send_provenance.fingerprint == row["snapshot"]["native_fingerprint"]
    else:
        wham.send_follow_up.assert_called_once()
        assert row["state"] == "reserved", actions
        assert own[0].send_provenance.outcome == ("unknown" if change == "ambiguous_bookkeeping" else "accepted")
    # Real readers/clients reload on the next pass. No source or outcome replay.
    with patch("auto_coder.pr_processor.get_pr_review_allowlist_from_config", return_value=[7]), patch("auto_coder.pr_processor.get_review_adjudicator_allowlist_from_config", return_value=[8]):
        _apply_review_adjudication_effects(REPO, PR, data, github)
    assert wham.send_follow_up.call_count == (0 if change in {"head", "decision", "source_unavailable"} else 1)
    github.resolve_review_thread.assert_not_called()
    github.merge_pull_request.assert_not_called()
