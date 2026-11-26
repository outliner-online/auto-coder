"""Dynamic review transport must respect actual local workspace ownership."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.adversarial_validator import AdversarialValidationContext, DynamicCheckExecution, IssueRequirement, _run_dynamic_reviewer_followup, run_adversarial_validation
from auto_coder.automation_config import AutomationConfig
from auto_coder.backend_manager import BackendManager
from auto_coder.editable_review import ReviewChangesProduced, bind_editable_review, capture_review_workspace
from auto_coder.exceptions import SessionWorkspaceCompatibilityError
from auto_coder.invocation_admission import bind_pr_review_designation
from auto_coder.local_llm_observability import observe_local_llm_call
from auto_coder.review_capture.validation_evidence import ManifestInput
from auto_coder.reviewer_session_registry import ReviewerSessionRegistry
from auto_coder.utils import CommandResult
from tests.test_editable_review import repository  # noqa: F401

HEAD = "b" * 40
REQUIREMENT = "A completed focused check must determine current-head correctness."


def _response(*, target: str | None = None, thread_status: str | None = None) -> str:
    response = {
        "result": "PASS",
        "summary": "Complete the focused check" if target else "Focused execution confirms the Requirement",
        "requirement_coverage": [{"requirement_id": "REQ-001", "status": "VERIFIED", "evidence": "Inspected source and the exact focused test"}],
        "findings": [],
        "dynamic_check_requested": target,
    }
    if thread_status:
        response["thread_dispositions"] = [{"thread_id": "thread-1", "status": thread_status, "rationale": "Current focused evidence", "evidence": "Exact focused output"}]
    return json.dumps(response)


def _context() -> AdversarialValidationContext:
    return AdversarialValidationContext(
        repo_name="owner/repo",
        pr_number=2944,
        pr_title="Retained review transport",
        pr_body="Closes #2894",
        pr_diff="diff --git a/source.py b/source.py\n+current_head_change",
        changed_tests=["tests/test_feature.py"],
        all_changed_files=["source.py", "tests/test_feature.py"],
        issue_context="Issue #2894: " + REQUIREMENT,
        issue_requirements=[IssueRequirement(requirement_id="REQ-001", text=REQUIREMENT)],
        validation_snapshot="snapshot-current",
    )


def _manager() -> MagicMock:
    manager = MagicMock()
    manager.get_current_backend_identity.return_value = ("codex-review", "codex", "strong")
    manager.has_retained_local_session.return_value = False
    manager._last_session_id = "initial-session"
    return manager


class FreshOnlyLocalReviewer:
    """A real-manager adapter with Codex's unsupported retained-resume contract."""

    supports_retained_local_continuation = False
    model_name = "strong"
    config_backend = SimpleNamespace(backend_type="codex")

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.session_id: str | None = None

    def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
        self.prompts.append(prompt)
        self.session_id = f"review-{len(self.prompts)}"
        return _response(target="tests/test_feature.py") if len(self.prompts) == 1 else _response()

    def continue_session(self, **_kwargs) -> str:
        pytest.fail("A fresh-only adapter must never receive explicit resume")

    def get_last_session_id(self) -> str | None:
        return self.session_id

    def clear_last_session_id(self) -> None:
        self.session_id = None

    def close(self) -> None:
        pass


