"""Ordinary review edits require current, explicit pre-edit blocker evidence."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from auto_coder import adversarial_validator as validator
from auto_coder.accepted_finding_bridge import CATEGORY_REGRESSION_GAP, AcceptedFindingProjection, AcceptedFindingRecord, known_gap_from_record
from auto_coder.automation_config import AutomationConfig
from auto_coder.editable_review import ReviewChangesProduced, bind_editable_review, capture_review_workspace
from auto_coder.invocation_admission import bind_pr_review_designation
from auto_coder.review_correction_policy import validate_review_correction_basis_json
from auto_coder.reviewer_session_registry import ReviewerSession

pytestmark = pytest.mark.usefixtures("_use_custom_subprocess_mock")

REQUIREMENT_ID = "#42/REQ-001"
REQUIREMENT_TEXT = "The production entry point must reject an empty request."


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def review_environment(tmp_path, monkeypatch):
    root = tmp_path / "review"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.name", "Review Test")
    _git(root, "config", "user.email", "review@example.com")
    (root / "source.py").write_text("def accept_request(value):\n    return True\n")
    (root / "tests").mkdir()
    (root / "tests/test_source.py").write_text("def test_request():\n    assert True\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Admitted source")
    _git(root, "remote", "add", "origin", str(tmp_path / "unused-remote.git"))
    head = _git(root, "rev-parse", "HEAD")
    context = validator.AdversarialValidationContext(
        repo_name="owner/repo",
        pr_number=100,
        pr_title="Request validation",
        pr_diff="diff --git a/source.py b/source.py\n+++ b/source.py\n+return True",
        all_changed_files=["source.py", "tests/test_source.py"],
        changed_tests=["tests/test_source.py"],
        issue_context="## Requirements\nREQ-001: " + REQUIREMENT_TEXT,
        issue_requirements=[validator.IssueRequirement(REQUIREMENT_ID, REQUIREMENT_TEXT)],
        requirement_manifest_mode="explicit-contract",
        validation_snapshot="reviewed-input-snapshot",
    )
    monkeypatch.setattr(validator, "build_adversarial_validation_context", lambda *_args, **_kwargs: context)
    manager = MagicMock()
    manager.get_current_backend_identity.return_value = ("reviewer", "codex", "model")
    manager.has_retained_local_session.return_value = False
    manager._last_session_id = "review-session"
    manager._last_continue_session_resumed = True
    registry = MagicMock()
    registry.get.return_value = None
    bridge = MagicMock()
    bridge.project.side_effect = lambda target, *_args, **_kwargs: AcceptedFindingProjection(target=target, complete=True)
    bridge.ledger.get_snapshot.side_effect = RuntimeError("No canonical correction reference in this fixture")
    return SimpleNamespace(root=root, head=head, context=context, manager=manager, registry=registry, bridge=bridge, threads=(), config=AutomationConfig())


def _finding(**overrides):
    item = {
        "requirement_id": REQUIREMENT_ID,
        "finding_identity": "empty-request-accepted",
        "correction_identity": "reject-empty-request",
        "violated_requirement": "Reviewer paraphrase is not authoritative",
        "reachability": "The public accept_request entry point accepts an empty string.",
        "required_behavior": "Reject the empty request.",
        "actual_behavior": "The empty request is accepted.",
        "evidence": "At the admitted HEAD, calling accept_request('') returns True.",
        "evidence_classification": "DEMONSTRATED",
        "counterexample": "accept_request('') returns True instead of False at the admitted HEAD.",
        "test_gap": "The existing test asserts only a nonempty request.",
        "suggested_regression_scenario": "Call accept_request('') and assert rejection.",
        "anchor_path": "source.py",
    }
    item.update(overrides)
    return item


def _gap():
    return {
        "requirement_id": REQUIREMENT_ID,
        "authoritative_boundary": "source.py:accept_request",
        "invariant": "An empty request is rejected.",
        "plausible_incorrect_implementation": "Remove the empty-request guard from accept_request.",
        "why_tests_still_pass": "The current tests only call accept_request with a nonempty request.",
        "material_consequence": "An empty request reaches the production queue.",
        "focused_regression_scenario": "Call the production entry point with an empty request and assert no queue write.",
        "anchor_path": "tests/test_source.py",
    }


def _response(verdict, *, findings=(), gaps=(), **extra):
    return json.dumps(
        {
            "result": verdict,
            "summary": "The decision describes the admitted pre-edit HEAD.",
            "findings": list(findings),
            "test_oracle_gaps": list(gaps),
            "requirement_coverage": [{"requirement_id": REQUIREMENT_ID, "status": "VIOLATED" if findings else "VERIFIED", "evidence": "Inspected the admitted production entry point."}],
            **extra,
        }
    )


def _run(environment):
    snapshot = capture_review_workspace(str(environment.root), environment.head)
    with bind_editable_review(snapshot), bind_pr_review_designation():
        return validator.run_adversarial_validation(
            "owner/repo",
            {"number": 100, "head": {"sha": environment.head}},
            environment.config,
            backend_manager=environment.manager,
            session_registry=environment.registry,
            accepted_finding_bridge=environment.bridge,
            claimed_review_threads=environment.threads,
            execution_cwd=str(environment.root),
            defer_session_persistence=True,
        )


def _edited_response(environment, monkeypatch, response, *, continuation=False):
    def edit_and_respond(*_args, **_kwargs):
        (environment.root / "source.py").write_text("def accept_request(value):\n    return bool(value)\n")
        return response

    if continuation:
        initial = _response("INCONCLUSIVE", dynamic_check_requested="tests/test_source.py")
        prompt = Mock(return_value=initial)
        environment.manager.continue_session.side_effect = edit_and_respond
        monkeypatch.setattr(validator, "run_exact_head_dynamic_check", Mock(return_value=validator.DynamicCheckExecution(success=True, executed_sha=environment.head)))
    else:
        prompt = Mock(side_effect=edit_and_respond)
    monkeypatch.setattr(validator, "run_llm_prompt", prompt)
    with pytest.raises(ReviewChangesProduced) as captured:
        _run(environment)
    assert _git(environment.root, "rev-parse", "HEAD") == environment.head
    environment.manager.release_local_session.assert_any_call("review-session")
    prompt.assert_called_once()
    if continuation:
        environment.manager.continue_session.assert_called_once()
    else:
        environment.manager.continue_session.assert_not_called()
    return captured.value


def test_clean_pass_keeps_the_admitted_source(review_environment, monkeypatch):
    monkeypatch.setattr(validator, "run_llm_prompt", Mock(return_value=_response("PASS")))
    result = _run(review_environment)
    assert result.is_pass
    assert _git(review_environment.root, "status", "--porcelain") == ""
    review_environment.manager.release_local_session.assert_not_called()


@pytest.mark.parametrize("continuation", [False, True])
@pytest.mark.parametrize("verdict", ["PASS", "INCONCLUSIVE", "BLOCKED"])
def test_noncorrective_raw_decisions_cannot_publish_edits_even_with_findings(review_environment, monkeypatch, verdict, continuation):
    changed = _edited_response(review_environment, monkeypatch, _response(verdict, findings=[_finding()]), continuation=continuation)
    assert changed.correction_basis is None
    assert "pre-edit NEEDS_FIX or NEEDS_TESTS" in changed.refusal_reason


@pytest.mark.parametrize("response", [_response("PASS"), "The tests look good.", '{"result":"NEEDS_FIX","findings":[{}]}'])
def test_pass_optional_edits_and_malformed_evidence_are_refused(review_environment, monkeypatch, response):
    changed = _edited_response(review_environment, monkeypatch, response)
    assert changed.correction_basis is None
    assert changed.refusal_reason


@pytest.mark.parametrize("continuation", [False, True])
def test_demonstrated_requirement_fix_retains_current_input_evidence(review_environment, monkeypatch, continuation):
    changed = _edited_response(review_environment, monkeypatch, _response("NEEDS_FIX", findings=[_finding()]), continuation=continuation)
    basis = changed.correction_basis
    assert basis is not None
    assert changed.refusal_reason == ""
    assert basis.head_sha == review_environment.head
    assert basis.contract_identity == "reviewed-input-snapshot"
    assert basis.verdict == "NEEDS_FIX"
    assert len(basis.blockers) == 1
    blocker = basis.blockers[0]
    assert blocker.requirement_ids == (REQUIREMENT_ID,)
    assert blocker.requirement_texts == (REQUIREMENT_TEXT,)
    assert blocker.counterexample == _finding()["counterexample"]
    assert blocker.kind == "IMPLEMENTATION"
    assert validate_review_correction_basis_json(basis.to_json(), review_environment.head) == ""


def test_unknown_requirement_does_not_authorize_correction(review_environment, monkeypatch):
    changed = _edited_response(review_environment, monkeypatch, _response("NEEDS_FIX", findings=[_finding(requirement_id="#99/REQ-001")]))
    assert changed.correction_basis is None


@pytest.mark.parametrize("manifest_mode", ["legacy-extraction", "mixed"])
def test_legacy_prose_is_not_explicit_requirement_authority(review_environment, monkeypatch, manifest_mode):
    legacy_id = "REQ-001-context-hash"
    review_environment.context.requirement_manifest_mode = manifest_mode
    review_environment.context.issue_requirements = [validator.IssueRequirement(legacy_id, "Context only: improve request validation.")]
    changed = _edited_response(review_environment, monkeypatch, _response("NEEDS_FIX", findings=[_finding(requirement_id=legacy_id)]))
    assert changed.correction_basis is None


@pytest.mark.parametrize("permitted_exception", [False, True])
def test_rereview_gap_requires_existing_discovery_exception(review_environment, monkeypatch, permitted_exception):
    review_environment.registry.get.return_value = ReviewerSession(repository="owner/repo", pr_number=100, last_head_sha=review_environment.head)
    gap = _gap()
    if permitted_exception:
        gap.update(
            discovery_phase="REREVIEW",
            rereview_exception_reason="PROTECTION_WEAKENED",
            rereview_exception_evidence="The corrective diff removed the empty-request assertion at the production entry point.",
        )
    changed = _edited_response(review_environment, monkeypatch, _response("NEEDS_TESTS", gaps=[gap]))
    assert (changed.correction_basis is not None) is permitted_exception


@pytest.mark.parametrize("required_deliverable", [False, True])
def test_current_material_gap_and_explicit_test_deliverable_remain_correctable(review_environment, monkeypatch, required_deliverable):
    if required_deliverable:
        review_environment.context.issue_requirements[0].text = "The PR must add a committed regression test that rejects an empty request."
    changed = _edited_response(review_environment, monkeypatch, _response("NEEDS_TESTS", gaps=[_gap()]))
    basis = changed.correction_basis
    assert basis is not None
    assert basis.verdict == "NEEDS_TESTS"
    assert basis.blockers[0].kind == ("IMPLEMENTATION" if required_deliverable else "TEST_ORACLE")
    assert basis.blockers[0].regression == _gap()["focused_regression_scenario"]
    assert validate_review_correction_basis_json(basis.to_json(), review_environment.head) == ""


def test_generic_missing_test_finding_is_not_material_gap_authority(review_environment, monkeypatch):
    finding = _finding(
        actual_behavior="A dedicated empty-request test is missing.",
        evidence="No test invokes the empty-request entry point.",
        counterexample="A future refactor could accidentally remove the guard.",
    )
    changed = _edited_response(review_environment, monkeypatch, _response("NEEDS_FIX", findings=[finding]))
    assert changed.correction_basis is None


def test_incomplete_material_gap_does_not_authorize_optional_test_expansion(review_environment, monkeypatch):
    gap = _gap()
    del gap["material_consequence"]
    changed = _edited_response(review_environment, monkeypatch, _response("NEEDS_TESTS", gaps=[gap]))
    assert changed.correction_basis is None


@pytest.mark.parametrize("currently_upheld", [False, True])
def test_recorded_open_gap_needs_current_independent_evidence(review_environment, monkeypatch, currently_upheld):
    gap = validator.parse_adversarial_validation_response(_response("NEEDS_TESTS", gaps=[_gap()])).test_oracle_gaps[0]
    review_environment.registry.get.return_value = ReviewerSession(repository="owner/repo", pr_number=100, last_head_sha=review_environment.head, test_oracle_gaps=[gap])
    dispositions = []
    if currently_upheld:
        review_environment.threads = (SimpleNamespace(thread_id="gap-thread", root_comment_database_id=7, original_finding=f"Gap identity: \u0060{gap.gap_id}\u0060\n\u0060{REQUIREMENT_ID}\u0060: {REQUIREMENT_TEXT}"),)
        dispositions = [{"thread_id": "gap-thread", "status": "STILL_VALID", "rationale": "Rechecked the current empty-request boundary.", "evidence": "Current inspection: tests/test_source.py never invokes the empty request."}]
    response = _response("NEEDS_TESTS", gaps=[{"gap_id": gap.gap_id, "status": "OPEN"}], thread_dispositions=dispositions)
    changed = _edited_response(review_environment, monkeypatch, response)
    if currently_upheld:
        assert changed.correction_basis is not None
        assert "Current inspection:" in changed.correction_basis.blockers[0].evidence
    else:
        assert changed.correction_basis is None


@pytest.mark.parametrize("accepted_strong_gap", [False, True])
def test_current_complete_gap_report_supplies_its_own_evidence(review_environment, monkeypatch, accepted_strong_gap):
    original = _gap()
    if accepted_strong_gap:
        record = AcceptedFindingRecord(
            source_identity="accepted-strong-gap",
            finding_id="empty-request-oracle",
            originating_head_sha=review_environment.head,
            requirement_ids=(REQUIREMENT_ID,),
            requirement_texts=(REQUIREMENT_TEXT,),
            original_scope=original["invariant"],
            affected_boundary=original["authoritative_boundary"],
            category=CATEGORY_REGRESSION_GAP,
            canonical_blocker_id="canonical-gap",
            evidence="tests/test_source.py:1 contains only the nonempty request.",
            plausible_incorrect_implementation=original["plausible_incorrect_implementation"],
            why_tests_admit_it=original["why_tests_still_pass"],
            material_consequence=original["material_consequence"],
            focused_regression_scenario=original["focused_regression_scenario"],
        )
        gap = known_gap_from_record(record)
        review_environment.bridge.project.side_effect = lambda target, *_args, **_kwargs: AcceptedFindingProjection(target=target, records=(record,), complete=True)
    else:
        gap = validator.parse_adversarial_validation_response(_response("NEEDS_TESTS", gaps=[original])).test_oracle_gaps[0]
        review_environment.registry.get.return_value = ReviewerSession(repository="owner/repo", pr_number=100, last_head_sha=review_environment.head, test_oracle_gaps=[gap])
    current = {**original, "gap_id": gap.gap_id, "why_tests_still_pass": "Current collection at admitted HEAD contains only the nonempty request test."}
    changed = _edited_response(review_environment, monkeypatch, _response("NEEDS_TESTS", gaps=[current]))
    assert changed.correction_basis is not None
    assert "Current collection at admitted HEAD" in changed.correction_basis.blockers[0].evidence
    assert original["why_tests_still_pass"] not in changed.correction_basis.blockers[0].evidence


def test_dynamic_wrapper_edits_have_no_reviewer_correction_authority(review_environment, monkeypatch):
    script = review_environment.root / "check.sh"
    script.write_text("printf 'incidental = True\\n' > source.py\n")
    _git(review_environment.root, "add", "check.sh")
    _git(review_environment.root, "commit", "-m", "Fixture test wrapper")
    review_environment.head = _git(review_environment.root, "rev-parse", "HEAD")
    review_environment.config.TEST_SCRIPT_PATH = str(script)
    prompt = Mock(return_value=_response("INCONCLUSIVE", dynamic_check_requested="tests/test_source.py"))
    monkeypatch.setattr(validator, "run_llm_prompt", prompt)
    with pytest.raises(ReviewChangesProduced) as captured:
        _run(review_environment)
    assert captured.value.correction_basis is None
    assert _git(review_environment.root, "rev-parse", "HEAD") == review_environment.head
    review_environment.manager.continue_session.assert_not_called()
