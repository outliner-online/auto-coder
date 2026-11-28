"""The PR stop latch fences native Codex sends without blocking old receipts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock

import pytest

from auto_coder import pr_processor
from auto_coder.codex_cloud_client import CodexCloudClient, _codex_followup_state_path, _load_pending_followups
from auto_coder.codex_wham_client import FollowUpDeliveryOutcome, FollowUpDeliveryResult
from auto_coder.codex_work_fence import CodexWorkOutsideScope
from auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration
from auto_coder.pr_automation_guard import PRCommit, PRCommitHistory, PRGuardStore, assess_pr_automation
from auto_coder.pr_repair import ExistingPrRepairTarget, build_existing_pr_repair_prompt

REPOSITORY = "owner/repo"
PR_NUMBER = 42
TASK_ID = "task_e_123"
TURN_ID = TASK_ID + "~assttrn_1"
LOGICAL_IDENTITY = "retained-review-feedback"


def commit_history(count: int, *, revert: int = 0) -> PRCommitHistory:
    commits = tuple(PRCommit(f"{index:040x}", 'Revert "failed correction"' if index == revert else f"Implement change {index}") for index in range(1, count + 1))
    return PRCommitHistory(commits[-1].sha, count, commits)


@dataclass
class HistoryReader:
    history: PRCommitHistory
    reads: int = 0

    def get_pr_automation_history_strict(self, repository: str, pr_number: int) -> PRCommitHistory:
        assert (repository, pr_number) == (REPOSITORY, PR_NUMBER)
        self.reads += 1
        return self.history


@dataclass
class NativeSendHarness:
    client: CodexCloudClient
    github: HistoryReader
    guard: PRGuardStore
    wham: Mock
    target: ExistingPrRepairTarget
    prompt: str
    journal: Path

    def allowed(self) -> bool:
        return assess_pr_automation(self.github, REPOSITORY, PR_NUMBER, store=self.guard).allowed

    def send_from_processor(self) -> bool:
        token = pr_processor._controller_feedback_client.set(self.github)
        try:
            return pr_processor._send_followup_with_quota_admission_impl(self.client, REPOSITORY, TASK_ID, self.prompt, (LOGICAL_IDENTITY,), repair_target=self.target)
        finally:
            pr_processor._controller_feedback_client.reset(token)


def outside_owner_scope(*_args, **_kwargs):
    raise CodexWorkOutsideScope("Legacy task has no ordinary issue owner")


@pytest.fixture
def native_send(tmp_path, monkeypatch) -> NativeSendHarness:
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(tmp_path / "runtime"))
    config = LLMBackendConfiguration()
    config.backends["codex-cloud"] = BackendConfig(name="codex-cloud", backend_type="codex-cloud", model="")
    monkeypatch.setattr("auto_coder.codex_cloud_client.get_llm_config", lambda **_kwargs: config)
    monkeypatch.setattr("auto_coder.codex_work_fence.production_codex_fence", outside_owner_scope)
    client = CodexCloudClient(repo_name=REPOSITORY)
    wham = Mock()
    wham.resolve_latest_assistant_turn.return_value = TURN_ID
    wham.send_follow_up.return_value = FollowUpDeliveryResult(FollowUpDeliveryOutcome.DELIVERED, 200)
    client.wham_client = wham
    github = HistoryReader(commit_history(1))
    target = ExistingPrRepairTarget(REPOSITORY, PR_NUMBER, "feature", "main", github.history.head_sha)
    return NativeSendHarness(client, github, PRGuardStore(), wham, target, build_existing_pr_repair_prompt(target, "Repair the current review finding"), _codex_followup_state_path(REPOSITORY))


def test_healthy_legacy_followup_keeps_existing_outside_owner_route(native_send) -> None:
    assert native_send.send_from_processor()
    native_send.wham.send_follow_up.assert_called_once()
    records = _load_pending_followups(native_send.journal)
    assert len(records) == 1
    assert next(iter(records.values())).status == FollowUpDeliveryOutcome.DELIVERED.value
    assert native_send.guard.status(REPOSITORY, PR_NUMBER).allowed
    assert native_send.github.reads >= 2


@pytest.mark.parametrize("stop_during", ["latest_turn", "fence_lookup"])
def test_new_revert_during_native_preparation_stops_before_journal_and_wire(native_send, monkeypatch, stop_during) -> None:
    def latch_revert() -> None:
        native_send.github.history = commit_history(2, revert=2)
        assert assess_pr_automation(native_send.github, REPOSITORY, PR_NUMBER, store=native_send.guard).stopped

    if stop_during == "latest_turn":

        def latest_turn(_task_id):
            latch_revert()
            return TURN_ID

        native_send.wham.resolve_latest_assistant_turn.side_effect = latest_turn
    else:

        def fence_lookup(*_args, **_kwargs):
            latch_revert()
            return outside_owner_scope()

        monkeypatch.setattr("auto_coder.codex_work_fence.production_codex_fence", fence_lookup)

    assert not native_send.send_from_processor()
    native_send.wham.send_follow_up.assert_not_called()
    assert not native_send.journal.exists()
    stopped = PRGuardStore(native_send.guard.path).status(REPOSITORY, PR_NUMBER)
    assert stopped.stopped and stopped.revert_sha == f"{2:040x}"


@pytest.mark.parametrize("force", [False, True])
def test_continue_if_paused_propagates_stop_check_to_actual_send(native_send, force) -> None:
    assert native_send.allowed()

    def latest_turn(_task_id):
        native_send.github.history = commit_history(30)
        return TURN_ID

    native_send.wham.resolve_latest_assistant_turn.side_effect = latest_turn
    assert not native_send.client.continue_if_paused(TASK_ID, prompt=native_send.prompt, repair_target=native_send.target, force=force, new_effect_authorizer=native_send.allowed)
    native_send.wham.send_follow_up.assert_not_called()
    assert not native_send.journal.exists()
    assert native_send.guard.status(REPOSITORY, PR_NUMBER).stopped
    assert TASK_ID not in native_send.client.last_continued_at


def test_stopped_pr_reconciles_already_sent_followup_after_restart_without_new_wire(native_send) -> None:
    native_send.wham.send_follow_up.return_value = FollowUpDeliveryResult(FollowUpDeliveryOutcome.INDETERMINATE, 502)
    assert not native_send.client.send_existing_pr_followup(TASK_ID, native_send.prompt, native_send.target, (LOGICAL_IDENTITY,), new_effect_authorizer=native_send.allowed)
    native_send.wham.send_follow_up.assert_called_once()
    assert next(iter(_load_pending_followups(native_send.journal).values())).status == FollowUpDeliveryOutcome.INDETERMINATE.value

    native_send.github.history = commit_history(30)
    assert not native_send.allowed()
    stopped = native_send.guard.status(REPOSITORY, PR_NUMBER)
    native_send.wham.reconcile_follow_up.return_value = True
    restarted = CodexCloudClient(repo_name=REPOSITORY)
    restarted.wham_client = native_send.wham
    assert restarted.send_existing_pr_followup(TASK_ID, native_send.prompt, native_send.target, (LOGICAL_IDENTITY,), new_effect_authorizer=native_send.allowed)
    native_send.wham.send_follow_up.assert_called_once()
    native_send.wham.reconcile_follow_up.assert_called_once()
    assert next(iter(_load_pending_followups(native_send.journal).values())).status == FollowUpDeliveryOutcome.DELIVERED.value
    assert PRGuardStore(native_send.guard.path).status(REPOSITORY, PR_NUMBER) == stopped


def test_late_stop_during_forced_retry_preserves_prior_ambiguous_receipt(native_send) -> None:
    native_send.wham.send_follow_up.return_value = FollowUpDeliveryResult(FollowUpDeliveryOutcome.INDETERMINATE, 502)
    assert not native_send.client.send_existing_pr_followup(TASK_ID, native_send.prompt, native_send.target, (LOGICAL_IDENTITY,), new_effect_authorizer=native_send.allowed)
    original_receipt = native_send.journal.read_bytes()
    native_send.wham.reconcile_follow_up.return_value = None
    authorizations = 0

    def stop_at_final_send() -> bool:
        nonlocal authorizations
        authorizations += 1
        if authorizations == 2:
            native_send.github.history = commit_history(2, revert=2)
        return native_send.allowed()

    assert not native_send.client.send_followup(TASK_ID, native_send.prompt, (LOGICAL_IDENTITY,), force=True, repair_target=native_send.target, new_effect_authorizer=stop_at_final_send)
    assert authorizations == 2
    native_send.wham.send_follow_up.assert_called_once()
    assert native_send.guard.status(REPOSITORY, PR_NUMBER).stopped
    # Refusing a new attempt does not prove the old ambiguous POST was rejected.
    assert native_send.journal.read_bytes() == original_receipt
