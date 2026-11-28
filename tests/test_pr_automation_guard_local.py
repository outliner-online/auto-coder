"""Stop unpublished Reverts at the production local-review boundaries."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import patch

import pytest

from auto_coder import local_review_repair
from auto_coder.local_job_handoff import LocalJobKind
from auto_coder.local_review_repair import LocalReviewRepairRequest, LocalReviewRepairStore, execute_local_review_repair
from auto_coder.pr_automation_guard import PRCommit, PRCommitHistory, PRGuardStore, assess_local_pr_automation, assess_pr_automation
from auto_coder.pr_local_job_runtime import build_pr_repair_finalizer
from auto_coder.pr_repair_finalizer import PRRepairPublicationDisposition
from tests.test_local_review_repair import _git, _repository, _request
from tests.test_pr_automation_guard_background import RemoteHistoryClient
from tests.test_pr_repair_finalizer import completed_job, repository

pytestmark = pytest.mark.usefixtures("_use_real_commands")


@dataclass
class RemoteHistory:
    """Read the GitHub history boundary from this test's actual bare remote."""

    remote: Path
    base: str

    def get_pr_automation_history_strict(self, repository: str, pr_number: int) -> PRCommitHistory:
        assert (repository, pr_number) == ("owner/repo", 42)
        head = _git(self.remote, "rev-parse", "refs/heads/repair-head")
        commits = tuple(PRCommit(sha, _git(self.remote, "show", "-s", "--format=%B", sha)) for sha in _git(self.remote, "rev-list", "--reverse", f"{self.base}..{head}").splitlines())
        return PRCommitHistory(head, len(commits), commits)


@dataclass
class Scenario:
    source: Path
    remote: Path
    request: LocalReviewRepairRequest
    repairs: LocalReviewRepairStore
    history: RemoteHistory

    def authorize(self) -> bool:
        return assess_pr_automation(self.history, self.request.repository, self.request.pr_number).allowed


