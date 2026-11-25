"""Durable proposal retention across PR-validation acceptance interruptions."""

import json
import stat
from dataclasses import replace

import pytest

from auto_coder.accepted_finding_bridge import AcceptedFindingProjection
from auto_coder.adversarial_validator import AdversarialValidationFinding, AdversarialValidationResult, RequirementCoverageEntry
from auto_coder.deferred_pr_validation import DeferredPRValidation, DeferredPRValidationStateError, DeferredPRValidationStore
from auto_coder.ordinary_correction_references import CanonicalCorrectionContext
from auto_coder.pr_review_execution import FindingDisposition, ReviewExecutionResult, ReviewMode, ScopeAssessment
from auto_coder.reviewer_session_registry import RecoveredFileEvidence, ReviewerSession, ReviewerSessionRegistry, TestOracleGap


def result_for(attempt_id="attempt-one", sequence=1):
    result = AdversarialValidationResult(
        result="NEEDS_FIX",
        summary="A current requirement still fails.",
        attempt_id=attempt_id,
        attempt_sequence=sequence,
        findings=[AdversarialValidationFinding(violated_requirement="REQ-001", counterexample="A stale source is accepted.")],
        requirement_coverage=[RequirementCoverageEntry(requirement_id="REQ-001", status="FAIL")],
        raw_response='{"result":"NEEDS_FIX"}',
        source_response_id="response-one",
    )
    result.reviewer_session_checkpoint = ReviewerSession(
        repository="owner/repo",
        pr_number=6009,
        backend_name="codex",
        backend_type="codex",
        model_name="reviewer",
        session_id="session-one",
        last_head_sha="head-one",
        evidence_head_sha="head-one",
        evidence_validation_snapshot="validation-one",
        test_oracle_gaps=[TestOracleGap(gap_id="gap-one", requirement_id="REQ-001", status="OPEN")],
        recovered_file_evidence=[RecoveredFileEvidence(path="src/runtime.py", evidence="Complete patch.", identity_version=1)],
    )
    result.closure_assessment = ReviewExecutionResult(
        mode=ReviewMode.ORDINARY_CLOSURE,
        round_id="round-one",
        attempt_id=attempt_id,
        head_sha="head-one",
        base_sha="base-one",
        contract_identity="contract-one",
        policy_identity="strong-policy-one",
        finding_set_revision=4,
        reviewer_provenance="codex/reviewer",
        verdict="PASS",
        dispositions=(FindingDisposition("finding-one", "FIXED", "The reviewed regression verifies closure."),),
        scope=ScopeAssessment.BOUNDED,
        repository="owner/repo",
        pr_number=6009,
        attempt_sequence=sequence,
    )
    result.correction_reference_context = CanonicalCorrectionContext(repository="owner/repo", pr_number=6009, head_sha="head-one", base_sha="base-one")
    return result


def proposal(result=None, **overrides):
    result = result or result_for()
    metadata = {
        "repository": "owner/repo",
        "pr_number": 6009,
        "head_sha": "head-one",
        "base_sha": "base-one",
        "attempt_id": result.attempt_id,
        "attempt_sequence": result.attempt_sequence,
        "review_id": "review-one",
        "policy_identity": "policy-one",
        "thread_snapshot": "discussion-one",
        "validation_snapshot": "validation-one",
        "context_identity": "context-one",
        "input_snapshot": "input-one",
    }
    metadata.update(overrides)
    return DeferredPRValidation.from_result(result=result, **metadata)


def test_restart_restores_semantic_result_checkpoint_and_original_attempt(tmp_path):
    original = result_for()
    original.reviewer_session_registry = ReviewerSessionRegistry(tmp_path / "sessions.json")
    original.accepted_finding_projection = AcceptedFindingProjection(complete=True)
    path = tmp_path / "pending.json"
    record = proposal(original)
    DeferredPRValidationStore("owner/repo", path).retain(record)

    retained = DeferredPRValidationStore("owner/repo", path).load(6009)
    assert retained == record
    restored = retained.restore_result()
    assert (restored.attempt_id, restored.attempt_sequence) == ("attempt-one", 1)
    assert restored.result == original.result
    assert restored.findings == original.findings
    assert restored.requirement_coverage == original.requirement_coverage
    assert restored.closure_assessment == original.closure_assessment
    assert restored.correction_reference_context == original.correction_reference_context
    assert restored.reviewer_session_checkpoint == original.reviewer_session_checkpoint
    assert restored.source_response_id == "response-one"
    assert restored.raw_response == original.raw_response
    assert restored.reviewer_session_registry is None
    assert restored.accepted_finding_projection is None
    assert not (tmp_path / "sessions.json").exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(DeferredPRValidationStore("owner/repo", path).lock_path.stat().st_mode) == 0o600


