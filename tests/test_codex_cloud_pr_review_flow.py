"""Tests for assigning unresolved PR review work to Codex Cloud."""

import json
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.adversarial_validator import AdversarialValidationResult
from auto_coder.automation_config import AutomationConfig, ProcessedPRResult, PRProcessingOutcome
from auto_coder.cloud_manager import CloudTaskBinding
from auto_coder.github_app_reviewer import ReviewPublicationResult
from auto_coder.pr_processor import (
    AdversarialValidationEligibility,
    ClaimedReviewThreadGateState,
    CloudReviewRepairResult,
    CodexReviewState,
    _cloud_review_feedback_markers,
    _delegate_cloud_review_thread_repair,
    _handle_pr_merge,
    _process_pr_for_merge,
    _record_pr_stage,
    _review_feedback_identity,
    _take_pr_actions,
)
from auto_coder.review_thread_validation import ClaimedReviewThread
from auto_coder.util.gh_cache import PullRequestRepairMetadata, ReviewThread, ReviewThreadComment
from auto_coder.util.github_action import GitHubActionsStatusResult
from tests.ci_observation_fakes import authoritative_ci_status


@pytest.fixture(autouse=True)
def durable_codex_cloud_origin():
    """Create the durable ownership origin required by review repair."""
    with patch(
        "auto_coder.pr_processor.CloudManager.get_binding",
        return_value=CloudTaskBinding(provider="codex-cloud", task_id="task_e_review5262"),
    ):
        yield


def _github_client(head_sha: str = "head-1", head_ref: str = "codex/review-5262") -> MagicMock:
    client = MagicMock()
    client.get_pr_comments.return_value = []
    client.get_pull_request_repair_metadata_strict.return_value = PullRequestRepairMetadata(head_ref=head_ref, head_sha=head_sha, base_ref="main")
    return client


def _pr_data(head_sha: str = "head-1") -> dict:
    return {
        "number": 5262,
        "body": "Fixes #42\n\nhttps://chatgpt.com/codex/tasks/task_e_review5262",
        "head": {"ref": "codex/review-5262", "sha": head_sha},
        "base": {"ref": "main", "sha": "base-1"},
        "labels": [],
        "user": {"login": "maintainer"},
    }


def _thread(reply: str = "Please cover the edge case") -> ReviewThread:
    return ReviewThread(
        id="PRRT_thread_1",
        is_resolved=False,
        comments=[
            ReviewThreadComment(
                database_id=101,
                body="This misses the empty-input case",
                author_login="reviewer",
            ),
            ReviewThreadComment(database_id=102, body=reply, author_login="maintainer"),
        ],
    )


def test_review_repair_uses_existing_task_and_deduplicates_unchanged_state(tmp_path) -> None:
    state_path = tmp_path / "review-repairs.json"

    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=state_path),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send_followup,
    ):
        first = _delegate_cloud_review_thread_repair(
            "owner/repo",
            _pr_data(),
            github_client=_github_client(),
            unresolved_threads=(_thread(),),
        )
        second = _delegate_cloud_review_thread_repair(
            "owner/repo",
            _pr_data(),
            github_client=_github_client(),
            unresolved_threads=(_thread(),),
        )

    assert first == ["Requested Codex Cloud task 'task_e_review5262' to address unresolved review threads for PR #5262"]
    assert second == ["Codex Cloud review repair was already requested for PR #5262 for all current actionable feedback"]
    assert first.delivered is True
    assert second.delivered is False
    assert second.deferred is True
    send_followup.assert_called_once()
    task_id, prompt = send_followup.call_args.args[:2]
    assert task_id == "task_e_review5262"
    assert "existing pull request #5262" in prompt
    assert "codex/review-5262" in prompt
    assert "Do not create a new pull request." in prompt
    assert "Address only the newly delivered actionable reviewer feedback" in prompt
    assert "This misses the empty-input case" in prompt
    assert "<!-- auto-coder-review-addressed:v1 -->" in prompt
    assert state_path.exists()


def test_implementer_reply_and_head_change_do_not_create_new_repair_work(tmp_path) -> None:
    state_path = tmp_path / "review-repairs.json"

    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=state_path),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send_followup,
    ):
        _delegate_cloud_review_thread_repair(
            "owner/repo",
            _pr_data(),
            github_client=_github_client(),
            unresolved_threads=(_thread("First clarification"),),
        )
        _delegate_cloud_review_thread_repair(
            "owner/repo",
            _pr_data("head-2"),
            github_client=_github_client("head-2"),
            unresolved_threads=(_thread("Updated clarification"),),
        )

    send_followup.assert_called_once()


def test_new_review_finding_is_delivered_without_redelivering_old_finding(tmp_path) -> None:
    state_path = tmp_path / "review-repairs.json"
    second = ReviewThread(
        id="PRRT_thread_2",
        is_resolved=False,
        comments=[ReviewThreadComment(database_id=201, body="Handle whitespace-only input", author_login="reviewer")],
    )

    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=state_path),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send_followup,
    ):
        _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=_github_client(), unresolved_threads=(_thread(),))
        _delegate_cloud_review_thread_repair("owner/repo", _pr_data("head-2"), github_client=_github_client(), unresolved_threads=(_thread(), second))
        _delegate_cloud_review_thread_repair("owner/repo", _pr_data("head-2"), github_client=_github_client(), unresolved_threads=(_thread(), second))

    assert send_followup.call_count == 2
    second_prompt = send_followup.call_args.args[1]
    assert "Handle whitespace-only input" in second_prompt
    assert "This misses the empty-input case" not in second_prompt


def test_new_reviewer_reply_is_delivered_once_without_redelivering_thread_root(tmp_path) -> None:
    state_path = tmp_path / "review-repairs.json"
    original = _thread("Implemented and tested")
    with_new_review = _thread("Implemented and tested")
    with_new_review.comments.append(ReviewThreadComment(database_id=103, body="Whitespace-only input is still broken", author_login="reviewer"))

    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=state_path),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send_followup,
    ):
        _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=_github_client(), unresolved_threads=(original,))
        _delegate_cloud_review_thread_repair("owner/repo", _pr_data("head-2"), github_client=_github_client(), unresolved_threads=(with_new_review,))
        _delegate_cloud_review_thread_repair("owner/repo", _pr_data("head-2"), github_client=_github_client(), unresolved_threads=(with_new_review,))

    assert send_followup.call_count == 2
    second_prompt = send_followup.call_args.args[1]
    assert "Whitespace-only input is still broken" in second_prompt
    assert "This misses the empty-input case" not in second_prompt
    assert "Implemented and tested" not in second_prompt


def test_distinct_findings_with_identical_text_are_each_delivered_once(tmp_path) -> None:
    state_path = tmp_path / "review-repairs.json"
    first = ReviewThread(
        id="PRRT_thread_1",
        comments=[ReviewThreadComment(database_id=101, body="Same actionable text", author_login="reviewer")],
    )
    second = ReviewThread(
        id="PRRT_thread_2",
        comments=[ReviewThreadComment(database_id=201, body="Same actionable text", author_login="reviewer")],
    )

    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=state_path),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send_followup,
    ):
        _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=_github_client(), unresolved_threads=(first,))
        _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=_github_client(), unresolved_threads=(first, second))
        _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=_github_client(), unresolved_threads=(first, second))

    assert send_followup.call_count == 2
    assert "Thread `PRRT_thread_2`:\nSame actionable text" in send_followup.call_args.args[1]
    assert "Thread `PRRT_thread_1`" not in send_followup.call_args.args[1]


@pytest.mark.parametrize("receipt_unavailable", [False, True])
def test_unconfirmed_review_feedback_delivery_is_not_blindly_retried(tmp_path, receipt_unavailable) -> None:
    from auto_coder.codex_wham_client import FollowUpDeliveryOutcome

    state_path = tmp_path / "review-repairs.json"
    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=state_path),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.get_followup_delivery") as delivery,
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", side_effect=[False, True]) as send_followup,
    ):

        def read_receipt(_task_id, _identity):
            if receipt_unavailable and send_followup.call_count:
                raise OSError("receipt unavailable after POST")
            # The first request is genuinely new; uncertainty starts only
            # after its attempted POST, rather than preexisting in the journal.
            return FollowUpDeliveryOutcome.INDETERMINATE if send_followup.call_count else FollowUpDeliveryOutcome.NOT_DELIVERED

        delivery.side_effect = read_receipt
        _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=_github_client(), unresolved_threads=(_thread(),))
        _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=_github_client(), unresolved_threads=(_thread(),))

    assert send_followup.call_count == 1
    assert json.loads(state_path.read_text())["pending_feedback"]
    assert state_path.exists()


def test_implementer_cannot_forge_a_cloud_delivery_receipt(tmp_path) -> None:
    state_path = tmp_path / "review-repairs.json"
    thread = _thread()
    prefix = "owner/repo#5262:task_e_review5262:"
    identity = _review_feedback_identity(prefix, thread, 0)
    client = _github_client()
    client.get_authenticated_user_login.return_value = "auto-coder-bot"
    client.get_pr_comments.return_value = [
        {
            "body": _cloud_review_feedback_markers([identity]),
            "user": {"login": "maintainer"},
        }
    ]

    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=state_path),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send_followup,
    ):
        actions = _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=client, unresolved_threads=(thread,))

    send_followup.assert_called_once()
    assert actions == ["Requested Codex Cloud task 'task_e_review5262' to address unresolved review threads for PR #5262"]


def test_non_codex_pr_preserves_generic_review_gate() -> None:
    pr_data = _pr_data()
    pr_data["body"] = "Fixes #42"

    with (
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=None),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup") as send_followup,
    ):
        actions = _delegate_cloud_review_thread_repair("owner/repo", pr_data)

    assert actions == ["Review repair was not delivered for PR #5262: no provider-owned cloud task association was found"]
    assert actions.delivered is False
    send_followup.assert_not_called()