@pytest.mark.usefixtures("_use_real_commands")
def test_released_local_dynamic_followup_reaches_final_review_with_real_manager(tmp_path: Path, monkeypatch) -> None:
    """Exercise the rejecting BackendManager and then the actual fresh route."""
    monkeypatch.chdir(tmp_path)
    client = FreshOnlyLocalReviewer()
    with patch("pathlib.Path.home", return_value=tmp_path):
        manager = BackendManager(default_backend="codex-review", default_client=client, factories={"codex-review": lambda: client}, order=["codex-review"], automatic_session_resume=False)
    # This is the manager's state after the unsupported adapter releases its root.
    manager._released_local_workspace_sessions.add("review-1")
    with pytest.raises(SessionWorkspaceCompatibilityError, match="original private workspace"):
        manager.continue_session("review-1", "Do not resume a released workspace")

    registry = ReviewerSessionRegistry(tmp_path / "reviewer-sessions.json")
    with (
        patch("auto_coder.adversarial_validator.build_adversarial_validation_context", return_value=_context()),
        patch("auto_coder.adversarial_validator.run_exact_head_dynamic_check", return_value=DynamicCheckExecution(success=True, output="1 passed", executed_sha=HEAD)),
        patch("auto_coder.backend_manager.observe_local_llm_call", wraps=observe_local_llm_call) as observed_calls,
    ):
        result = run_adversarial_validation("owner/repo", {"number": 2944, "head_sha": HEAD}, AutomationConfig(), backend_manager=manager, session_registry=registry)

    assert result.result == "PASS"
    assert len(client.prompts) == 2
    assert client.prompts[0] in client.prompts[1]
    assert _response(target="tests/test_feature.py") in client.prompts[1]
    assert "1 passed" in client.prompts[1]
    assert manager._last_continue_session_resumed is False
    assert [call.kwargs["continuation"] for call in observed_calls.call_args_list] == [False, False]
    assert [call.kwargs["backend_type"] for call in observed_calls.call_args_list] == ["codex", "codex"]
    assert result.reviewer_session_checkpoint.session_id == "review-2"
    assert registry.get("owner/repo", 2944, "codex-review", "codex", "strong").session_id == "review-2"
    manager.close()


@pytest.mark.parametrize("route,final_disposition", [("direct", "STILL_VALID"), ("direct", None), ("invalid_target", "STILL_VALID"), ("unselectable_target", "STILL_VALID")])
def test_dynamic_review_selects_fresh_followups_with_complete_evidence(tmp_path: Path, route: str, final_disposition: str | None) -> None:
    manager = _manager()
    target = "run arbitrary tests" if route == "invalid_target" else "tests/test_feature.py"
    initial = _response(target=target, thread_status="ADDRESSED")
    correction_data = json.loads(_response(target="tests/test_feature.py", thread_status="ADDRESSED"))
    correction_data["result"] = "INCONCLUSIVE"
    correction_data["requirement_coverage"][0]["status"] = "UNVERIFIED"
    correction_data["decision_critical_evidence_gaps"] = [{"requirement_id": "REQ-001", "evidence_needed": "Execute the corrected focused target", "recovery_attempts": ["Inspected the committed test and corrected its target; focused execution is still required"]}]
    correction = json.dumps(correction_data)
    final = _response(thread_status=final_disposition)
    responses = [initial, final] if route == "direct" else [initial, correction, final]
    prompts: list[str] = []

    def invoke(prompt: str, **_kwargs) -> str:
        prompts.append(prompt)
        manager._last_session_id = f"session-{len(prompts)}"
        return responses[len(prompts) - 1]

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_feature.py").write_text("def test_feature():\n    assert True\n")
    check_result = DynamicCheckExecution(success=True, output="focused-current-head-result", executed_sha=HEAD)
    results = [DynamicCheckExecution(target_selection_error="pytest selected zero tests"), check_result] if route == "unselectable_target" else [check_result]
    registry = ReviewerSessionRegistry(tmp_path / "reviewer-sessions.json")
    with (
        patch("auto_coder.adversarial_validator.build_adversarial_validation_context", return_value=_context()),
        patch("auto_coder.adversarial_validator.run_llm_prompt", side_effect=invoke),
        patch("auto_coder.adversarial_validator.run_exact_head_dynamic_check", side_effect=results),
        patch("auto_coder.adversarial_validator.CommandExecutor.run_command", return_value=CommandResult(True, HEAD, "", 0)),
        patch("auto_coder.adversarial_validator.validation_evidence.observe_prompt") as observe_prompt,
    ):
        result = run_adversarial_validation("owner/repo", {"number": 2944, "head_sha": HEAD}, AutomationConfig(), backend_manager=manager, session_registry=registry, execution_cwd=str(tmp_path))

    assert result.result == "PASS"
    if final_disposition is None:
        assert result.thread_dispositions == []
    else:
        assert result.thread_dispositions[0].status == final_disposition
    assert len(prompts) == len(responses)
    manager.continue_session.assert_not_called()
    manager.authorize_retained_local_session_reuse.assert_not_called()
    for index, submitted in enumerate(prompts[1:], start=1):
        assert prompts[0] in submitted
        assert responses[index - 1] in submitted
        assert REQUIREMENT in submitted
        assert "current_head_change" in submitted
        assert "does not renew any review" in submitted
        observations = [call for call in observe_prompt.call_args_list if call.args[1] == submitted]
        assert len(observations) == 1
        assert observations[0].args[2].entries == [("REQ-001", REQUIREMENT)]
    assert "focused-current-head-result" in prompts[-1]
    assert result.reviewer_session_checkpoint.session_id == f"session-{len(prompts)}"


