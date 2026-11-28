"""Exercise the production background publisher against durable PR stop state."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from auto_coder.github_pending_work import PendingWorkStore
from auto_coder.local_job_handoff import LocalJobState, LocalJobStore
from auto_coder.local_pr_repair_recovery import (
    LOCAL_PR_REPAIR_FINALIZATION_EFFECT,
    LocalPRRepairFinalizationHandler,
    rediscover_pr_repair_finalizations,
)
from auto_coder.pr_automation_guard import PRCommit, PRCommitHistory, PRGuardStore, assess_pr_automation
from auto_coder.pr_local_job_runtime import build_pr_repair_finalizer
from auto_coder.pr_repair_finalizer import PRRepairPublicationDisposition
from tests.test_pr_repair_finalizer import completed_job, git, repository


@dataclass
class RemoteHistoryClient:
    """Serve the controlled GitHub boundary from an actual bare Git remote."""

    remote: Path
    base: str
    history_reads: int = 0

    def get_pull_request_metadata_strict(self, repository: str, pr_number: int) -> dict[str, object]:
        return {
            "number": pr_number,
            "state": "open",
            "merged": False,
            "head": {"repo": {"full_name": repository}, "ref": "feature", "sha": git(self.remote, "rev-parse", "refs/heads/feature")},
            "base": {"repo": {"full_name": repository}, "ref": "main", "sha": self.base},
        }

    def get_pr_automation_history_strict(self, repository: str, pr_number: int) -> PRCommitHistory:
        assert repository == "owner/repo" and pr_number == 7
        self.history_reads += 1
        head = git(self.remote, "rev-parse", "refs/heads/feature")
        commits = tuple(PRCommit(sha, git(self.remote, "show", "-s", "--format=%B", sha)) for sha in git(self.remote, "rev-list", "--reverse", f"{self.base}..{head}").splitlines())
        return PRCommitHistory(head, len(commits), commits)


def advance_feature(source: Path, total_commits: int) -> str:
    """Extend the initial one-commit PR without changing its source result."""
    for index in range(1, total_commits):
        git(source, "commit", "--allow-empty", "-m", f"Repair attempt {index + 1}")
    git(source, "push", "origin", "feature")
    return git(source, "rev-parse", "HEAD")


@pytest.mark.parametrize("stop_reason", ["revert", "commit_limit"])
def test_background_recovery_stops_before_commit_and_preserves_artifact(tmp_path: Path, _use_real_commands, stop_reason: str) -> None:
    source, remote, head, base = repository(tmp_path)
    if stop_reason == "revert":
        git(source, "revert", "--no-edit", head)
        git(source, "push", "origin", "feature")
        head = git(source, "rev-parse", "HEAD")
    else:
        head = advance_feature(source, 30)
    store, claim, prepared, artifact = completed_job(tmp_path, source, head, base)
    client = RemoteHistoryClient(remote, base)
    wakes: list[str] = []
    finalizer = build_pr_repair_finalizer(client, store, lambda _repo, _number, candidate: not wakes.append(candidate))
    pending = PendingWorkStore(tmp_path / "pending.sqlite3")
    identities = rediscover_pr_repair_finalizations("owner/repo", store, pending)
    assert len(identities) == 1
    obligation = pending.get(identities[0])
    assert obligation is not None

    # Startup recovery reaches the production publisher without PR ingress.
    with patch("auto_coder.pr_repair_finalizer.git_commit_with_retry") as commit, patch("auto_coder.pr_repair_finalizer.git_push") as push:
        outcome = LocalPRRepairFinalizationHandler("owner/repo", store, finalizer).recover(obligation)
        repeated = build_pr_repair_finalizer(client, LocalJobStore(store.path), lambda *_args: True).finalize(claim.record.job_id)

    decision = PRGuardStore().status("owner/repo", 7)
    assert decision.stopped
    assert decision.revert_sha == (head if stop_reason == "revert" else "")
    assert decision.observed_commits == (2 if stop_reason == "revert" else 30)
    assert repeated.disposition is PRRepairPublicationDisposition.REFUSED
    assert repeated.diagnostic == decision.reason
    assert outcome.completed_effects == (LOCAL_PR_REPAIR_FINALIZATION_EFFECT,)
    assert client.history_reads == 1
    assert git(remote, "rev-parse", "refs/heads/feature") == head
    assert git(prepared.workspace, "rev-parse", "HEAD") == head
    retained = store.get(claim.record.job_id)
    assert retained is not None and retained.state is LocalJobState.RESULT_RECORDED
    assert retained.result_reference == artifact.artifact_id
    assert store.get_result_artifact(artifact.artifact_id) == artifact
    assert store.get_effect_strict(retained.job_id, retained.execution_incarnation, "commit") is None
    assert store.get_effect_strict(retained.job_id, retained.execution_incarnation, "push") is None
    assert wakes == []
    commit.assert_not_called()
    push.assert_not_called()


def test_stop_observed_after_local_commit_blocks_push_even_if_remote_head_is_restored(tmp_path: Path, _use_real_commands) -> None:
    source, remote, head, base = repository(tmp_path)
    store, claim, prepared, artifact = completed_job(tmp_path, source, head, base)
    client = RemoteHistoryClient(remote, base)
    wakes: list[str] = []
    from auto_coder.pr_repair_finalizer import git_commit_with_retry as real_commit

    def commit_then_observe_revert(*args, **kwargs):
        result = real_commit(*args, **kwargs)
        assert result.success
        # A second evaluator observes a Revert while publication is preparing.
        git(source, "revert", "--no-edit", head)
        git(source, "push", "origin", "feature")
        assert assess_pr_automation(client, "owner/repo", 7).stopped
        # Restoring H cannot erase the durable need for operator intervention.
        git(remote, "update-ref", "refs/heads/feature", head)
        return result

    finalizer = build_pr_repair_finalizer(client, store, lambda _repo, _number, candidate: not wakes.append(candidate))
    with patch("auto_coder.pr_repair_finalizer.git_commit_with_retry", side_effect=commit_then_observe_revert) as commit, patch("auto_coder.pr_repair_finalizer.git_push") as push:
        result = finalizer.finalize(claim.record.job_id)
        reopened = LocalJobStore(store.path)
        repeated = build_pr_repair_finalizer(client, reopened, lambda *_args: True).finalize(claim.record.job_id)

    candidate = git(prepared.workspace, "rev-parse", "HEAD")
    assert candidate != head
    assert result.disposition is repeated.disposition is PRRepairPublicationDisposition.REFUSED
    assert result.candidate_sha == repeated.candidate_sha == candidate
    assert git(remote, "rev-parse", "refs/heads/feature") == head
    assert PRGuardStore().status("owner/repo", 7).stopped
    assert store.get_effect_strict(claim.record.job_id, claim.record.execution_incarnation, "commit") is not None
    assert store.get_effect_strict(claim.record.job_id, claim.record.execution_incarnation, "push") is None
    assert reopened.get_result_artifact(artifact.artifact_id) == artifact
    assert wakes == []
    assert commit.call_count == 1
    push.assert_not_called()


def test_restart_confirms_already_sent_thirtieth_commit_without_another_publication(tmp_path: Path, _use_real_commands, monkeypatch) -> None:
    source, remote, _first_head, base = repository(tmp_path)
    head = advance_feature(source, 29)
    store, claim, prepared, artifact = completed_job(tmp_path, source, head, base)
    client = RemoteHistoryClient(remote, base)
    wakes: list[str] = []
    wake = lambda _repo, _number, candidate: not wakes.append(candidate)
    original_record = store.record_effect
    lose_receipt = True

    def record_without_first_push_receipt(effect_claim, name, state, evidence=""):
        nonlocal lose_receipt
        if lose_receipt and name == "push" and state == "completed":
            lose_receipt = False
            return False
        return original_record(effect_claim, name, state, evidence)

    monkeypatch.setattr(store, "record_effect", record_without_first_push_receipt)
    first = build_pr_repair_finalizer(client, store, wake).finalize(claim.record.job_id)
    candidate = git(prepared.workspace, "rev-parse", "HEAD")
    assert first.disposition is PRRepairPublicationDisposition.PENDING
    assert candidate != head and git(remote, "rev-parse", "refs/heads/feature") == candidate
    stopped = assess_pr_automation(client, "owner/repo", 7)
    assert stopped.stopped and stopped.observed_commits == 30
    assert wakes == []

    reopened = LocalJobStore(store.path)
    finalizer = build_pr_repair_finalizer(client, reopened, wake)
    pending = PendingWorkStore(tmp_path / "restart-pending.sqlite3")
    identities = rediscover_pr_repair_finalizations("owner/repo", reopened, pending)
    assert len(identities) == 1
    obligation = pending.get(identities[0])
    assert obligation is not None
    with patch("auto_coder.pr_repair_finalizer.git_commit_with_retry") as commit_again, patch("auto_coder.pr_repair_finalizer.git_push") as push_again:
        outcome = LocalPRRepairFinalizationHandler("owner/repo", reopened, finalizer).recover(obligation)
        replay = finalizer.finalize(claim.record.job_id)

    assert outcome.completed_effects == (LOCAL_PR_REPAIR_FINALIZATION_EFFECT,)
    assert replay.disposition is PRRepairPublicationDisposition.PUBLISHED
    assert replay.candidate_sha == candidate
    assert wakes == [candidate]
    retained = reopened.get(claim.record.job_id)
    assert retained is not None and retained.state is LocalJobState.SETTLED
    receipt = reopened.get_effect_strict(retained.job_id, retained.execution_incarnation, "push")
    assert receipt is not None and receipt.state == "completed"
    assert artifact.artifact_id in receipt.evidence
    assert PRGuardStore().status("owner/repo", 7) == stopped
    commit_again.assert_not_called()
    push_again.assert_not_called()