def test_single_pr_merge_entry_validates_claimed_thread_without_redelegating() -> None:
    client = MagicMock()
    client.get_pull_request.return_value = {"head": {"sha": "head-1"}}
    claimed = ClaimedReviewThread(
        thread_id="PRRT_thread_1",
        root_comment_database_id=101,
        root_author_login="chatgpt-codex-connector[bot]",
        original_finding="This misses the empty-input case",
        discussion="agent: fixed and tested",
    )
    config = AutomationConfig()
    config.AUTO_MERGE = True
    config.MAX_ADVERSARIAL_VALIDATIONS = -1

    with (
        patch("auto_coder.pr_processor.GitHubClient.get_instance", return_value=client),
        patch("auto_coder.pr_processor.LabelManager") as label_manager,
        patch("auto_coder.pr_processor.retry_pending_stale_review_thread_rollbacks", return_value=[]),
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor._check_github_actions_status", side_effect=authoritative_ci_status(success=True, ids=[1])),
        patch(
            "auto_coder.pr_processor._get_claimed_review_thread_state",
            return_value=ClaimedReviewThreadGateState(claimed=(claimed,)),
        ),
        patch(
            "auto_coder.pr_processor._get_adversarial_validation_eligibility",
            return_value=AdversarialValidationEligibility(issue_numbers=(42,)),
        ),
        patch("auto_coder.pr_processor._get_codex_review_state", return_value=CodexReviewState()),
        patch("auto_coder.pr_processor._get_published_adversarial_validation_status", return_value=(None, None)),
        patch("auto_coder.pr_processor.isolated_pr_head_worktree") as worktree,
        patch(
            "auto_coder.pr_processor.run_adversarial_validation",
            return_value=AdversarialValidationResult(result="PASS", summary="Pass", findings=[]),
        ) as run_validation,
        patch(
            "auto_coder.pr_processor.publish_adversarial_review",
            return_value=ReviewPublicationResult(True, "APPROVE", ""),
        ),
        patch("auto_coder.pr_processor._merge_pr", return_value=False) as merge,
        patch("auto_coder.pr_processor._record_pr_stage", wraps=_record_pr_stage) as stages,
        patch("auto_coder.pr_processor._delegate_cloud_review_thread_repair") as delegate_review_repair,
    ):
        label_manager.return_value.__enter__.return_value = MagicMock()
        worktree.return_value.__enter__.return_value = "/tmp/worktree"
        result = _process_pr_for_merge("owner/repo", _pr_data(), config)

    assert any("claimed-addressed review thread" in action for action in result.actions_taken)
    # A PR-level PASS without a disposition cannot complete the selected thread.
    assert result.outcome is PRProcessingOutcome.FAILED, result.actions_taken
    assert result.error == "Review-thread closure remains unfinished for 1 thread(s): PRRT_thread_1"
    assert any("Review-thread closure incomplete" in action for action in result.actions_taken)
    assert not any("Adversarial validation passed" in action for action in result.actions_taken)
    closure_stages = [call for call in stages.call_args_list if call.args[1] == "pr.review-thread-closure"]
    assert len(closure_stages) == 1
    closure_facts = closure_stages[0].args[4]
    assert closure_facts["confirmed_count"] == 0
    assert closure_facts["unfinished_count"] == 1
    assert closure_facts["unfinished"] == [
        {
            "thread_id": claimed.thread_id,
            "phase": "independent-decision",
            "reason": "Disposition omitted from validator response",
            "decision": "MISSING",
            "acceptance_state": "NOT_ACCEPTED",
            "effect_state": "NOT_ATTEMPTED",
        }
    ]
    run_validation.assert_called_once()
    assert "PRRT_thread_1" in run_validation.call_args.kwargs["claimed_review_threads_section"]
    merge.assert_not_called()
    client.reply_to_review_thread.assert_not_called()
    client.resolve_review_thread.assert_not_called()
    delegate_review_repair.assert_not_called()


@patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
@patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
@patch("auto_coder.pr_processor._check_github_actions_status")
@patch("auto_coder.pr_processor._get_claimed_review_thread_state")
@patch("auto_coder.pr_processor._delegate_cloud_review_thread_repair")
def test_passing_codex_pr_delegates_blocking_review_threads(
    delegate_review_repair,
    claimed_state,
    checks,
    _mergeable,
    _continue,
) -> None:
    thread = _thread()
    checks.side_effect = authoritative_ci_status(success=True, ids=[1])
    claimed_state.return_value = ClaimedReviewThreadGateState(
        unresolved=(thread,),
        blocking_unresolved=(thread,),
        has_blocking_unresolved=True,
    )
    delegate_review_repair.return_value = CloudReviewRepairResult(
        ["Requested Codex Cloud task 'task_e_review5262' to address unresolved review threads for PR #5262"],
        delivered=True,
    )
    config = AutomationConfig()
    config.AUTO_MERGE = True
    client = MagicMock()

    actions = _handle_pr_merge(client, "owner/repo", _pr_data(), config, {})

    assert actions == [
        "All GitHub Actions checks passed for PR #5262",
        "Skipping merge for PR #5262 due to unresolved review threads",
        "Requested Codex Cloud task 'task_e_review5262' to address unresolved review threads for PR #5262",
    ]
    delegate_review_repair.assert_called_once_with(
        "owner/repo",
        _pr_data(),
        github_client=client,
        unresolved_threads=(thread,),
    )


def test_passing_jules_pr_routes_review_repair_from_durable_origin(tmp_path) -> None:
    """Exercise the production merge gate through ownership and transport."""
    repair = _thread()
    provenance = ReviewThread(
        id="PRRT_provenance",
        comments=[ReviewThreadComment(database_id=301, body="<!-- auto-coder-change-provenance-clarification:v1 -->\nWhich commit introduced this?", author_login="reviewer")],
    )
    client = _github_client(head_sha="head-2", head_ref="jules/live-repair-5262")
    jules = MagicMock()
    jules.send_followup.return_value = True
    config = AutomationConfig()
    config.AUTO_MERGE = True
    pr_data = _pr_data()
    pr_data["user"] = {"login": "google-labs-jules[bot]"}

    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=tmp_path / "repairs.json"),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.pr_processor._check_github_actions_status", side_effect=authoritative_ci_status(success=True, ids=[1])),
        patch(
            "auto_coder.pr_processor._get_claimed_review_thread_state",
            return_value=ClaimedReviewThreadGateState(unresolved=(repair, provenance), blocking_unresolved=(repair, provenance), has_blocking_unresolved=True),
        ),
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=CloudTaskBinding(provider="jules", task_id="existing-jules-session")),
        patch("auto_coder.cloud_task_engine.CloudTaskEngine.get_client_for_provider", return_value=jules),
    ):
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

    jules.send_followup.assert_called_once()
    task_id, prompt = jules.send_followup.call_args.args
    assert task_id == "existing-jules-session"
    assert "existing pull request #5262" in prompt
    assert "jules/live-repair-5262" in prompt
    assert "head-2" in prompt
    assert "head-1" not in prompt
    assert "codex/review-5262" not in prompt
    assert "This misses the empty-input case" in prompt
    assert "Which commit introduced this?" not in prompt
    assert any("Requested Jules task 'existing-jules-session'" in action for action in actions)


def test_unresolved_repair_without_durable_owner_fails_processing_visibly() -> None:
    thread = _thread()
    client = MagicMock()
    config = AutomationConfig()
    config.AUTO_MERGE = True
    status = ProcessedPRResult(pr_data=_pr_data())

    with (
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.pr_processor._check_github_actions_status", side_effect=authoritative_ci_status(success=True, ids=[1])),
        patch(
            "auto_coder.pr_processor._get_claimed_review_thread_state",
            return_value=ClaimedReviewThreadGateState(unresolved=(thread,), blocking_unresolved=(thread,), has_blocking_unresolved=True),
        ),
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=None),
    ):
        actions = _take_pr_actions(client, "owner/repo", _pr_data(), config, status)

    assert status.outcome is PRProcessingOutcome.FAILED
    assert status.error == "Review repair was not delivered for PR #5262: no provider-owned cloud task association was found"
    assert status.error in actions
    assert not any("processing deferred" in action for action in actions)


@pytest.mark.parametrize("shared_account_guidance", [False, True])
def test_accepted_repair_without_any_durable_receipt_is_not_redelivered(tmp_path, shared_account_guidance) -> None:
    """A transport success with indeterminate persistence fails closed across runs."""
    thread = _thread()
    if shared_account_guidance:
        from auto_coder.review_feedback_marker import REVIEW_GUIDANCE_MARKER

        thread.comments = [ReviewThreadComment(database_id=104, body="Retain the required oracle\n" + REVIEW_GUIDANCE_MARKER, author_login="maintainer")]
        thread.comments.insert(0, ReviewThreadComment(database_id=101, body="Old finding", author_login="maintainer"))
    client = _github_client()
    if shared_account_guidance:
        client.get_authenticated_user_login.return_value = "controller"
        identity = _review_feedback_identity("owner/repo#5262:jules:existing-jules-session:", thread, 0)
        client.get_pr_comments.return_value = [{"user": {"login": "controller"}, "body": _cloud_review_feedback_markers([identity])}]
    provider = MagicMock()
    provider.send_followup.return_value = True
    config = AutomationConfig()
    config.AUTO_MERGE = True

    common_patches = (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=tmp_path / "repairs.json"),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.pr_processor._check_github_actions_status", side_effect=authoritative_ci_status(success=True, ids=[1])),
        patch(
            "auto_coder.pr_processor._get_claimed_review_thread_state",
            return_value=ClaimedReviewThreadGateState(unresolved=(thread,), blocking_unresolved=(thread,), has_blocking_unresolved=True),
        ),
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=CloudTaskBinding(provider="jules", task_id="existing-jules-session")),
        patch("auto_coder.cloud_task_engine.CloudTaskEngine.get_client_for_provider", return_value=provider),
        patch("auto_coder.pr_processor._record_delivered_review_feedback", side_effect=OSError("local disk unavailable")),
        patch("auto_coder.pr_processor._record_pr_delivered_review_feedback", side_effect=RuntimeError("GitHub receipt unavailable")),
    )

    with common_patches[0], common_patches[1], common_patches[2], common_patches[3], common_patches[4], common_patches[5], common_patches[6], common_patches[7], common_patches[8]:
        first_status = ProcessedPRResult(pr_data=_pr_data())
        first_actions = _take_pr_actions(client, "owner/repo", _pr_data(), config, first_status)
        second_status = ProcessedPRResult(pr_data=_pr_data())
        second_actions = _take_pr_actions(client, "owner/repo", _pr_data(), config, second_status)

    provider.send_followup.assert_called_once()
    assert first_status.outcome is PRProcessingOutcome.FAILED
    assert any("durable delivery confirmation failed" in action for action in first_actions)
    assert second_status.outcome is PRProcessingOutcome.FAILED
    assert any("duplicate delivery was suppressed" in action for action in second_actions)
    persisted = json.loads((tmp_path / "repairs.json").read_text(encoding="utf-8"))
    assert persisted["delivered_feedback"] == ([identity] if shared_account_guidance else [])
    assert len(persisted["pending_feedback"]) == 1