@pytest.mark.parametrize("backend_type,retained,refusal_stage", [("opencode", True, "authorize"), ("opencode", True, "resume"), ("codex-cloud", False, "resume")])
def test_dynamic_followup_resume_refusal_never_starts_fresh(backend_type: str, retained: bool, refusal_stage: str) -> None:
    manager = _manager()
    manager.get_current_backend_identity.return_value = ("review", backend_type, "strong")
    manager.has_retained_local_session.return_value = retained
    refusal = manager.authorize_retained_local_session_reuse if refusal_stage == "authorize" else manager.continue_session
    refusal.side_effect = SessionWorkspaceCompatibilityError("generation changed")
    with patch("auto_coder.adversarial_validator.run_llm_prompt") as fresh:
        with pytest.raises(SessionWorkspaceCompatibilityError, match="generation changed"):
            _run_dynamic_reviewer_followup(manager, "session", "focused", original_prompt="full review", prior_response="prior", phase="dynamic_check_followup", manifest=ManifestInput())
    assert manager.continue_session.call_count == int(refusal_stage == "resume")
    fresh.assert_not_called()
    assert manager.authorize_retained_local_session_reuse.call_count == int(retained)


@pytest.mark.usefixtures("_use_real_commands")
def test_fresh_dynamic_followup_edits_cannot_approve_the_changed_source(repository, tmp_path: Path) -> None:
    root, _remote, head = repository
    manager = _manager()
    initial = _response(target="tests/test_feature.py")
    submitted = []

    def invoke(prompt: str, **_kwargs) -> str:
        submitted.append(prompt)
        manager._last_session_id = f"session-{len(submitted)}"
        if len(submitted) == 1:
            return initial
        (root / "source.py").write_text("value = 2\n")
        return _response()

    registry = ReviewerSessionRegistry(tmp_path / "sessions.json")
    snapshot = capture_review_workspace(str(root), head)
    with (
        bind_editable_review(snapshot),
        bind_pr_review_designation(),
        patch("auto_coder.adversarial_validator.build_adversarial_validation_context", return_value=_context()),
        patch("auto_coder.adversarial_validator.run_llm_prompt", side_effect=invoke),
        patch("auto_coder.adversarial_validator.run_exact_head_dynamic_check", return_value=DynamicCheckExecution(success=True, output="1 passed", executed_sha=head)),
    ):
        with pytest.raises(ReviewChangesProduced) as changed:
            run_adversarial_validation("owner/repo", {"number": 2944, "head_sha": head}, AutomationConfig(), backend_manager=manager, session_registry=registry)

    assert len(submitted) == 2
    assert changed.value.correction_basis is None
    assert not registry.path.exists()
    manager.continue_session.assert_not_called()
    manager.release_local_session.assert_any_call("session-2")
