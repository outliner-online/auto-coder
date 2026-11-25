"""Production review admission after a completed, still-unaddressed cloud repair."""

import json
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.adversarial_validator import AdversarialValidationResult
from auto_coder.automation_config import AutomationConfig
from auto_coder.cloud_manager import CloudTaskBinding
from auto_coder.codex_cloud_client import CodexReviewFeedbackReadiness
from auto_coder.github_app_reviewer import ReviewPublicationResult
from auto_coder.pr_processor import (
    AdversarialValidationEligibility,
    ClaimedReviewThreadGateState,
    CloudReviewRepairResult,
    CodexReviewState,
    _delegate_cloud_review_thread_repair,
    _handle_pr_merge,
    _record_review_feedback_state,
    _review_feedback_identity,
)
from auto_coder.util.gh_cache import PullRequestRepairMetadata, ReviewThread, ReviewThreadComment
from tests.ci_observation_fakes import authoritative_ci_status
from tests.test_accepted_finding_bridge import _simulated_review_source

REPOSITORY = "owner/repo"
TASK = "task_e_review6019"
HEAD = "b" * 40
PR = 6019


def _pr():
    return {
        "number": PR,
        "state": "open",
        "body": f"Fixes #5983\n\nhttps://chatgpt.com/remote/{TASK}",
        "user": {"login": "implementer"},
        "labels": [],
        "head": {"ref": "feature", "sha": HEAD},
        "base": {"ref": "main", "sha": "a" * 40},
    }


def _thread():
    return ReviewThread(
        id="ordinary-unverified-finding",
        is_resolved=False,
        comments=[
            ReviewThreadComment(database_id=101, body="[P2] Add the production browser regression.", author_login="implementer", author_id=2),
            ReviewThreadComment(database_id=102, body="The code changed, but setup failed before the browser test. This remains unverified.", author_login="implementer", author_id=2, in_reply_to_id=101),
        ],
    )


def _client():
    github = MagicMock()
    github.get_authenticated_user_login.return_value = "implementer"
    github.get_pr_comments.return_value = []
    github.get_pr_comments_strict.return_value = []
    github.get_pr_reviews_strict.return_value = []
    github.get_pull_request.return_value = _pr()
    github.get_pull_request_metadata_strict.return_value = _pr()
    github.get_pull_request_repair_metadata_strict.return_value = PullRequestRepairMetadata(head_ref="feature", head_sha=HEAD, base_ref="main")
    return github


@pytest.mark.parametrize("readiness", [CodexReviewFeedbackReadiness.COMPLETED, CodexReviewFeedbackReadiness.CURRENT_TASK_SETTLED])
@pytest.mark.parametrize("case", ["completed", "completed_due_checkpoint", "not_completed", "not_requested", "truncated", "pending", "changed_head", "closed", "changed_owner", "observation_error"])
def test_completed_delivery_observation_does_not_replay_feedback(tmp_path, case, readiness):
    """The real producer admits only positive, current evidence without altering old receipts."""
    thread = _thread()
    identity = _review_feedback_identity(f"{REPOSITORY}#{PR}:codex-cloud:{TASK}:", thread, 0)
    state = tmp_path / "review-repairs.json"
    pending = {f"{REPOSITORY}#{PR}:codex-cloud:{TASK}:uncertain"} if case == "pending" else set()
    _record_review_feedback_state(state, {identity}, pending)
    if case == "completed_due_checkpoint":
        from auto_coder.pr_no_commit_recovery import NoCommitRecovery
        from auto_coder.pr_repair import resolve_existing_pr_repair_target

        target = resolve_existing_pr_repair_target(REPOSITORY, _pr())
        assert target is not None
        NoCommitRecovery(state).record_acceptance("codex-cloud", TASK, target, (identity,), "Original repair", 1.0)
    before = state.read_bytes()
    github = _client()
    if case == "truncated":
        thread.comments_truncated = True
    if case == "changed_head":
        github.get_pull_request_metadata_strict.return_value = {**_pr(), "head": {"ref": "feature", "sha": "c" * 40}}
    if case == "closed":
        github.get_pull_request_metadata_strict.return_value = {**_pr(), "state": "closed"}
    owner = CloudTaskBinding(provider="codex-cloud", task_id=TASK)
    bindings = [owner, CloudTaskBinding(provider="codex-cloud", task_id="task_e_other")] if case == "changed_owner" else None
    with (
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=owner, side_effect=bindings),
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=state),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.observe_review_feedback_readiness", return_value=CodexReviewFeedbackReadiness.WAITING if case == "not_completed" else readiness, side_effect=RuntimeError("provider unavailable") if case == "observation_error" else None) as completed,
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup") as send,
    ):
        result = _delegate_cloud_review_thread_repair(REPOSITORY, _pr(), github_client=github, unresolved_threads=(thread,), allow_current_head_reassessment=case != "not_requested")

    admitted = case in {"completed", "completed_due_checkpoint"}
    assert result.completed_no_actionable_feedback is (admitted and readiness is CodexReviewFeedbackReadiness.COMPLETED)
    assert result.settled_no_actionable_feedback is (admitted and readiness is CodexReviewFeedbackReadiness.CURRENT_TASK_SETTLED)
    assert result.acknowledged_no_actionable_feedback is False
    assert result.deferred is True
    assert result.delivered is False
    assert result.diagnostic_reason == "unclassified_same_author_reply"
    if case in {"not_requested", "truncated", "pending"}:
        completed.assert_not_called()
    else:
        completed.assert_called_once()
        assert completed.call_args.args[1:3] == (TASK, (identity,))
        assert completed.call_args.args[3].head_sha == HEAD
    send.assert_not_called()
    assert state.read_bytes() == before
    github.add_comment_to_pr.assert_not_called()
    github.resolve_review_thread.assert_not_called()
    github.reply_to_review_thread.assert_not_called()