def test_new_finding_remains_deliverable_beside_indeterminate_old_finding(tmp_path) -> None:
    old_finding = _thread()
    new_finding = ReviewThread(
        id="PRRT_thread_2",
        comments=[ReviewThreadComment(database_id=201, body="A distinct new defect", author_login="reviewer")],
    )
    client = _github_client()
    provider = MagicMock()
    provider.send_followup.return_value = True

    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=tmp_path / "repairs.json"),
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=CloudTaskBinding(provider="jules", task_id="existing-jules-session")),
        patch("auto_coder.cloud_task_engine.CloudTaskEngine.get_client_for_provider", return_value=provider),
        patch("auto_coder.pr_processor._record_delivered_review_feedback", side_effect=OSError("local disk unavailable")),
        patch("auto_coder.pr_processor._record_pr_delivered_review_feedback", side_effect=RuntimeError("GitHub receipt unavailable")),
    ):
        first = _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), client, (old_finding,))
        second = _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), client, (old_finding,))
        third = _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), client, (old_finding, new_finding))

    assert first.delivered is False
    assert second.delivered is False
    assert third.delivered is False
    assert provider.send_followup.call_count == 2
    latest_prompt = provider.send_followup.call_args.args[1]
    assert "A distinct new defect" in latest_prompt
    assert "This misses the empty-input case" not in latest_prompt


def test_external_and_managed_feedback_share_the_repair_bundle(tmp_path):
    from auto_coder.canonical_pr_blocker_ledger import BlockerAdmissionPayload, CanonicalPRBlockerLedger, CorrectionScope

    ledger = CanonicalPRBlockerLedger(tmp_path / "blockers.sqlite3")
    snapshot = ledger.initialize_namespace("https://api.github.com", "owner/repo", 5262)
    managed_id, _ = ledger.admit_blocker(
        "https://api.github.com",
        "owner/repo",
        5262,
        "managed",
        snapshot.ledger_revision,
        BlockerAdmissionPayload(category="IMPLEMENTATION", authoritative_boundary="parser", required_correction_outcome="Fix parser", accepted_scope=CorrectionScope(description="Fix parser")),
    )
    managed = ReviewThread(id="managed-thread", comments=[ReviewThreadComment(database_id=301, body=f"### Auto-Coder adversarial finding\nBlocker identity: `{managed_id}`\nFix parser")])
    client = _github_client()
    with (
        patch("auto_coder.pr_processor.CanonicalPRBlockerLedger", return_value=ledger),
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=tmp_path / "repairs.json"),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send_followup,
    ):
        result = _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), client, (managed, _thread()))
    assert result.delivered is True
    send_followup.assert_called_once()
    snapshot = ledger.get_snapshot("https://api.github.com", "owner/repo", 5262)
    external = snapshot.get_blockers_for_alias("github_root_comment", "101")
    assert len(external) == 1
    prompt = send_followup.call_args.args[1]
    assert external[0].blocker_id in prompt
    assert managed_id in prompt
    assert "This misses the empty-input case" in prompt
    bundle = ledger.get_latest_repair_bundle("https://api.github.com", "owner/repo", 5262)
    assert {blocker.blocker_id for blocker in bundle.blockers} == {managed_id, external[0].blocker_id}
    client.resolve_review_thread.assert_not_called()


def test_external_feedback_ledger_failure_prevents_provider_dispatch(tmp_path):
    ledger = MagicMock()
    ledger.initialize_namespace.side_effect = OSError("ledger unavailable")
    with (
        patch("auto_coder.pr_processor.CanonicalPRBlockerLedger", return_value=ledger),
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=tmp_path / "repairs.json"),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup") as send_followup,
    ):
        result = _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), _github_client(), (_thread(),))
    assert result.delivered is False
    assert "external feedback identity is unavailable" in result[0]
    send_followup.assert_not_called()


@pytest.mark.parametrize("root_author", ["maintainer", "reviewer"])
@pytest.mark.parametrize("existing_unmarked, retained_identity", [(False, "none"), (True, "none"), (True, "delivered"), (True, "pending")])
def test_shared_account_guidance_reaches_production_gate_once(tmp_path, root_author, existing_unmarked, retained_identity):
    """Old receipts are waits; only explicit new guidance creates a handoff."""
    from auto_coder.execution_trace import Outcome
    from auto_coder.review_feedback_marker import REVIEW_ADDRESSED_MARKER, REVIEW_GUIDANCE_MARKER

    original = ReviewThread(id="PRRT_shared", comments=[ReviewThreadComment(database_id=701, body="Cover the required recovery boundary", author_login=root_author)])
    acknowledged = ReviewThread(
        id=original.id,
        comments=original.comments
        + [
            ReviewThreadComment(database_id=702, body="Added a focused case\n" + REVIEW_ADDRESSED_MARKER, author_login="maintainer"),
            ReviewThreadComment(database_id=703, body="The complete integration scenarios have not yet been established", author_login="maintainer"),
        ],
    )
    guidance = ReviewThread(
        id=original.id,
        comments=acknowledged.comments + [ReviewThreadComment(database_id=704, body="The loaded resolver still lacks recovery coverage\n" + REVIEW_GUIDANCE_MARKER, author_login="MAINTAINER")],
    )
    unmarked = ReviewThread(
        id=original.id,
        comments=acknowledged.comments + [ReviewThreadComment(database_id=704, body="The loaded resolver still lacks recovery coverage", author_login="MAINTAINER")],
    )
    edited = ReviewThread(
        id=original.id,
        comments=acknowledged.comments + [ReviewThreadComment(database_id=704, body="Clarified wording for the same delivered correction\n" + REVIEW_GUIDANCE_MARKER, author_login="MAINTAINER")],
    )
    polls = (original, acknowledged, unmarked, guidance, edited, guidance) if existing_unmarked else (original, acknowledged, guidance, guidance)
    client = _github_client()
    client.get_authenticated_user_login.return_value = "controller"
    root_identity = _review_feedback_identity("owner/repo#5262:codex-cloud:task_e_review5262:", original, 0)
    guidance_identity = _review_feedback_identity("owner/repo#5262:codex-cloud:task_e_review5262:", guidance, len(guidance.comments) - 1)
    state_path = tmp_path / "repairs.json"
    if retained_identity != "none":
        from auto_coder.pr_processor import _record_review_feedback_state

        delivered = {root_identity, guidance_identity} if retained_identity == "delivered" else {root_identity}
        pending = {guidance_identity} if retained_identity == "pending" else set()
        _record_review_feedback_state(state_path, delivered, pending)
    client.get_pr_comments.return_value = [{"user": {"login": "controller"}, "body": _cloud_review_feedback_markers([root_identity])}]
    config = AutomationConfig()
    config.AUTO_MERGE = True
    state = ClaimedReviewThreadGateState(unresolved=(original,), blocking_unresolved=(original,), has_blocking_unresolved=True)
    statuses = []
    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=tmp_path / "repairs.json"),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.pr_processor._check_github_actions_status", side_effect=authoritative_ci_status(success=True, ids=[1])),
        patch("auto_coder.pr_processor._get_claimed_review_thread_state", return_value=state) as observe_threads,
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send,
        patch("auto_coder.pr_processor._record_pr_stage", wraps=_record_pr_stage) as stages,
        patch("auto_coder.pr_processor._merge_pr") as merge,
    ):
        for thread in polls:
            observe_threads.return_value = ClaimedReviewThreadGateState(unresolved=(thread,), blocking_unresolved=(thread,), has_blocking_unresolved=True)
            status = ProcessedPRResult(pr_data=_pr_data())
            _take_pr_actions(client, "owner/repo", _pr_data(), config, status)
            statuses.append(status)
    assert send.call_count == (1 if retained_identity == "none" else 0)
    if retained_identity == "none":
        prompt = send.call_args.args[1]
        assert "The loaded resolver still lacks recovery coverage" in prompt
        assert "Cover the required recovery boundary" not in prompt
        assert "Added a focused case" not in prompt
        assert "The complete integration scenarios" not in prompt
    delegation = [call.args[3] for call in stages.call_args_list if call.args[1] == "pr.repair-delegation"]
    expected = [Outcome.DEFERRED, Outcome.DEFERRED, Outcome.DEFERRED, Outcome.ACCEPTED_HANDOFF, Outcome.DEFERRED, Outcome.DEFERRED] if existing_unmarked else [Outcome.DEFERRED, Outcome.DEFERRED, Outcome.ACCEPTED_HANDOFF, Outcome.DEFERRED]
    if retained_identity == "delivered":
        expected = [Outcome.DEFERRED] * len(polls)
    elif retained_identity == "pending":
        expected = [Outcome.DEFERRED] * 3 + [Outcome.FAILED] * 3
    assert delegation == expected
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert set(persisted["delivered_feedback"]) == ({root_identity} if retained_identity == "pending" else {root_identity, guidance_identity})
    assert set(persisted["pending_feedback"]) == ({guidance_identity} if retained_identity == "pending" else set())
    if retained_identity == "pending":
        assert all("duplicate delivery was suppressed" in status.error for status in statuses[3:])
        assert statuses[-1].outcome is PRProcessingOutcome.FAILED
    else:
        assert all(status.error is None for status in statuses)
        assert statuses[-1].outcome is PRProcessingOutcome.DEFERRED
    assert statuses[0].outcome is PRProcessingOutcome.DEFERRED
    merge.assert_not_called()
    client.resolve_review_thread.assert_not_called()
    client.reply_to_review_thread.assert_not_called()