def test_retry_updates_never_replace_semantics_or_shorten_wait(tmp_path):
    store = DeferredPRValidationStore("owner/repo", tmp_path / "pending.json")
    record = proposal()
    store.retain(record)
    deferred = store.defer(record.attempt_id, "rate_limit_cooldown", 300.0)
    repeated = store.defer(record.attempt_id, "request_in_flight", 200.0)

    assert repeated.retry_not_before == 300.0
    assert repeated.reason == "request_in_flight"
    assert repeated.semantic_identity() == record.semantic_identity()
    assert deferred.result_payload == record.result_payload
    assert deferred.reviewer_session_payload == record.reviewer_session_payload
    assert store.retain(record) == repeated


def test_conflicting_same_attempt_cannot_replace_first_result(tmp_path):
    store = DeferredPRValidationStore("owner/repo", tmp_path / "pending.json")
    record = proposal()
    store.retain(record)
    conflicting = proposal(replace(result_for(), result="PASS", findings=[]))

    with pytest.raises(DeferredPRValidationStateError, match="Conflicting"):
        store.retain(conflicting)

    assert store.load(6009) == record


def test_exact_attempt_removal_cannot_erase_a_newer_proposal(tmp_path):
    path = tmp_path / "pending.json"
    first_store = DeferredPRValidationStore("owner/repo", path)
    second_store = DeferredPRValidationStore("owner/repo", path)
    older = proposal()
    newer = proposal(result_for("attempt-two", 2), review_id="review-two")
    first_store.retain(older)
    second_store.retain(newer)

    assert first_store.load(6009) == newer
    assert first_store.remove(older.attempt_id)
    assert not first_store.remove(older.attempt_id)
    assert second_store.load(6009) == newer
    assert second_store.remove(newer.attempt_id)
    assert first_store.load(6009) is None


@pytest.mark.parametrize("corrupt_text", ["{", "[]", '{"version":1,"repository":"owner/repo","records":[]}'])
def test_corrupt_store_is_unavailable_instead_of_appearing_empty(tmp_path, corrupt_text):
    path = tmp_path / "pending.json"
    path.write_text(corrupt_text, encoding="utf-8")

    with pytest.raises(DeferredPRValidationStateError):
        DeferredPRValidationStore("owner/repo", path).load(6009)


def test_foreign_store_cannot_be_consumed_or_replaced(tmp_path):
    path = tmp_path / "pending.json"
    owner = DeferredPRValidationStore("owner/repo", path)
    owner.retain(proposal())
    outsider = DeferredPRValidationStore("other/repo", path)

    with pytest.raises(DeferredPRValidationStateError, match="ownership"):
        outsider.load(6009)
    with pytest.raises(DeferredPRValidationStateError, match="another repository"):
        outsider.retain(proposal())
    assert owner.load(6009) == proposal()


def test_mismatched_attempt_or_checkpoint_cannot_be_retained():
    with pytest.raises(DeferredPRValidationStateError, match="different producing attempt"):
        proposal(attempt_id="foreign-attempt")
    with pytest.raises(DeferredPRValidationStateError, match="different target"):
        proposal(head_sha="different-head")
    with pytest.raises(DeferredPRValidationStateError, match="different validation snapshot"):
        proposal(validation_snapshot="different-snapshot")


def test_persisted_foreign_attempt_payload_is_not_silently_reused(tmp_path):
    path = tmp_path / "pending.json"
    store = DeferredPRValidationStore("owner/repo", path)
    record = proposal()
    store.retain(record)
    state = json.loads(path.read_text(encoding="utf-8"))
    payload = json.loads(state["records"][record.attempt_id]["result_payload"])
    payload["attempt_id"] = "foreign-attempt"
    state["records"][record.attempt_id]["result_payload"] = json.dumps(payload)
    path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(DeferredPRValidationStateError, match="different producing attempt"):
        store.load(6009)


def test_no_checkpoint_result_is_retained_without_inventing_one(tmp_path):
    result = result_for()
    result.reviewer_session_checkpoint = None
    record = proposal(result)
    store = DeferredPRValidationStore("owner/repo", tmp_path / "pending.json")
    store.retain(record)

    assert store.load(6009).restore_result().reviewer_session_checkpoint is None


def test_atomic_replace_failure_preserves_last_durable_proposal(tmp_path, monkeypatch):
    path = tmp_path / "pending.json"
    store = DeferredPRValidationStore("owner/repo", path)
    record = proposal()
    store.retain(record)

    def fail_replace(source, destination):
        raise OSError("injected replacement failure")

    monkeypatch.setattr("auto_coder.deferred_pr_validation.os.replace", fail_replace)
    with pytest.raises(DeferredPRValidationStateError, match="replacement failure"):
        store.defer(record.attempt_id, "rate_limit_cooldown", 500.0)

    assert store.load(6009) == record
    assert not list(tmp_path.glob("pending.tmp.*"))


