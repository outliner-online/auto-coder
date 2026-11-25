"""External review roots remain context without becoming executable references."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

import pytest

from auto_coder.adversarial_validator import AdversarialValidationFinding, AdversarialValidationResult
from auto_coder.canonical_pr_blocker_ledger import BlockerAdmissionPayload, BlockerAlias, CanonicalPRBlockerLedger, QualifiedRequirement
from auto_coder.external_review_feedback import retain_external_review_feedback
from auto_coder.ordinary_closure_evidence import deserialize_ordinary_result, serialize_ordinary_result
from auto_coder.ordinary_correction_references import build_correction_reference_context, correction_scope_fingerprint, validate_correction_reference
from auto_coder.pr_finding_reconciliation import HistoricalRootParseResult, reconcile_pr_findings_before_publication
from auto_coder.util.gh_cache import ReviewThread, ReviewThreadComment

API_ORIGIN = "https://api.github.com"
REPOSITORY = "outliner-online/outliner"
PR_NUMBER = 6027
REQUIREMENTS = ("#5984/REQ-001", "#5984/REQ-002", "#5984/REQ-003", "#5984/REQ-004", "#5984/REQ-012")
SOURCE_TEXT = (
    "**#5984/REQ-001–004 and REQ-012: support the saved Grid and Calendar production paths.** "
    "Given a valid saved Grid or Calendar requested via the shipped service, its default opener registry only provides Kanban and this branch rejects the request. "
    "The fake Grid opener in the tests does not establish a live Hocuspocus/PGlite observation or UI-equivalent snapshot. "
    "Wire the real producers and run the committed lifecycle/parity evidence against the shipped path, including the heterogeneous source controls required in REQ-012."
)


def _external_root(ledger: CanonicalPRBlockerLedger) -> str:
    comment = ReviewThreadComment(database_id=4665182463, body=SOURCE_TEXT, author_login="reviewer")
    thread = ReviewThread(id="PRRT_kwDOOtx83s660YTf", comments=[comment])
    return retain_external_review_feedback(ledger, REPOSITORY, PR_NUMBER, thread, comment, "original-head")[0]


def _finding() -> AdversarialValidationFinding:
    return AdversarialValidationFinding(
        requirement_ids=list(REQUIREMENTS),
        finding_identity="missing-grid-calendar-production-observers",
        correction_identity="wire-grid-calendar-production-observers",
        anchor_path="server/src/notifications/view-observation.ts",
        anchor_line=86,
        actual_behavior="The default opener registry provides only Kanban and rejects saved Grid and Calendar views.",
        required_behavior="Register the real Grid and Calendar observation producers and verify their shipped lifecycle and snapshot parity.",
        evidence="The shipped default registry contains only Kanban; Grid coverage substitutes a fake opener.",
        counterexample="Request a valid saved Grid through the production notification service.",
    )


def test_raw_external_root_is_preserved_as_source_context_without_a_canonical_reference(tmp_path) -> None:
    ledger = CanonicalPRBlockerLedger(tmp_path / "blockers.sqlite3")
    owner = _external_root(ledger)
    before = ledger.get_snapshot(API_ORIGIN, REPOSITORY, PR_NUMBER)
    source = before.get_blocker(owner)
    assert source is not None and source.qualified_requirements == ()

    context = build_correction_reference_context(before, "head", "base", "snapshot", "manifest")

    assert context.references == ()
    rendered = asdict(context)
    assert rendered["external_review_context"] == ({"blocker_id": owner, "authoritative_boundary": "GitHub review thread PRRT_kwDOOtx83s660YTf", "review_text": SOURCE_TEXT},)
    result = AdversarialValidationResult(result="NEEDS_FIX", findings=[_finding()], correction_reference_context=context)
    restored = deserialize_ordinary_result(serialize_ordinary_result(result))
    assert restored.correction_reference_context == context
    assert ledger.get_snapshot(API_ORIGIN, REPOSITORY, PR_NUMBER) == before

    # A model cannot turn the source identifier back into an executable scope.
    reference, error = validate_correction_reference(context, f"canonical:{owner}", correction_scope_fingerprint(source), context.fingerprint, REQUIREMENTS, _finding().anchor_path, "IMPLEMENTATION")
    assert reference is None and "supplied 0 times" in error


@pytest.mark.parametrize("with_requirements,external_alias", [(True, False), (True, True), (False, False)])
def test_registered_corrections_keep_their_strict_scope_binding(tmp_path, with_requirements: bool, external_alias: bool) -> None:
    ledger = CanonicalPRBlockerLedger(tmp_path / "blockers.sqlite3")
    snapshot = ledger.initialize_namespace(API_ORIGIN, REPOSITORY, PR_NUMBER)
    owner, snapshot = ledger.admit_blocker(
        API_ORIGIN,
        REPOSITORY,
        PR_NUMBER,
        operation_id="registered-correction",
        expected_ledger_revision=snapshot.ledger_revision,
        payload=BlockerAdmissionPayload(
            category="IMPLEMENTATION",
            qualified_requirements=(QualifiedRequirement(5984, "REQ-001"),) if with_requirements else (),
            authoritative_boundary="server/src/notifications/view-observation.ts",
            incorrect_behavior_or_missing_invariant="The shipped registry rejects a supported saved view.",
            required_correction_outcome="Register the production observer.",
            aliases=(BlockerAlias(alias_type="external_review_comment", alias_value="comment:42"),) if external_alias else (),
        ),
    )
    context = build_correction_reference_context(snapshot, "head", "base")
    assert len(context.references) == 1
    reference = context.references[0]
    assert reference.blocker_id == owner
    assert reference.scope_fingerprint == correction_scope_fingerprint(snapshot.get_blocker(owner))
    assert context.external_review_context == ()
    validated, error = validate_correction_reference(context, reference.correction_identity, reference.scope_fingerprint, context.fingerprint, reference.requirement_ids, reference.authoritative_boundary, "IMPLEMENTATION")
    assert validated == reference and not error
    _, error = validate_correction_reference(context, reference.correction_identity, reference.scope_fingerprint, context.fingerprint, reference.requirement_ids, "another-file.ts", "IMPLEMENTATION")
    assert "contradicts the authoritative boundary" in error

    # Older retained contexts did not contain the optional source-context field.
    legacy_context = asdict(context)
    legacy_context.pop("external_review_context")
    expected_fingerprint = hashlib.sha256(json.dumps(legacy_context, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    payload = json.loads(serialize_ordinary_result(AdversarialValidationResult(result="NEEDS_FIX", correction_reference_context=context)))
    payload["correction_reference_context"] = legacy_context
    restored = deserialize_ordinary_result(json.dumps(payload))
    assert restored.correction_reference_context is not None
    assert restored.correction_reference_context.fingerprint == expected_fingerprint == context.fingerprint
    assert ledger.get_snapshot(API_ORIGIN, REPOSITORY, PR_NUMBER) == snapshot


def test_independent_finding_retains_a_typed_owner_without_rewriting_the_external_root(tmp_path) -> None:
    ledger = CanonicalPRBlockerLedger(tmp_path / "blockers.sqlite3")
    external_owner = _external_root(ledger)
    before = ledger.get_snapshot(API_ORIGIN, REPOSITORY, PR_NUMBER)
    context = build_correction_reference_context(before, "head", "base")
    finding = _finding()
    result = AdversarialValidationResult(result="NEEDS_FIX", findings=[finding], correction_reference_context=context)

    first = reconcile_pr_findings_before_publication(ledger, API_ORIGIN, REPOSITORY, PR_NUMBER, 5984, "head", "base", result, HistoricalRootParseResult(), attempt_id="first")

    assert not first.is_ambiguous
    owner = first.blocker_for_finding[0][1]
    assert owner != external_owner
    assert first.unrooted_findings == (finding,)
    after = ledger.get_snapshot(API_ORIGIN, REPOSITORY, PR_NUMBER)
    assert after.get_blocker(external_owner) == before.get_blocker(external_owner)
    assert len(after.get_open_blockers()) == 2
    current_context = build_correction_reference_context(after, "head", "base")
    assert tuple(reference.blocker_id for reference in current_context.references) == (owner,)
    assert current_context.external_review_context == context.external_review_context

    # Reobservation uses normal typed-scope reconciliation rather than creating
    # another owner or treating the unqualified external source as fixed.
    replay = AdversarialValidationResult(result="NEEDS_FIX", findings=[_finding()], correction_reference_context=current_context)
    second = reconcile_pr_findings_before_publication(ledger, API_ORIGIN, REPOSITORY, PR_NUMBER, 5984, "head", "base", replay, HistoricalRootParseResult(), attempt_id="second")
    assert not second.is_ambiguous
    assert second.blocker_for_finding[0][1] == owner
    final = ledger.get_snapshot(API_ORIGIN, REPOSITORY, PR_NUMBER)
    assert len(final.get_open_blockers()) == 2
    assert final.get_blocker(external_owner) == before.get_blocker(external_owner)