@pytest.mark.parametrize(
    "reply_kind, receipt, expected_reason, expected_outcome, sends",
    [
        ("ordinary", "none", "unclassified_same_author_reply", "deferred", 0),
        ("ordinary", "delivered", "actionable_feedback_already_delivered", "deferred", 0),
        ("ordinary", "pending", "actionable_feedback_already_delivered", "deferred", 0),
        ("ordinary", "remote_trusted", "actionable_feedback_already_delivered", "deferred", 0),
        ("ordinary", "remote_untrusted", "unclassified_same_author_reply", "deferred", 0),
        ("addressed", "none", "actionable_feedback_already_delivered", "deferred", 0),
        ("addressed_guidance", "none", "actionable_feedback_already_delivered", "deferred", 0),
        ("third_party", "none", "", "accepted_handoff", 1),
        ("third_party", "delivered", "actionable_feedback_already_delivered", "deferred", 0),
        ("third_party", "pending", "unconfirmed_delivery_receipt", "failed", 0),
        ("guidance", "delivered", "actionable_feedback_already_delivered", "deferred", 0),
        ("guidance", "pending", "unconfirmed_delivery_receipt", "failed", 0),
        ("adjudication", "none", "actionable_feedback_already_delivered", "deferred", 0),
    ],
)
def test_review_delivery_reason_reaches_public_projection_without_changing_effects(tmp_path, monkeypatch, reply_kind, receipt, expected_reason, expected_outcome, sends):
    """Real gate/delegate/trace/projection, controlled GitHub and provider I/O."""
    from auto_coder.execution_trace import TraceCollector, get_trace_collector
    from auto_coder.pr_processor import _reconcile_codex_review_feedback, _record_review_feedback_state
    from auto_coder.public_api import project_event
    from auto_coder.review_feedback_marker import REVIEW_ADDRESSED_MARKER, REVIEW_GUIDANCE_MARKER

    private_prose = "Private discussion payload ghp_" + "a" * 30
    body = private_prose
    if reply_kind in {"addressed", "addressed_guidance"}:
        body += "\n" + REVIEW_ADDRESSED_MARKER
    if reply_kind in {"guidance", "addressed_guidance"}:
        body += "\n" + REVIEW_GUIDANCE_MARKER
    if reply_kind == "adjudication":
        body = '<!-- auto-coder-review-adjudication:v1 -->\n```json\n{"private": "' + private_prose + '"}\n```\n' + REVIEW_GUIDANCE_MARKER
    thread = _thread(body)
    thread.comments[1].author_login = "another-reviewer" if reply_kind == "third_party" else "MAINTAINER"
    prefix = "owner/repo#5262:codex-cloud:task_e_review5262:"
    root_identity = _review_feedback_identity(prefix, thread, 0)
    reply_identity = _review_feedback_identity(prefix, thread, 1)
    state_path = tmp_path / "repairs.json"
    delivered = {root_identity, reply_identity} if receipt == "delivered" else {root_identity}
    pending = {reply_identity} if receipt == "pending" else set()
    remote_only = receipt in {"remote_trusted", "remote_untrusted"}
    if not remote_only:
        _record_review_feedback_state(state_path, delivered, pending)
    before = state_path.read_bytes() if state_path.exists() else None
    client = _github_client()
    client.get_authenticated_user_login.return_value = "controller"
    if remote_only:
        # Same native ID after a delivered guidance comment loses its marker;
        # replacement workers can recover receipts without a local state file.
        thread.comments[1].body += "\n" + REVIEW_GUIDANCE_MARKER
        assert _review_feedback_identity(prefix, thread, 1) == reply_identity
        thread.comments[1].body = body
        client.get_pr_comments.return_value = [
            {"user": {"login": "controller"}, "body": _cloud_review_feedback_markers([root_identity])},
            {"user": {"login": "CoNtRoLlEr" if receipt == "remote_trusted" else "untrusted"}, "body": _cloud_review_feedback_markers([reply_identity])},
        ]
    config = AutomationConfig()
    config.AUTO_MERGE = True
    state = ClaimedReviewThreadGateState(unresolved=(thread,), blocking_unresolved=(thread,), has_blocking_unresolved=True)
    monkeypatch.setattr(TraceCollector, "_instance", None)
    collector = get_trace_collector()
    status = ProcessedPRResult(pr_data=_pr_data())
    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=state_path),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.pr_processor._check_github_actions_status", side_effect=authoritative_ci_status(success=True, ids=[1])),
        patch("auto_coder.pr_processor._get_claimed_review_thread_state", return_value=state),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send,
        patch("auto_coder.pr_processor._reconcile_codex_review_feedback", wraps=_reconcile_codex_review_feedback) as reconcile,
        patch("auto_coder.pr_processor._merge_pr") as merge,
        collector.start_execution("owner/repo", "pr", 5262, origin="worker"),
    ):
        _take_pr_actions(client, "owner/repo", _pr_data(), config, status)
    event = next(event for event in collector.get_snapshot().events if event.stage_id == "pr.repair-delegation")
    assert event.facts["reason"] == expected_reason
    assert event.facts["head_sha"] == "head-1"
    assert event.outcome == expected_outcome
    public = project_event(event, "owner/repo")
    assert public is not None
    assert public.facts == {"reason": expected_reason, "head_sha": "head-1"}
    assert (public.item_type, public.item_number, public.outcome) == ("pr", 5262, expected_outcome)
    assert private_prose not in json.dumps(public.facts)
    assert send.call_count == sends
    if sends == 0:
        assert (state_path.read_bytes() if state_path.exists() else None) == before
        client.add_comment_to_pr.assert_not_called()
    else:
        assert private_prose in send.call_args.args[1]
    if remote_only:
        assert not state_path.exists()
        assert reconcile.call_args.args[3] == []
    assert status.outcome is (PRProcessingOutcome.FAILED if expected_outcome == "failed" else PRProcessingOutcome.DEFERRED)
    merge.assert_not_called()
    client.resolve_review_thread.assert_not_called()
    client.reply_to_review_thread.assert_not_called()


@pytest.mark.parametrize("provider", ["codex-cloud", "claude-routine"])
@pytest.mark.parametrize("bound_draft", [False, True])
def test_accepted_review_handoff_reaches_hourly_checkpoint_on_same_owner(tmp_path, provider, bound_draft):
    """Production receipt creation and duplicate-wait path share one policy."""
    from auto_coder.pr_processor import DraftPRRepairAuthority, draft_pr_repair_authority_scope

    state_path = tmp_path / "review-repairs.json"
    clock = [1000.0]
    client = _github_client()
    live_pr = {**_pr_data(), "state": "open"}
    client.get_pull_request_metadata_strict.return_value = live_pr
    task_id = "task_e_review5262" if provider == "codex-cloud" else "session-5262"
    sender = MagicMock()
    sender.send_followup.return_value = True
    # Native Codex has an identity-aware API; Claude accepts task/text.
    if provider == "codex-cloud":
        provider_patch = patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True)
    else:
        provider_patch = patch("auto_coder.cloud_task_engine.CloudTaskEngine.get_client_for_provider", return_value=sender)
    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=state_path),
        patch("auto_coder.pr_processor.time.time", side_effect=lambda: clock[0]),
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=CloudTaskBinding(provider, task_id)),
        provider_patch as native,
    ):

        def evaluate():
            return _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=client, unresolved_threads=(_thread(),))

        assert evaluate().delivered
        native_sender = native if provider == "codex-cloud" else sender.send_followup
        assert native_sender.call_count == 1
        source = json.loads(state_path.read_text())
        assert source["delivered_feedback"]
        assert len(source["no_commit_series"]) == 1
        clock[0] = 1000 + 59 * 60
        evaluate()
        assert native_sender.call_count == 1
        client.get_pull_request_metadata_strict.assert_not_called()
        authority = DraftPRRepairAuthority("owner/repo", 5262, "codex/review-5262", "head-1", (42,), provider, task_id) if bound_draft else None
        with draft_pr_repair_authority_scope(authority):
            for ordinal in range(1, 4):
                clock[0] = 1000 + ordinal * 3600
                evaluate()
                assert native_sender.call_count == ordinal + 1
                args = native_sender.call_args.args
                assert args[0] == task_id
                assert "independently" in args[1]
                assert "Never make an empty commit" in args[1]
                assert "existing pull request #5262" in args[1]
                assert "codex/review-5262" in args[1]
                if provider == "codex-cloud":
                    assert args[2][0].startswith("no-commit:")
                else:
                    assert len(args) == 2
        clock[0] += 36000
        evaluate()
        assert native_sender.call_count == 4
        assert list(json.loads(state_path.read_text())["no_commit_series"].values())[0]["ordinal"] == 3