def test_unknown_attempt_cannot_manufacture_a_deferred_result(tmp_path):
    store = DeferredPRValidationStore("owner/repo", tmp_path / "pending.json")

    with pytest.raises(DeferredPRValidationStateError, match="unknown"):
        store.defer("never-produced", "rate_limit_cooldown", 500.0)

    assert store.load(6009) is None


def review_policy():
    from auto_coder.automation_config import AutomationConfig
    from auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration

    config = AutomationConfig()
    reviewer = BackendConfig(name="reviewer", backend_type="codex", model="review-model-v1", options=["--reasoning", "high"], options_for_noedit=["--sandbox", "read-only"])
    fallback = BackendConfig(name="fallback", backend_type="claude", model="fallback-model-v1")
    configured = LLMBackendConfiguration(backends={"reviewer": reviewer, "fallback": fallback}, backend_pr_adversarial_validation_order=["reviewer", "fallback"])
    return config, configured


@pytest.mark.parametrize("changed", ["model", "noedit_options", "resume_options", "backend_type", "provider", "enabled", "route", "admission"])
def test_pending_policy_changes_with_effective_reviewer_configuration(changed):
    from auto_coder.deferred_pr_validation import deferred_validation_policy_identity

    config, configured = review_policy()
    before = deferred_validation_policy_identity(config, True, llm_config=configured)
    reviewer = configured.backends["reviewer"]
    if changed == "model":
        reviewer.model = "review-model-v2"
    elif changed == "noedit_options":
        reviewer.options_for_noedit.append("--another-review-option")
    elif changed == "resume_options":
        reviewer.options_for_resume.append("--resume-review-policy")
    elif changed == "backend_type":
        reviewer.backend_type = "claude"
    elif changed == "provider":
        reviewer.model_provider = "different-provider"
    elif changed == "enabled":
        reviewer.enabled = False
    elif changed == "route":
        configured.backend_pr_adversarial_validation_order.reverse()
    else:
        config.MAX_ADVERSARIAL_VALIDATIONS = 2

    assert deferred_validation_policy_identity(config, True, llm_config=configured) != before


def test_pending_policy_ignores_rotating_credentials_and_runtime_capacity():
    from auto_coder.deferred_pr_validation import deferred_validation_policy_identity

    config, configured = review_policy()
    before = deferred_validation_policy_identity(config, True, llm_config=configured)
    reviewer = configured.backends["reviewer"]
    reviewer.api_key = "rotated-test-key"
    reviewer.claude_code_oauth_token = "rotated-test-token"
    reviewer.usage_markers.append("latest-usage-observation")
    configured.quota_observation_cache = {"remaining": 0.01}
    configured.backend_issue_adversarial_validation_order = ["unrelated-issue-reviewer"]

    assert deferred_validation_policy_identity(config, True, llm_config=configured) == before
    assert deferred_validation_policy_identity(config, False, llm_config=configured) != before


def test_pending_policy_obeys_production_dedicated_route_precedence():
    from auto_coder.deferred_pr_validation import deferred_validation_policy_identity
    from auto_coder.llm_backend_config import BackendConfig

    config, configured = review_policy()
    configured.backends["legacy"] = BackendConfig(name="legacy", backend_type="codex", model="legacy-model")
    configured.backend_adversarial_validation_order = ["legacy"]
    dedicated = deferred_validation_policy_identity(config, True, llm_config=configured)
    configured.backends["legacy"].model = "another-legacy-model"
    assert deferred_validation_policy_identity(config, True, llm_config=configured) == dedicated

    configured.backend_pr_adversarial_validation_order = []
    assert deferred_validation_policy_identity(config, True, llm_config=configured) != dedicated


def test_pending_policy_reloads_the_requested_repository_and_fails_closed(monkeypatch):
    from auto_coder.deferred_pr_validation import deferred_validation_policy_identity

    config, configured = review_policy()
    requested = []

    def load(*, repo_name):
        requested.append(repo_name)
        return configured

    monkeypatch.setattr("auto_coder.llm_backend_config.get_llm_config", load)
    assert deferred_validation_policy_identity(config, True, repo_name="owner/repo") == deferred_validation_policy_identity(config, True, llm_config=configured)
    assert requested == ["owner/repo"]

    def unavailable(*, repo_name):
        raise ValueError("credential-bearing configuration detail must not be exposed")

    monkeypatch.setattr("auto_coder.llm_backend_config.get_llm_config", unavailable)
    with pytest.raises(DeferredPRValidationStateError, match="policy is unavailable") as caught:
        deferred_validation_policy_identity(config, True, repo_name="owner/repo")
    assert "credential-bearing" not in str(caught.value)