def scenario(tmp_path: Path, monkeypatch) -> Scenario:
    source, base = _repository(tmp_path)
    (source / "tracked.txt").write_text("PR implementation\n", encoding="utf-8")
    _git(source, "commit", "-am", "Implement the reviewed PR change")
    head = _git(source, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(source, "remote", "add", "origin", str(remote))
    _git(source, "push", "origin", f"{head}:refs/heads/repair-head")
    monkeypatch.chdir(source)
    value = Scenario(source, remote, replace(_request(), head_sha=head, head_ref="repair-head"), LocalReviewRepairStore(tmp_path / "repairs.sqlite3"), RemoteHistory(remote, base))
    observed = assess_pr_automation(value.history, "owner/repo", 42)
    assert observed.allowed and observed.observed_commits == 1
    return value


def saved_revert(value: Scenario, tmp_path: Path) -> tuple[Path, str]:
    """Persist genuine committed output as an unsent correction attempt."""
    workspace = tmp_path / "retained-correction"
    _git(value.source, "worktree", "add", "--detach", str(workspace), value.request.head_sha)
    _git(workspace, "revert", "--no-edit", value.request.head_sha)
    revert = _git(workspace, "rev-parse", "HEAD")
    claim = value.repairs.admit(value.request)
    assert claim.admitted
    assert value.repairs.transition(value.request, claim, "executing", workspace_path=str(workspace))
    assert value.repairs.mark_invocation_entered(value.request, claim)
    assert value.repairs.transition(value.request, claim, "publication_pending", result_sha=revert, workspace_path=str(workspace))
    return workspace, revert


def test_editor_revert_then_dirty_edit_stops_before_next_commit_or_push(tmp_path: Path, monkeypatch) -> None:
    value = scenario(tmp_path, monkeypatch)
    editor_reverts = []

    def edit(request: LocalReviewRepairRequest, workspace: str) -> str:
        path = Path(workspace)
        _git(path, "revert", "--no-edit", request.head_sha)
        editor_reverts.append(_git(path, "rev-parse", "HEAD"))
        (path / "tracked.txt").write_text("Preserve this unfinished edit\n", encoding="utf-8")
        return "ACTION_SUMMARY: reverted the candidate and attempted another correction"

    with (
        patch("auto_coder.local_review_repair.git_commit_with_retry", wraps=local_review_repair.git_commit_with_retry) as commit,
        patch("auto_coder.local_review_repair.git_push", wraps=local_review_repair.git_push) as push,
    ):
        outcome = execute_local_review_repair(value.request, store=value.repairs, executor=edit, publication_authorizer=value.authorize)

    assert outcome.phase == "indeterminate" and not outcome.published
    retained = value.repairs.get(value.request)
    assert retained is not None and retained.phase == "indeterminate"
    workspace = Path(retained.workspace_path)
    assert _git(workspace, "rev-parse", "HEAD") == editor_reverts[0]
    assert _git(workspace, "rev-list", "--count", f"{value.request.head_sha}..HEAD") == "1"
    assert (workspace / "tracked.txt").read_text() == "Preserve this unfinished edit\n"
    assert _git(workspace, "status", "--porcelain") == "M tracked.txt"
    assert _git(value.remote, "rev-parse", "refs/heads/repair-head") == value.request.head_sha
    stopped = PRGuardStore().status("owner/repo", 42)
    assert stopped.stopped and stopped.revert_sha == editor_reverts[0]
    assert stopped.head_sha == value.request.head_sha and stopped.observed_commits == 1
    assert outcome.reason == stopped.reason
    assert assess_pr_automation(value.history, "owner/repo", 42, store=PRGuardStore()) == stopped
    event = PRGuardStore().events("owner/repo", 42)[0]
    assert event["evidence"]["source"] == "local_pr_history"
    assert event["evidence"]["local_revert_shas"] == editor_reverts
    commit.assert_not_called()
    push.assert_not_called()


def test_saved_local_revert_is_refused_before_publication_send_entry(tmp_path: Path, monkeypatch) -> None:
    value = scenario(tmp_path, monkeypatch)
    workspace, revert = saved_revert(value, tmp_path)
    with (
        patch("auto_coder.local_review_repair._prepare_default_executor") as prepare,
        patch("auto_coder.local_review_repair.git_push", wraps=local_review_repair.git_push) as push,
    ):
        outcome = execute_local_review_repair(value.request, store=LocalReviewRepairStore(value.repairs.path), publication_authorizer=value.authorize)

    retained = value.repairs.get(value.request)
    assert retained is not None and retained.phase == "publication_pending"
    assert retained.publication_send_state == "not_entered"
    assert retained.result_sha == revert and Path(retained.workspace_path) == workspace
    assert outcome.phase == "publication_pending" and not outcome.published
    stopped = PRGuardStore().status("owner/repo", 42)
    assert stopped.stopped and stopped.revert_sha == revert
    assert stopped.head_sha == value.request.head_sha and stopped.observed_commits == 1
    assert outcome.reason == stopped.reason
    assert _git(value.remote, "rev-parse", "refs/heads/repair-head") == value.request.head_sha
    prepare.assert_not_called()
    push.assert_not_called()


def test_explicit_resume_acknowledges_only_recorded_local_revert(tmp_path: Path, monkeypatch) -> None:
    value = scenario(tmp_path, monkeypatch)
    _workspace, first_revert = saved_revert(value, tmp_path)
    stopped = execute_local_review_repair(value.request, store=value.repairs, publication_authorizer=value.authorize)
    assert stopped.phase == "publication_pending" and not stopped.published
    guard = PRGuardStore()
    generation = guard.stop_generation("owner/repo", 42)
    remote_history = value.history.get_pr_automation_history_strict("owner/repo", 42)
    assert remote_history.head_sha == value.request.head_sha
    assert all(not commit.is_revert for commit in remote_history.commits)
    assert guard.resume("owner/repo", 42, remote_history, expected_head=remote_history.head_sha, reason="Inspected and approved the retained local Revert", expected_stop_generation=generation).allowed

    with patch("auto_coder.local_review_repair._prepare_default_executor") as prepare:
        resumed = execute_local_review_repair(value.request, store=LocalReviewRepairStore(value.repairs.path), publication_authorizer=value.authorize)
    assert resumed.phase == "awaiting_validation" and resumed.published
    assert _git(value.remote, "rev-parse", "refs/heads/repair-head") == first_revert
    assert value.authorize()
    prepare.assert_not_called()

    next_request = replace(value.request, head_sha=first_revert, feedback_identities=("new-finding",))
    new_reverts = []

    def another_revert(request: LocalReviewRepairRequest, workspace: str) -> str:
        path = Path(workspace)
        _git(path, "revert", "--no-edit", request.head_sha)
        new_reverts.append(_git(path, "rev-parse", "HEAD"))
        (path / "tracked.txt").write_text("Another unfinished correction\n", encoding="utf-8")
        return "ACTION_SUMMARY: another Revert"

    with (
        patch("auto_coder.local_review_repair.git_commit_with_retry", wraps=local_review_repair.git_commit_with_retry) as commit,
        patch("auto_coder.local_review_repair.git_push", wraps=local_review_repair.git_push) as push,
    ):
        again = execute_local_review_repair(next_request, store=value.repairs, executor=another_revert, publication_authorizer=value.authorize)
    assert again.phase == "indeterminate" and not again.published
    new_stop = PRGuardStore().status("owner/repo", 42)
    assert new_stop.stopped and new_stop.revert_sha == new_reverts[0] != first_revert
    assert _git(value.remote, "rev-parse", "refs/heads/repair-head") == first_revert
    assert [event["event"] for event in guard.events("owner/repo", 42)] == ["stopped", "operator_resumed", "stopped"]
    commit.assert_not_called()
    push.assert_not_called()


def test_finalizer_excludes_proven_base_revert_but_retains_outgoing_pr_revert(tmp_path: Path) -> None:
    source, remote, head, _old_base = repository(tmp_path)
    _git(source, "switch", "main")
    (source / "shared.txt").write_text("Unrelated base change\n", encoding="utf-8")
    _git(source, "commit", "-am", "Change the base independently")
    changed_base = _git(source, "rev-parse", "HEAD")
    _git(source, "revert", "--no-edit", changed_base)
    base_revert = _git(source, "rev-parse", "HEAD")
    _git(source, "push", "origin", "main")
    _git(source, "switch", "feature")
    store, claim, prepared, _artifact = completed_job(tmp_path, source, head, base_revert, kind=LocalJobKind.PR_MERGE_CONFLICT)
    client = RemoteHistoryClient(remote, base_revert)

    # The actual manifest binds the exact base; finalization must preserve the
    # base's independent history without classifying it as a PR repair Revert.
    outcome = build_pr_repair_finalizer(client, store, lambda *_args: True).finalize(claim.record.job_id)
    assert outcome.disposition is PRRepairPublicationDisposition.PUBLISHED
    assert _git(remote, "rev-parse", "refs/heads/feature") == outcome.candidate_sha
    assert _git(prepared.workspace, "show", "-s", "--format=%P", outcome.candidate_sha).split() == [head, base_revert]
    assert PRGuardStore().events("owner/repo", 7) == []

    _git(prepared.workspace, "revert", "--no-edit", head)
    outgoing_revert = _git(prepared.workspace, "rev-parse", "HEAD")
    stopped = assess_local_pr_automation("owner/repo", 7, prepared.workspace, outcome.candidate_sha, exclude_base_head=base_revert)
    assert stopped is not None and stopped.stopped
    assert stopped.revert_sha == outgoing_revert != base_revert
    assert PRGuardStore().events("owner/repo", 7)[0]["evidence"]["local_revert_shas"] == [outgoing_revert]
    assert _git(remote, "rev-parse", "refs/heads/feature") == outcome.candidate_sha