@pytest.mark.parametrize("change", ["commit", "closed", "unavailable", "local", "owner"])
def test_due_review_checkpoint_refreshes_scope_before_any_send(tmp_path, change):
    state_path = tmp_path / "review-repairs.json"
    clock = [1000.0]
    github = _github_client()
    live = {**_pr_data(), "state": "open"}
    github.get_pull_request_metadata_strict.return_value = live
    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=state_path),
        patch("auto_coder.pr_processor.time.time", side_effect=lambda: clock[0]),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as sender,
    ):
        _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=github, unresolved_threads=(_thread(),))
        if change == "commit":
            live["head"] = {**live["head"], "sha": "head-2"}
        elif change == "closed":
            live["state"] = "closed"
        elif change == "unavailable":
            github.get_pull_request_metadata_strict.side_effect = RuntimeError("partial observation")
        elif change == "local":
            live["body"] += "\n<!-- auto-coder:local -->"
        else:
            # Ownership changes in the native durable binding during fresh read.
            github.get_pull_request_metadata_strict.side_effect = lambda *_: live
            from auto_coder.pr_processor import CloudTaskOrigin, CloudTaskOriginResolution

            original = __import__("auto_coder.pr_processor", fromlist=["_resolve_cloud_task_origin"])._resolve_cloud_task_origin
            calls = [0]

            def resolve(*args, **kwargs):
                calls[0] += 1
                if calls[0] > 1:
                    return CloudTaskOriginResolution(CloudTaskOrigin("jules", "new-owner", MagicMock()))
                return original(*args, **kwargs)

        clock[0] += 3600
        if change == "owner":
            with patch("auto_coder.pr_processor._resolve_cloud_task_origin", side_effect=resolve):
                _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=github, unresolved_threads=(_thread(),))
        else:
            _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=github, unresolved_threads=(_thread(),))
        assert sender.call_count == 1


def test_explicit_addressed_outcome_waits_for_validation_instead_of_checkpoint(tmp_path):
    state_path = tmp_path / "review-repairs.json"
    clock = [1000.0]
    github = _github_client()
    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=state_path),
        patch("auto_coder.pr_processor.time.time", side_effect=lambda: clock[0]),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as sender,
    ):
        _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=github, unresolved_threads=(_thread(),))
        clock[0] += 3600
        _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=github, unresolved_threads=(_thread("<!-- auto-coder-review-addressed:v1 --> No change is needed; the guard covers this case."),))
        assert sender.call_count == 1
        record = next(iter(json.loads(state_path.read_text())["no_commit_series"].values()))
        assert (record["status"], record["outcome"], record["ordinal"]) == ("ended", "acknowledged_outcome", 0)
        github.get_pull_request_metadata_strict.assert_not_called()


def test_other_pr_unknown_post_does_not_hold_eligible_checkpoint(tmp_path):
    class Interrupted(BaseException):
        pass

    path = tmp_path / "repairs.json"
    clock = [1000.0]
    github = _github_client()
    github.get_pull_request_metadata_strict.return_value = {**_pr_data(), "state": "open"}
    native = MagicMock()
    native.send_followup.side_effect = [Interrupted(), True, True]
    other = {**_pr_data(), "number": 5263}
    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=path),
        patch("auto_coder.pr_processor.time.time", side_effect=lambda: clock[0]),
        patch("auto_coder.pr_processor.CloudManager.get_binding", side_effect=lambda number: CloudTaskBinding("claude-routine", f"session-{number}")),
        patch("auto_coder.cloud_task_engine.CloudTaskEngine.get_client_for_provider", return_value=native),
    ):
        # B's real pre-send reservation survives an interrupted, unknown POST.
        with pytest.raises(Interrupted):
            _delegate_cloud_review_thread_repair("owner/repo", other, github_client=github, unresolved_threads=(_thread(),))
        held = set(json.loads(path.read_text())["pending_feedback"])
        assert held and all(identity.startswith("owner/repo#5263:") for identity in held)
        assert _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=github, unresolved_threads=(_thread(),)).delivered
        clock[0] += 3600
        _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=github, unresolved_threads=(_thread(),))
        assert native.send_followup.call_count == 3
        assert native.send_followup.call_args.args[0] == "session-5262"
        # Reobserving B preserves its fence and never replays the unknown POST.
        result = _delegate_cloud_review_thread_repair("owner/repo", other, github_client=github, unresolved_threads=(_thread(),))
        assert not result.delivered
        assert native.send_followup.call_count == 3
        document = json.loads(path.read_text())
        assert set(document["pending_feedback"]) == held
        assert not held.intersection(document["delivered_feedback"])


@pytest.mark.parametrize("crash", [True, False])
def test_confirmed_original_receipt_and_clock_survive_crash_before_later_promotion(tmp_path, crash):
    class Interrupted(BaseException):
        pass

    path = tmp_path / "repairs.json"
    clock = [1000.0]
    github = _github_client()
    github.get_pull_request_metadata_strict.return_value = {**_pr_data(), "state": "open"}
    native = MagicMock()
    native.send_followup.return_value = True
    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=path),
        patch("auto_coder.pr_processor.time.time", side_effect=lambda: clock[0]),
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=CloudTaskBinding("claude-routine", "session-5262")),
        patch("auto_coder.cloud_task_engine.CloudTaskEngine.get_client_for_provider", return_value=native),
    ):
        with (
            patch("auto_coder.pr_processor._record_delivered_review_feedback", side_effect=Interrupted() if crash else OSError("later receipt write lost")),
            patch("auto_coder.pr_processor._record_pr_delivered_review_feedback", side_effect=RuntimeError("GitHub receipt unavailable")),
        ):
            if crash:
                with pytest.raises(Interrupted):
                    _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=github, unresolved_threads=(_thread(),))
            else:
                assert _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=github, unresolved_threads=(_thread(),)).delivered
        document = json.loads(path.read_text())
        assert document["delivered_feedback"]
        assert document["pending_feedback"] == []
        assert next(iter(document["no_commit_series"].values()))["accepted_at"] == 1000.0
        assert native.send_followup.call_count == 1
        # All in-memory request objects are recreated; native reconciliation is
        # unnecessary for the exact original identities already confirmed.
        clock[0] += 3600
        _delegate_cloud_review_thread_repair("owner/repo", dict(_pr_data()), github_client=github, unresolved_threads=(_thread(),))
        assert native.send_followup.call_count == 2
        assert "independently" in native.send_followup.call_args.args[1]
        assert next(iter(json.loads(path.read_text())["no_commit_series"].values()))["ordinal"] == 1


def test_idle_invalidation_scheduler_reaches_all_three_checkpoints_without_webhooks(tmp_path, monkeypatch):
    import asyncio

    from auto_coder.automation_config import Candidate
    from auto_coder.automation_engine import AutomationEngine, CandidateProcessingResult
    from auto_coder.entity_invalidation import EntityIdentity

    path = tmp_path / "repairs.json"
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    clock = [1000.0]
    github = _github_client()
    data = {**_pr_data(), "state": "open"}
    github.get_pull_request_metadata_strict.return_value = data
    config = AutomationConfig()
    config.AUTO_MERGE = True
    engine = AutomationEngine(github, config)
    native = MagicMock()
    send_times = []
    native.send_followup.side_effect = lambda *_: send_times.append(clock[0]) or True
    thread = _thread()
    no_timer = []
    deadline_reader = engine.invalidations.seconds_until_next_ci

    def expire_existing_timer(repository):
        delay = deadline_reader(repository)
        if delay is None:
            no_timer.append(True)
            return None
        # Deterministically expire the ordinary scheduler's timer at its real
        # durable deadline. No webhook, wake event or candidate scan is injected.
        clock[0] += delay
        return 0.0

    def process(_repository, candidate, **_kwargs):
        actions = _handle_pr_merge(github, "owner/repo", candidate.data, config, {})
        return CandidateProcessingResult(type="pr", number=5262, success=True, actions=actions, outcome=PRProcessingOutcome.DEFERRED)

    monkeypatch.setattr(engine.invalidations, "seconds_until_next_ci", expire_existing_timer)
    monkeypatch.setattr(engine, "_create_candidate_from_single", lambda *_args, **_kwargs: Candidate("pr", dict(data), 0))
    monkeypatch.setattr(engine, "_process_single_candidate", process)
    monkeypatch.setattr(engine, "_check_if_pr_merged_or_closed", lambda *_: False)

    async def wait_until(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.001)

    async def scenario():
        assert engine.invalidations._connection.execute("SELECT count(*) FROM ci_watches").fetchone() == (0,)
        engine.invalidations.invalidate(EntityIdentity("owner/repo", "pr", 5262))
        scheduler = asyncio.create_task(engine._invalidation_loop("owner/repo"))
        worker = None
        try:
            # The scheduler enqueues the initial request, then finds no timers
            # before the worker has had a chance to register the very first watch.
            await wait_until(lambda: no_timer and engine.queue.qsize() == 1)
            worker = asyncio.create_task(engine._worker_loop("owner/repo", 0))
            await wait_until(lambda: len(send_times) == 4 and next(iter(json.loads(path.read_text())["no_commit_series"].values()))["status"] == "accepted")
        finally:
            scheduler.cancel()
            if worker is not None:
                worker.cancel()
            await asyncio.gather(scheduler, *([worker] if worker is not None else []), return_exceptions=True)

    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=path),
        patch("auto_coder.pr_processor.time.time", side_effect=lambda: clock[0]),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.pr_processor._check_github_actions_status", side_effect=authoritative_ci_status(success=True, ids=[1])),
        patch("auto_coder.pr_processor._get_claimed_review_thread_state", return_value=ClaimedReviewThreadGateState(unresolved=(thread,), blocking_unresolved=(thread,), has_blocking_unresolved=True)),
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=CloudTaskBinding("claude-routine", "session-5262")),
        patch("auto_coder.cloud_task_engine.CloudTaskEngine.get_client_for_provider", return_value=native),
    ):
        asyncio.run(scenario())
    assert len(send_times) == 4
    assert all(later - earlier >= 3600 for earlier, later in zip(send_times, send_times[1:]))
    assert next(iter(json.loads(path.read_text())["no_commit_series"].values()))["ordinal"] == 3