@pytest.mark.parametrize(
    "case, expects_validation, readiness",
    [
        (case, expects_validation, readiness)
        for readiness in (CodexReviewFeedbackReadiness.COMPLETED, CodexReviewFeedbackReadiness.CURRENT_TASK_SETTLED)
        for case, expects_validation in (
            ("completed", True),
            ("completed_post_codex", True),
            ("changed_post_codex", False),
            ("not_completed", False),
            ("truncated", False),
            ("review_cap", False),
            ("saved_verdict", False),
        )
    ]
    + [("later_failed", True, CodexReviewFeedbackReadiness.COMPLETED)],
)
def test_completed_cloud_feedback_reaches_first_current_head_validation(tmp_path, monkeypatch, case, expects_validation, readiness):
    """The real receipt-only delegation path reaches one validator without closing ordinary roots."""
    from auto_coder.execution_trace import Outcome, get_trace_collector
    from tests.test_dashboard_observability import _mounted_detail

    thread = _thread()
    if case == "truncated":
        thread.comments_truncated = True
    state = ClaimedReviewThreadGateState(unresolved=(thread,), blocking_unresolved=(thread,), has_blocking_unresolved=True)
    after_codex = state
    if case == "changed_post_codex":
        changed = ReviewThread(id=thread.id, is_resolved=False, comments=[*thread.comments, ReviewThreadComment(database_id=103, body="A new reviewer finding arrived.", author_login="another-reviewer", author_id=3)])
        after_codex = ClaimedReviewThreadGateState(unresolved=(changed,), blocking_unresolved=(changed,), has_blocking_unresolved=True)
    codex_completed = case in {"completed_post_codex", "changed_post_codex"}
    states = [state, after_codex, state] if codex_completed else [state, state]
    github = _client()
    github.get_pr_review_threads_strict.return_value = [thread]
    identity = _review_feedback_identity(f"{REPOSITORY}#{PR}:codex-cloud:{TASK}:", thread, 0)
    receipts = tmp_path / "review-repairs.json"
    _record_review_feedback_state(receipts, {identity}, set())
    before = receipts.read_bytes()
    config = AutomationConfig()
    config.AUTO_MERGE = True
    config.ENABLE_ADVERSARIAL_VALIDATION = True
    config.MAX_ADVERSARIAL_VALIDATIONS = 0 if case == "review_cap" else 10
    collector = get_trace_collector()
    worktree = tmp_path / "review-workspace"
    worktree.mkdir()
    with ExitStack() as stack:
        stack.enter_context(_simulated_review_source(worktree, HEAD))
        patches = {
            "CloudManager.get_binding": {"return_value": CloudTaskBinding(provider="codex-cloud", task_id=TASK)},
            "_cloud_review_repair_state_path": {"return_value": receipts},
            "check_github_actions_and_exit_if_in_progress": {"return_value": True},
            "_get_mergeable_state": {"return_value": {"mergeable": True, "merge_state_status": "clean"}},
            "_check_github_actions_status": {"side_effect": authoritative_ci_status(success=True, ids=[1])},
            "_get_claimed_review_thread_state": {"side_effect": states},
            "_get_codex_review_state": {"return_value": CodexReviewState(present=codex_completed, completed=codex_completed)},
            "_get_adversarial_validation_eligibility": {"return_value": AdversarialValidationEligibility(issue_numbers=(5983,))},
            "_get_published_adversarial_validation_status": {"return_value": ("NEEDS_TESTS" if case == "saved_verdict" else None, None)},
            "_get_published_adversarial_validation_comment": {"return_value": ("Existing exact-head NEEDS_TESTS report", None)},
            "_replay_saved_nonpass_review": {"return_value": (CloudReviewRepairResult(["Retained correction remains pending"], deferred=True), False)},
            "run_adversarial_validation": {"return_value": AdversarialValidationResult(result="PASS", summary="The independently examined current implementation satisfies its requirements")},
            "publish_adversarial_review": {"return_value": ReviewPublicationResult(True, "COMMENT", "")},
            "resolve_addressed_review_threads": {"return_value": []},
            "isolated_pr_head_worktree": {},
            "_merge_pr": {},
        }
        mocks = {name: stack.enter_context(patch("auto_coder.pr_processor." + name, **kwargs)) for name, kwargs in patches.items()}
        mocks["isolated_pr_head_worktree"].return_value.__enter__.return_value = str(worktree)
        native = None
        if case == "later_failed":
            from auto_coder.pr_processor import CloudTaskOrigin, CloudTaskOriginResolution
            from auto_coder.pr_repair import ExistingPrRepairTarget
            from tests.test_codex_failed_task_review_feedback import install_failed_feedback_evidence

            target = ExistingPrRepairTarget(REPOSITORY, PR, "feature", "main", HEAD)
            native = install_failed_feedback_evidence(monkeypatch, tmp_path / "native-followups.json", REPOSITORY, TASK, target, identity)
            native_before = native[1].read_bytes()
            origin = CloudTaskOrigin(provider="codex-cloud", task_id=TASK, client=native[0])
            stack.enter_context(patch("auto_coder.pr_processor._resolve_cloud_task_origin", return_value=CloudTaskOriginResolution(origin=origin)))
            completed = None
        else:
            completed = stack.enter_context(patch("auto_coder.codex_cloud_client.CodexCloudClient.observe_review_feedback_readiness", return_value=CodexReviewFeedbackReadiness.WAITING if case == "not_completed" else readiness))
        send = stack.enter_context(patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup"))
        execution = stack.enter_context(collector.start_execution(REPOSITORY, "pr", PR, origin="worker"))
        actions = _handle_pr_merge(github, REPOSITORY, _pr(), config, {})

    events = [event for event in collector.get_snapshot(repository=REPOSITORY, item_type="pr", item_number=PR).events if event.execution_id == execution.scope.execution_id]
    expected_effect = "completed-cloud-review-reassessment" if readiness is CodexReviewFeedbackReadiness.COMPLETED else "settled-cloud-task-reassessment"
    reassessments = [event for event in events if event.stage_id == "pr.review-thread-gate" and event.facts.get("effect") == expected_effect]
    assert not any((event.facts or {}).get("effect") == ("settled-cloud-task-reassessment" if readiness is CodexReviewFeedbackReadiness.COMPLETED else "completed-cloud-review-reassessment") for event in events)
    if expects_validation:
        mocks["run_adversarial_validation"].assert_called_once()
        assert mocks["run_adversarial_validation"].call_args.kwargs["execution_cwd"] == str(worktree)
        assert thread.id not in mocks["run_adversarial_validation"].call_args.kwargs["claimed_review_threads_section"]
        assert len(reassessments) == 1
        assert reassessments[0].outcome == Outcome.DEFERRED
        assert reassessments[0].facts["examined_head"] == HEAD
        assert reassessments[0].facts["blocking_thread_ids"] == [thread.id]
        assert any("unresolved review threads remain at the merge boundary" in action for action in actions)
        with patch("auto_coder.dashboard.ui") as ui:
            diagram = _mounted_detail(ui, "pr", PR)
        assert "review-thread gate" in diagram
        assert "deferred" in diagram
    else:
        mocks["run_adversarial_validation"].assert_not_called()
    if case == "saved_verdict":
        assert completed is not None
        completed.assert_not_called()
        mocks["_replay_saved_nonpass_review"].assert_called_once()
        assert reassessments == []
    if native is not None:
        assert native[3].call_count >= 1
        assert native[5].call_count >= 2
        assert native[1].read_bytes() == native_before
        native[0].send_followup.assert_not_called()
        native[0].wham_client.send_follow_up.assert_not_called()
        assert native[6].current_assistant_turn.status == "failed"
    mocks["_merge_pr"].assert_not_called()
    send.assert_not_called()
    github.resolve_review_thread.assert_not_called()
    assert state.claimed == ()
    assert thread.is_resolved is False
    after = json.loads(receipts.read_text())
    prior = json.loads(before)
    assert after["delivered_feedback"] == prior["delivered_feedback"]
    assert after["pending_feedback"] == prior["pending_feedback"]