def test_jules_is_not_enrolled_in_no_commit_recovery(tmp_path):
    path = tmp_path / "repairs.json"
    clock = [1000.0]
    github = _github_client()
    native = MagicMock()
    native.send_followup.return_value = True
    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=path),
        patch("auto_coder.pr_processor.time.time", side_effect=lambda: clock[0]),
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=CloudTaskBinding("jules", "existing-jules-session")),
        patch("auto_coder.cloud_task_engine.CloudTaskEngine.get_client_for_provider", return_value=native),
    ):
        assert _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=github, unresolved_threads=(_thread(),)).delivered
        for _ in range(4):
            clock[0] += 3600
            _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=github, unresolved_threads=(_thread(),))
    native.send_followup.assert_called_once()
    assert json.loads(path.read_text()).get("no_commit_series", {}) == {}
    github.get_pull_request_metadata_strict.assert_not_called()


def test_stale_peer_write_cannot_erase_accepted_claude_receipt_after_restart(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from auto_coder.pr_no_commit_recovery import NoCommitRecovery
    from auto_coder.pr_processor import _load_delivered_review_feedback, _load_pending_review_feedback, _record_review_feedback_state

    class Interrupted(BaseException):
        pass

    path = tmp_path / "repairs.json"
    clock = [1000.0]
    other_pending = {"owner/repo#5263:claude-routine:session-B:unknown-post"}
    _record_review_feedback_state(path, set(), other_pending)
    entered, peer_loaded, release_peer = Event(), Event(), Event()

    def stale_peer():
        assert entered.wait(5), "A never entered its original POST"
        delivered = _load_delivered_review_feedback(path)
        pending = _load_pending_review_feedback(path)
        peer_loaded.set()
        assert release_peer.wait(5), "peer write was never released"
        _record_review_feedback_state(path, delivered, pending)

    native = MagicMock()

    def accept(_task, _message):
        if native.send_followup.call_count == 1:
            entered.set()
            assert peer_loaded.wait(5), "B never captured the pre-acceptance snapshot"
        return True

    native.send_followup.side_effect = accept
    github = _github_client()
    github.get_pull_request_metadata_strict.return_value = {**_pr_data(), "state": "open"}
    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=path),
        patch("auto_coder.pr_processor.time.time", side_effect=lambda: clock[0]),
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=CloudTaskBinding("claude-routine", "session-5262")),
        patch("auto_coder.cloud_task_engine.CloudTaskEngine.get_client_for_provider", return_value=native),
        ThreadPoolExecutor(max_workers=1) as workers,
    ):
        peer = workers.submit(stale_peer)
        try:
            with patch("auto_coder.pr_processor._record_delivered_review_feedback", side_effect=Interrupted()):
                with pytest.raises(Interrupted):
                    _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=github, unresolved_threads=(_thread(),))
            accepted = _load_delivered_review_feedback(path)
            assert accepted and all(identity.startswith("owner/repo#5262:") for identity in accepted)
            # Reconstruct A from its durable acceptance before B resumes writing
            # the snapshot captured while A's original POST was still pending.
            assert not NoCommitRecovery(path).has_due_request(5262, clock[0])
        finally:
            release_peer.set()
            peer.result(timeout=5)
        document = json.loads(path.read_text())
        assert set(document["delivered_feedback"]) == accepted
        assert set(document["pending_feedback"]) == other_pending
        assert not other_pending.intersection(document["delivered_feedback"])
        assert next(iter(document["no_commit_series"].values()))["accepted_at"] == 1000.0
        clock[0] += 59 * 60
        _delegate_cloud_review_thread_repair("owner/repo", dict(_pr_data()), github_client=github, unresolved_threads=(_thread(),))
        assert native.send_followup.call_count == 1
        clock[0] += 60
        _delegate_cloud_review_thread_repair("owner/repo", dict(_pr_data()), github_client=github, unresolved_threads=(_thread(),))
        assert native.send_followup.call_count == 2
        assert "independently" in native.send_followup.call_args.args[1]
        assert next(iter(json.loads(path.read_text())["no_commit_series"].values()))["ordinal"] == 1


@pytest.mark.parametrize("failure", ["crash", "exception", "false"])
def test_stale_peer_snapshot_before_reservation_cannot_erase_unknown_claude_post(tmp_path, failure):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from auto_coder.pr_processor import _load_delivered_review_feedback, _load_pending_review_feedback, _record_review_feedback_state

    class Interrupted(BaseException):
        pass

    path = tmp_path / "repairs.json"
    _record_review_feedback_state(path, set(), set())
    loaded, release_peer = Event(), Event()

    def stale_peer():
        delivered = _load_delivered_review_feedback(path)
        pending = _load_pending_review_feedback(path)
        assert pending == set()  # B's snapshot strictly precedes A's reservation.
        loaded.set()
        assert release_peer.wait(5), "B's stale write was never released"
        _record_review_feedback_state(path, delivered, pending)

    def unknown_post(*_args):
        if failure == "crash":
            raise Interrupted()
        if failure == "exception":
            raise RuntimeError("POST response lost")
        return False

    native = MagicMock()
    native.send_followup.side_effect = unknown_post
    github = _github_client()
    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=path),
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=CloudTaskBinding("claude-routine", "session-5262")),
        patch("auto_coder.cloud_task_engine.CloudTaskEngine.get_client_for_provider", return_value=native),
        ThreadPoolExecutor(max_workers=1) as workers,
    ):
        peer = workers.submit(stale_peer)
        assert loaded.wait(5), "B never loaded the older snapshot"
        try:
            if failure == "crash":
                with pytest.raises(Interrupted):
                    _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=github, unresolved_threads=(_thread(),))
            else:
                assert not _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=github, unresolved_threads=(_thread(),)).delivered
            unknown = _load_pending_review_feedback(path)
            assert unknown and all(identity.startswith("owner/repo#5262:") for identity in unknown)
        finally:
            release_peer.set()
            peer.result(timeout=5)
        assert _load_pending_review_feedback(path) == unknown
        assert _load_delivered_review_feedback(path) == set()
        # Recreate ordinary processing from disk after A's interruption and B's
        # stale write. A's unknown original must not acquire a second POST.
        result = _delegate_cloud_review_thread_repair("owner/repo", dict(_pr_data()), github_client=github, unresolved_threads=(_thread(),))
        assert not result.delivered
        assert "unconfirmed durable receipt" in result[0]
        native.send_followup.assert_called_once()
        assert not json.loads(path.read_text()).get("no_commit_series")


def test_pre_send_refusal_clears_only_its_own_reservation_and_remains_retryable(tmp_path):
    from auto_coder.pr_processor import _load_delivered_review_feedback, _load_pending_review_feedback, _record_review_feedback_state

    path = tmp_path / "repairs.json"
    peer_pending = {"owner/repo#5263:claude-routine:session-B:unknown-post"}
    github = _github_client()

    def unavailable_metadata(*_args):
        # B reserves after A loaded its snapshot. A's known pre-send cleanup
        # must remove only A's own identifiers, leaving this new claim intact.
        _record_review_feedback_state(path, _load_delivered_review_feedback(path), peer_pending)
        raise RuntimeError("current PR metadata unavailable")

    github.get_pull_request_repair_metadata_strict.side_effect = unavailable_metadata
    native = MagicMock()
    native.send_followup.return_value = True
    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=path),
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=CloudTaskBinding("claude-routine", "session-5262")),
        patch("auto_coder.cloud_task_engine.CloudTaskEngine.get_client_for_provider", return_value=native),
    ):
        refused = _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=github, unresolved_threads=(_thread(),))
        assert not refused.delivered
        native.send_followup.assert_not_called()
        assert _load_pending_review_feedback(path) == peer_pending
        github.get_pull_request_repair_metadata_strict.side_effect = None
        assert _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=github, unresolved_threads=(_thread(),)).delivered
        native.send_followup.assert_called_once()
        assert _load_pending_review_feedback(path) == peer_pending
        assert not peer_pending.intersection(_load_delivered_review_feedback(path))


@pytest.mark.parametrize("failure", [False, RuntimeError("legacy send failed")])
def test_legacy_jules_false_and_exception_remain_retryable_without_enrollment(tmp_path, failure):
    from auto_coder.pr_processor import _load_pending_review_feedback, _record_review_feedback_state

    path = tmp_path / "repairs.json"
    peer_pending = {"owner/repo#5263:claude-routine:session-B:unknown-post"}
    _record_review_feedback_state(path, set(), peer_pending)
    github = _github_client()
    native = MagicMock()
    native.send_followup.side_effect = [failure, True]
    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=path),
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=CloudTaskBinding("jules", "existing-jules-session")),
        patch("auto_coder.cloud_task_engine.CloudTaskEngine.get_client_for_provider", return_value=native),
    ):
        first = _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=github, unresolved_threads=(_thread(),))
        assert not first.delivered
        assert _load_pending_review_feedback(path) == peer_pending
        second = _delegate_cloud_review_thread_repair("owner/repo", dict(_pr_data()), github_client=github, unresolved_threads=(_thread(),))
        assert second.delivered
        assert native.send_followup.call_count == 2
        assert all(len(call.args) == 2 for call in native.send_followup.call_args_list)
        assert _load_pending_review_feedback(path) == peer_pending
        assert not json.loads(path.read_text()).get("no_commit_series")


@pytest.mark.parametrize("outcome", ["rejected", "indeterminate", "lost", "malformed", "mixed"])
def test_native_review_rejection_requires_positive_current_send_evidence(tmp_path, outcome):
    """A native journal lost write or unreadable entry cannot release the shared claim."""
    import hashlib

    from auto_coder.codex_cloud_client import CodexCloudClient
    from auto_coder.codex_wham_client import FollowUpDeliveryOutcome, FollowUpDeliveryResult

    shared_path = tmp_path / "review.json"
    native_path = tmp_path / "native.json"
    wham = MagicMock()
    wham.resolve_latest_assistant_turn.return_value = "task_e_review5262~assttrn_1"
    wham.reconcile_follow_up.return_value = False
    wham.send_follow_up.side_effect = [
        FollowUpDeliveryResult(FollowUpDeliveryOutcome.NOT_DELIVERED if outcome == "rejected" else FollowUpDeliveryOutcome.INDETERMINATE, 400 if outcome == "rejected" else 502),
        FollowUpDeliveryResult(FollowUpDeliveryOutcome.DELIVERED, 200),
    ]
    native_send = CodexCloudClient.send_followup
    sent_identities = []

    def send_then_interleave(client, task_id, message, logical_identities=(), *args, **kwargs):
        result = native_send(client, task_id, message, logical_identities, *args, **kwargs)
        sent_identities.extend(logical_identities)
        if outcome == "lost":
            # Another process replaces a whole-file snapshot missing this key.
            native_path.write_text("{}")
        elif outcome in {"malformed", "mixed"}:
            records = json.loads(native_path.read_text())
            key = hashlib.sha256(f"{task_id}\0{logical_identities[-1]}".encode()).hexdigest()
            records[key] = None
            if outcome == "mixed":
                first_key = hashlib.sha256(f"{task_id}\0{logical_identities[0]}".encode()).hexdigest()
                records[first_key]["status"] = "delivered"
            native_path.write_text(json.dumps(records))
        return result

    threads = (_thread(),)
    if outcome == "mixed":
        threads += (ReviewThread(id="PRRT_second", comments=[ReviewThreadComment(database_id=203, body="Second finding", author_login="reviewer")]),)
    pr_data = _pr_data(head_sha="a" * 40)
    github = _github_client()
    github.get_pull_request_repair_metadata_strict.return_value = PullRequestRepairMetadata("codex/review-5262", "a" * 40, "main")
    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=shared_path),
        patch("auto_coder.codex_cloud_client._codex_followup_state_path", return_value=native_path),
        patch("auto_coder.codex_cloud_client.CodexWhamClient", return_value=wham),
        patch.object(CodexCloudClient, "send_followup", autospec=True, side_effect=send_then_interleave),
    ):
        first = _delegate_cloud_review_thread_repair("owner/repo", pr_data, github, threads)
        assert first.delivered is False
        second = _delegate_cloud_review_thread_repair("owner/repo", pr_data, github, threads)

    if outcome == "rejected":
        assert "rejected follow-up delivery" in first[0]
        assert second.delivered is True
        assert wham.send_follow_up.call_count == 2
    else:
        assert "unconfirmed follow-up delivery" in first[0]
        assert second.delivered is False
        wham.send_follow_up.assert_called_once()
        assert json.loads(shared_path.read_text())["pending_feedback"]
    if outcome == "mixed":
        assert len(sent_identities) == 2


def test_supported_external_feedback_producer_cannot_supply_mixed_split_resolution(tmp_path):
    """Real comment intake admits aliases but cannot produce the required decision.

    No native records or ledger decisions are seeded. The first ordinary send
    becomes unknown at controlled transport; adding distinct unsent feedback
    exposes the missing production producer without replaying the old request.
    """
    from auto_coder.canonical_pr_blocker_ledger import CanonicalPRBlockerLedger
    from auto_coder.codex_wham_client import FollowUpDeliveryOutcome, FollowUpDeliveryResult

    shared = tmp_path / "shared.json"
    native = tmp_path / "native.json"
    ledger = CanonicalPRBlockerLedger(tmp_path / "ledger.sqlite")
    head = "a" * 40
    old = ReviewThread(id="coverage", comments=[ReviewThreadComment(database_id=101, body="Reject incomplete executable-file coverage", author_login="reviewer")])
    new = ReviewThread(id="failure-path", comments=[ReviewThreadComment(database_id=201, body="Preserve the failure-path exit status", author_login="reviewer")])
    prefix = "owner/repo#5262:codex-cloud:task_e_review5262:"
    old_id = _review_feedback_identity(prefix, old, 0)
    new_id = _review_feedback_identity(prefix, new, 0)
    github = _github_client(head_sha=head)
    wham = MagicMock()
    wham.resolve_latest_assistant_turn.return_value = "task_e_review5262~assttrn_1"
    wham.reconcile_follow_up.return_value = False
    wham.send_follow_up.return_value = FollowUpDeliveryResult(FollowUpDeliveryOutcome.INDETERMINATE, 502)
    config = AutomationConfig()
    config.AUTO_MERGE = True
    gate = ClaimedReviewThreadGateState(unresolved=(old,), blocking_unresolved=(old,), has_blocking_unresolved=True)
    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=shared),
        patch("auto_coder.codex_cloud_client._codex_followup_state_path", return_value=native),
        patch("auto_coder.codex_cloud_client.CodexWhamClient", return_value=wham),
        patch("auto_coder.pr_processor.CanonicalPRBlockerLedger", return_value=ledger),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.pr_processor._check_github_actions_status", side_effect=authoritative_ci_status(success=True, ids=[1])),
        patch("auto_coder.pr_processor._get_claimed_review_thread_state", return_value=gate) as thread_gate,
    ):
        first = _handle_pr_merge(github, "owner/repo", _pr_data(head), config, {})
        snapshot = ledger.get_snapshot("https://api.github.com", "owner/repo", 5262)
        assert snapshot.get_blockers_for_alias("external_review_comment", "comment:101")
        assert snapshot.reconciliation_records == ()
        original_native = native.read_bytes()
        thread_gate.return_value = ClaimedReviewThreadGateState(unresolved=(old, new), blocking_unresolved=(old, new), has_blocking_unresolved=True)
        second = _handle_pr_merge(github, "owner/repo", _pr_data(head), config, {})
        third = _handle_pr_merge(github, "owner/repo", _pr_data(head), config, {})
    wham.send_follow_up.assert_called_once()
    assert "Reject incomplete executable-file coverage" in wham.send_follow_up.call_args.kwargs["prompt"]
    assert "Preserve the failure-path exit status" not in wham.send_follow_up.call_args.kwargs["prompt"]
    assert any("unconfirmed follow-up delivery" in action for action in first)
    assert all(any("independent scope resolution" in action for action in actions) for actions in (second, third))
    assert native.read_bytes() == original_native
    state = json.loads(shared.read_text())
    assert state["pending_feedback"] == [old_id]
    assert state["delivered_feedback"] == []
    assert new_id not in state["pending_feedback"]
    snapshot = ledger.get_snapshot("https://api.github.com", "owner/repo", 5262)
    assert snapshot.reconciliation_records == ()
    assert not snapshot.get_blockers_for_alias("external_review_comment", "comment:201")
    github.merge_pull_request.assert_not_called()


def test_addressed_thread_supersedes_prior_indeterminate_and_unsent_findings(tmp_path, monkeypatch):
    """Addressed unresolved threads defer cleanly without retrying or clearing old receipts."""
    from auto_coder.codex_cloud_client import PendingCodexFollowUp, _save_pending_followups
    from auto_coder.execution_trace import Outcome, TraceCollector, get_trace_collector
    from auto_coder.pr_processor import _record_review_feedback_state
    from auto_coder.review_feedback_marker import REVIEW_ADDRESSED_MARKER, REVIEW_GUIDANCE_MARKER

    prefix = "owner/repo#5262:codex-cloud:task_e_review5262:"
    thread_with_indeterminate = ReviewThread(
        id="PRRT_indeterminate_addressed",
        is_resolved=False,
        comments=[
            ReviewThreadComment(database_id=801, body="Initial finding", author_login="maintainer"),
            ReviewThreadComment(database_id=802, body="Fixed initial finding\n\n" + REVIEW_ADDRESSED_MARKER, author_login="maintainer"),
            ReviewThreadComment(database_id=803, body="Follow-up guidance\n\n" + REVIEW_GUIDANCE_MARKER, author_login="maintainer"),
            ReviewThreadComment(database_id=804, body="Unmarked discussion before final guidance", author_login="maintainer"),
            ReviewThreadComment(database_id=805, body="Final guidance\n\n" + REVIEW_GUIDANCE_MARKER, author_login="maintainer"),
            ReviewThreadComment(database_id=806, body="Published fix in commit f6e60fa.\\n\\n" + REVIEW_ADDRESSED_MARKER, author_login="MAINTAINER"),
        ],
    )
    thread_with_unsent_root = ReviewThread(
        id="PRRT_unsent_addressed",
        is_resolved=False,
        comments=[
            ReviewThreadComment(database_id=901, body="Root addressed during CI repair", author_login="reviewer"),
            ReviewThreadComment(database_id=902, body="Addressed with regression\n\n" + REVIEW_ADDRESSED_MARKER, author_login="maintainer"),
        ],
    )
    old_root_id = _review_feedback_identity(prefix, thread_with_indeterminate, 0)
    old_guidance_id = _review_feedback_identity(prefix, thread_with_indeterminate, 2)
    delivered_guidance_id = _review_feedback_identity(prefix, thread_with_indeterminate, 4)

    shared_path = tmp_path / "shared.json"
    native_path = tmp_path / "native.json"
    _record_review_feedback_state(shared_path, {delivered_guidance_id}, {old_root_id, old_guidance_id})
    _save_pending_followups(
        native_path,
        {
            f"key-{idx}": PendingCodexFollowUp(
                task_id="task_e_review5262",
                message_fingerprint="a" * 64,
                pre_send_turn_id="task_e_review5262~assttrn_1",
                status="indeterminate",
                logical_identity=ident,
            )
            for idx, ident in enumerate((old_root_id, old_guidance_id))
        },
    )
    shared_before = shared_path.read_bytes()
    native_before = native_path.read_bytes()

    github = _github_client(head_sha="a" * 40)
    github.get_authenticated_user_login.return_value = "controller"
    wham = MagicMock()
    wham.reconcile_follow_up.return_value = None
    config = AutomationConfig()
    config.AUTO_MERGE = True
    threads = (thread_with_indeterminate, thread_with_unsent_root)
    gate = ClaimedReviewThreadGateState(unresolved=threads, blocking_unresolved=threads, has_blocking_unresolved=True)

    monkeypatch.setattr(TraceCollector, "_instance", None)
    collector = get_trace_collector()
    status = ProcessedPRResult(pr_data=_pr_data("a" * 40))
    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=shared_path),
        patch("auto_coder.codex_cloud_client._codex_followup_state_path", return_value=native_path),
        patch("auto_coder.codex_cloud_client.CodexWhamClient", return_value=wham),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.pr_processor._check_github_actions_status", side_effect=authoritative_ci_status(success=True, ids=[1])),
        patch("auto_coder.pr_processor._get_claimed_review_thread_state", return_value=gate) as observe_threads,
        patch("auto_coder.pr_processor._merge_pr") as merge,
        collector.start_execution("owner/repo", "pr", 5262, origin="worker"),
    ):
        _take_pr_actions(github, "owner/repo", _pr_data("a" * 40), config, status)

        assert status.outcome is PRProcessingOutcome.DEFERRED
        assert status.error is None
        wham.send_follow_up.assert_not_called()
        wham.reconcile_follow_up.assert_not_called()
        assert shared_path.read_bytes() == shared_before
        assert native_path.read_bytes() == native_before
        merge.assert_not_called()
        github.resolve_review_thread.assert_not_called()

        delegation_event = next(event for event in collector.get_snapshot().events if event.stage_id == "pr.repair-delegation")
        assert delegation_event.outcome == Outcome.DEFERRED.value
        assert delegation_event.facts["reason"] == "actionable_feedback_already_delivered"

        # New guidance after the addressed reply is selected again, and the preserved
        # prior indeterminate PR claim still enforces the mixed-feedback guard.
        reopened_thread = ReviewThread(
            id=thread_with_indeterminate.id,
            is_resolved=False,
            comments=thread_with_indeterminate.comments + [ReviewThreadComment(database_id=807, body="New follow-up after addressed claim\n\n" + REVIEW_GUIDANCE_MARKER, author_login="maintainer")],
        )
        observe_threads.return_value = ClaimedReviewThreadGateState(
            unresolved=(reopened_thread, thread_with_unsent_root),
            blocking_unresolved=(reopened_thread, thread_with_unsent_root),
            has_blocking_unresolved=True,
        )
        reopened_status = ProcessedPRResult(pr_data=_pr_data("a" * 40))
        _take_pr_actions(github, "owner/repo", _pr_data("a" * 40), config, reopened_status)

    assert reopened_status.outcome is PRProcessingOutcome.FAILED
    assert reopened_status.error is not None and "independent scope resolution" in reopened_status.error
    wham.send_follow_up.assert_not_called()
    assert shared_path.read_bytes() == shared_before
    assert native_path.read_bytes() == native_before


@pytest.mark.parametrize("case", ["root_marker", "third_party_marker", "adjudication_marker"])
def test_addressed_marker_in_root_third_party_or_adjudication_does_not_suppress_delivery(tmp_path, case):
    """Only a non-root, non-adjudication implementer reply can mark earlier comments addressed."""
    from auto_coder.review_feedback_marker import REVIEW_ADDRESSED_MARKER

    if case == "root_marker":
        thread = ReviewThread(
            id="PRRT_root_marker",
            is_resolved=False,
            comments=[ReviewThreadComment(database_id=951, body="Root quoting marker\n\n" + REVIEW_ADDRESSED_MARKER, author_login="maintainer")],
        )
    elif case == "third_party_marker":
        thread = ReviewThread(
            id="PRRT_third_party_marker",
            is_resolved=False,
            comments=[
                ReviewThreadComment(database_id=952, body="Original finding", author_login="reviewer"),
                ReviewThreadComment(database_id=953, body="Please include " + REVIEW_ADDRESSED_MARKER + " after fixing", author_login="another-reviewer"),
            ],
        )
    else:
        adjudication_body = '<!-- auto-coder-review-adjudication:v1 -->\n```json\n{"rationale": "saw ' + REVIEW_ADDRESSED_MARKER + '"}\n```'
        thread = ReviewThread(
            id="PRRT_adjudication_marker",
            is_resolved=False,
            comments=[
                ReviewThreadComment(database_id=954, body="Original finding", author_login="reviewer"),
                ReviewThreadComment(database_id=955, body=adjudication_body, author_login="maintainer"),
            ],
        )

    state_path = tmp_path / "repairs.json"
    github = _github_client()
    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=state_path),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send_followup,
    ):
        result = _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=github, unresolved_threads=(thread,))

    assert result.delivered is True
    send_followup.assert_called_once()
    assert thread.comments[0].body in send_followup.call_args.args[1]


@pytest.mark.parametrize(
    "case, expected_acknowledgement, expected_sends, expected_reason",
    [
        ("addressed", True, 0, "actionable_feedback_already_delivered"),
        ("escaped_newlines", True, 0, "actionable_feedback_already_delivered"),
        ("no_receipts", True, 0, "actionable_feedback_already_delivered"),
        ("unrelated_pending", True, 0, "actionable_feedback_already_delivered"),
        ("unmarked", False, 0, "unclassified_same_author_reply"),
        ("foreign_marker", False, 1, ""),
        ("truncated", False, 0, "actionable_feedback_already_delivered"),
        ("empty_thread", False, 0, "actionable_feedback_already_delivered"),
        ("no_threads", False, 0, "actionable_feedback_already_delivered"),
        ("new_guidance", False, 1, ""),
        ("delivered_guidance", False, 0, "actionable_feedback_already_delivered"),
        ("retained_pending", False, 0, "actionable_feedback_already_delivered"),
        ("unacknowledged_sibling", False, 0, "actionable_feedback_already_delivered"),
        ("active_handoff", False, 1, ""),
    ],
)
def test_review_repair_acknowledgement_preserves_delivery_and_closure_boundaries(tmp_path, case, expected_acknowledgement, expected_sends, expected_reason):
    """Only complete acknowledged discussions with no observed pending work permit rereview."""
    from auto_coder.pr_processor import _record_review_feedback_state
    from auto_coder.review_feedback_marker import REVIEW_ADDRESSED_MARKER, REVIEW_GUIDANCE_MARKER

    thread = _thread("Implemented and checked the correction\n\n" + REVIEW_ADDRESSED_MARKER)
    prefix = "owner/repo#5262:codex-cloud:task_e_review5262:"
    root_identity = _review_feedback_identity(prefix, thread, 0)
    delivered = {root_identity}
    pending = set()
    threads = (thread,)
    if case == "escaped_newlines":
        thread.comments[1].body = "Published the correction.\\n\\n" + REVIEW_ADDRESSED_MARKER
    elif case == "unmarked":
        thread.comments[1].body = "The implementation appears corrected"
    elif case == "foreign_marker":
        thread.comments[1].author_login = "another-reviewer"
    elif case == "truncated":
        thread.comments_truncated = True
    elif case == "empty_thread":
        thread.comments = []
    elif case == "no_threads":
        threads = ()
    elif case in {"new_guidance", "delivered_guidance"}:
        thread.comments.append(ReviewThreadComment(database_id=103, body="Cover the remaining rollback case\n" + REVIEW_GUIDANCE_MARKER, author_login="maintainer"))
        if case == "delivered_guidance":
            delivered.add(_review_feedback_identity(prefix, thread, 2))
    elif case == "retained_pending":
        delivered = set()
        pending = {root_identity}
    elif case == "unrelated_pending":
        pending = {_review_feedback_identity("owner/repo#5263:codex-cloud:task_other:", thread, 0)}
    elif case == "unacknowledged_sibling":
        sibling = ReviewThread(id="PRRT_unacknowledged", comments=[ReviewThreadComment(database_id=201, body="Another original concern", author_login="reviewer")])
        delivered.add(_review_feedback_identity(prefix, sibling, 0))
        threads = (thread, sibling)
    elif case == "active_handoff":
        thread.comments = thread.comments[:1]
        delivered = set()

    state_path = tmp_path / "review-repairs.json"
    if case != "no_receipts":
        _record_review_feedback_state(state_path, delivered, pending)
    before = state_path.read_bytes() if state_path.exists() else None
    github = _github_client()
    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=state_path),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as sender,
    ):
        result = _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=github, unresolved_threads=threads)

    assert result.acknowledged_no_actionable_feedback is expected_acknowledgement
    assert result.diagnostic_reason == expected_reason
    assert result.delivered is bool(expected_sends)
    assert result.deferred is (not bool(expected_sends))
    assert sender.call_count == expected_sends
    if not expected_sends:
        assert (state_path.read_bytes() if state_path.exists() else None) == before
        github.add_comment_to_pr.assert_not_called()
    github.resolve_review_thread.assert_not_called()
    github.reply_to_review_thread.assert_not_called()


def test_review_repair_native_uncertainty_prevents_acknowledged_admission(tmp_path):
    """A native unknown observed during reconciliation still forbids automatic rereview."""
    from auto_coder.review_feedback_marker import REVIEW_ADDRESSED_MARKER

    thread = _thread("Correction completed\n" + REVIEW_ADDRESSED_MARKER)
    identity = _review_feedback_identity("owner/repo#5262:codex-cloud:task_e_review5262:", thread, 0)
    state_path = tmp_path / "review-repairs.json"
    github = _github_client()

    def observe_unknown(client, provider, task_id, identities, *, uncertain):
        uncertain.add(identity)
        return set()

    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=state_path),
        patch("auto_coder.pr_processor._reconcile_codex_review_feedback", side_effect=observe_unknown),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup") as sender,
    ):
        result = _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=github, unresolved_threads=(thread,))

    assert result.acknowledged_no_actionable_feedback is False
    assert result.diagnostic_reason == "actionable_feedback_already_delivered"
    assert result.delivered is False
    assert result.deferred is True
    assert json.loads(state_path.read_text())["pending_feedback"] == [identity]
    sender.assert_not_called()
    github.resolve_review_thread.assert_not_called()
    github.reply_to_review_thread.assert_not_called()
