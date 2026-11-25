"""Recovery of a completed discussion review whose producing binding was lost."""

import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from auto_coder.adversarial_validation_attempts import AdversarialValidationAttemptRepository
from auto_coder.effective_decision_application import EffectiveDecisionStore
from auto_coder.pr_processor import _invalidate_retained_discussion_authority
from auto_coder.pr_review_effects import ReviewEffectRepository
from tests.test_accepted_finding_bridge import REPO, env  # noqa: F401
from tests.test_effective_decision_pr_flow import ClosureScript, SimulatedCrash, _reconstruct_flow_stores, flow_env, ordinary_response  # noqa: F401
from tests.test_single_review_ordinary_closure_flow import ADDRESSED, _repaired_flow


def _completed_review_without_binding(flow_env, monkeypatch):
    """Retain the historical interrupted effect through real production stores."""
    flow = _repaired_flow(flow_env, monkeypatch, 7480, resolve_on_approve=True)
    flow.production_thread_collection = True
    flow.auto_status = True
    flow.thread_resolved = True
    flow.model_responses = [json.dumps({**json.loads(ADDRESSED), "thread_dispositions": []})]
    first = flow.run(closure=ClosureScript(status="FIXED"), public_origin=True)
    accepted = flow.env.cycle.snapshot(flow.pr).accepted_closure
    assert accepted is not None and accepted.publication_status == "PENDING", first
    effects = ReviewEffectRepository(REPO)
    operations = [item for item in json.loads(effects.storage_path.read_text())["operations"].values() if item["pr_number"] == flow.pr and item["purpose"] == "finding-thread-resolution"]
    assert len(operations) == 1 and operations[0]["status"] == "UNCERTAIN", first
    assert json.loads(operations[0]["exact_payload"])["discussion_evidence"] == []

    flow.model_responses = [ordinary_response(flow.thread_id, status="ADDRESSED", evidence="tests/test_state.py independently evaluates the complete current root")]
    with patch("auto_coder.pr_processor._consume_pending_two_tier_publication", return_value=(False, "retained effect awaits reconciliation")):
        second = flow.run(closure=ClosureScript(status="FIXED"), public_origin=True)
    retained = flow.retained()
    assert retained is not None and retained.status == "PASS" and retained.publication == "CONFIRMED", second
    assert retained.ordinary_correction is not None
    assert len([key for key in retained.reconciliation_attempts if key.startswith("discussion-revalidation-v1:")]) == 1, second
    # The historical transport invalidated the later successful binding while
    # trying to consume the immutable effect with its empty original digest.
    _invalidate_retained_discussion_authority(REPO, flow.pr, [flow.thread_id])
    retained = flow.retained()
    assert retained.ordinary_correction is None
    assert retained.discussion_revalidation_thread_ids == [flow.thread_id]
    _reconstruct_flow_stores(flow)
    return flow, operations[0]


def test_completed_discussion_review_recovers_binding_and_original_effect_once(flow_env, monkeypatch):
    flow, original_effect = _completed_review_without_binding(flow_env, monkeypatch)
    calls, reviews, handoffs = flow.model_calls, len(flow.reviews), flow.handoffs
    flow.model_responses = [ordinary_response(flow.thread_id, status="ADDRESSED", evidence="tests/test_state.py independently evaluates the complete current root")]

    def assert_reserved_before_review():
        keys = [key for key in flow.retained().reconciliation_attempts if key.startswith("discussion-binding-recovery-v1:")]
        assert len(keys) == 1

    with patch("auto_coder.pr_processor._record_pr_stage") as stages:
        recovered = flow.run(closure=ClosureScript(status="FIXED"), public_origin=True, on_review=assert_reserved_before_review)
    assert flow.model_calls == calls + 1, recovered
    admissions = [call.args for call in stages.call_args_list if call.args[4].get("effect") == "saved-discussion-binding-recovery"]
    assert len(admissions) == 1
    admission = admissions[0]
    assert admission[1] == "pr.adversarial-validation" and admission[3].value == "deferred"
    assert admission[4]["thread_ids"] == [flow.thread_id]
    assert admission[4]["examined_head"] == flow.head and admission[4]["requires_reassessment"] is True
    _reconstruct_flow_stores(flow)
    flow.model_responses = []
    with patch("auto_coder.pr_processor._record_pr_stage") as stages:
        resumed = flow.run(closure=ClosureScript(status="FIXED"), public_origin=True)
    assert not [call for call in stages.call_args_list if call.args[4].get("effect") == "saved-discussion-binding-recovery"]
    assert flow.model_calls == calls + 1, resumed
    assert flow.handoffs == handoffs
    assert len(flow.reviews) == reviews + 1
    assert flow.merge.call_count == 1, (recovered, resumed)
    retained = EffectiveDecisionStore(REPO).load(flow.pr)
    assert retained is not None and retained.ordinary_correction is not None
    assert retained.discussion_revalidation_thread_ids == []
    effect = ReviewEffectRepository(REPO).get(original_effect["operation_id"])
    assert effect.status == "CONFIRMED"
    assert effect.exact_payload == original_effect["exact_payload"]
    assert flow.env.cycle.snapshot(flow.pr).accepted_closure.publication_status == "ACKNOWLEDGED"


@pytest.mark.parametrize("defect", ["unconfirmed_publication", "wrong_head", "wrong_base", "wrong_epoch", "unknown_attempt", "unfinished_attempt", "newer_attempt", "unpublished_attempt"])
def test_binding_recovery_requires_current_completed_published_attempt(flow_env, monkeypatch, defect):
    flow, original_effect = _completed_review_without_binding(flow_env, monkeypatch)
    store = EffectiveDecisionStore(REPO)
    retained = store.load(flow.pr)
    attempts = AdversarialValidationAttemptRepository(REPO)
    if defect == "unconfirmed_publication":
        store.update(flow.pr, publication="PENDING")
    elif defect == "wrong_head":
        store.update(flow.pr, head_sha="different-head")
    elif defect == "wrong_base":
        store.update(flow.pr, base_sha="different-base")
    elif defect == "wrong_epoch":
        store.update(flow.pr, reopen_epoch=retained.reopen_epoch + 1)
    elif defect == "unknown_attempt":
        store.update(flow.pr, attempt_id="unknown-attempt")
    elif defect == "unfinished_attempt":
        attempts.finish(retained.attempt_id, "IN_PROGRESS")
    elif defect == "newer_attempt":
        attempts.start(flow.pr, flow.head)
    else:
        saved = json.loads(attempts.storage_path.read_text())
        next(item for item in saved["attempts"] if item["id"] == retained.attempt_id).pop("published")
        attempts.storage_path.write_text(json.dumps(saved))
    calls, reviews, handoffs = flow.model_calls, len(flow.reviews), flow.handoffs
    flow.model_responses = []
    refused = flow.run(closure=ClosureScript(status="FIXED"), public_origin=True)
    assert flow.model_calls == calls, refused
    assert len(flow.reviews) == reviews and flow.handoffs == handoffs
    flow.merge.assert_not_called()
    assert ReviewEffectRepository(REPO).get(original_effect["operation_id"]).status == "UNCERTAIN"


@pytest.mark.parametrize("observation", ["truncated", "missing"])
def test_binding_recovery_requires_complete_current_discussion(flow_env, monkeypatch, observation):
    flow, original_effect = _completed_review_without_binding(flow_env, monkeypatch)
    original_threads = flow.threads
    monkeypatch.setattr(flow, "threads", lambda: tuple(replace(thread, comments_truncated=True) for thread in original_threads()) if observation == "truncated" else ())
    calls, reviews, handoffs = flow.model_calls, len(flow.reviews), flow.handoffs
    flow.model_responses = []
    refused = flow.run(closure=ClosureScript(status="FIXED"), public_origin=True)
    assert flow.model_calls == calls, refused
    assert len(flow.reviews) == reviews and flow.handoffs == handoffs
    flow.merge.assert_not_called()
    assert ReviewEffectRepository(REPO).get(original_effect["operation_id"]).status == "UNCERTAIN"


@pytest.mark.parametrize("result", ["inconclusive", "omitted", "error", "interrupted"])
def test_unsuccessful_binding_recovery_cannot_repeat_for_unchanged_input(flow_env, monkeypatch, result):
    flow, original_effect = _completed_review_without_binding(flow_env, monkeypatch)
    calls, handoffs = flow.model_calls, flow.handoffs
    raw = json.loads(ordinary_response(flow.thread_id, status="INCONCLUSIVE", evidence="The supplied execution does not establish the exact invariant"))
    if result == "omitted":
        raw["thread_dispositions"] = []
    elif result == "error":
        raw = {"result": "ERROR", "summary": "The independent reviewer could not complete the examination."}
    flow.model_responses = [json.dumps(raw)]
    if result == "interrupted":

        def crash():
            raise SimulatedCrash()

        with pytest.raises(SimulatedCrash):
            flow.run(closure=ClosureScript(status="FIXED"), public_origin=True, on_review=crash)
    else:
        first = flow.run(closure=ClosureScript(status="FIXED"), public_origin=True)
        assert flow.model_calls == calls + 1, first
    _reconstruct_flow_stores(flow)
    flow.model_responses = []
    reviews = len(flow.reviews)
    for _ in range(2):
        again = flow.run(closure=ClosureScript(status="FIXED"), public_origin=True)
        assert flow.model_calls == calls + 1, again
        assert len(flow.reviews) == reviews
    assert flow.handoffs == handoffs
    flow.merge.assert_not_called()
    assert ReviewEffectRepository(REPO).get(original_effect["operation_id"]).status == "UNCERTAIN"


def test_later_completed_attempt_does_not_renew_spent_binding_recovery(flow_env, monkeypatch):
    flow, original_effect = _completed_review_without_binding(flow_env, monkeypatch)
    calls = flow.model_calls
    flow.model_responses = [ordinary_response(flow.thread_id, status="INCONCLUSIVE", evidence="The exact concern could not be independently established")]
    first = flow.run(closure=ClosureScript(status="FIXED"), public_origin=True)
    assert flow.model_calls == calls + 1, first
    attempts = AdversarialValidationAttemptRepository(REPO)
    later = attempts.start(flow.pr, flow.head)
    attempts.finish(later.attempt_id, "PASS")
    attempts.mark_published(later.attempt_id)
    EffectiveDecisionStore(REPO).update(flow.pr, status="PASS", publication="CONFIRMED", ordinary_correction=None, next_action="NONE", attempt_id=later.attempt_id, attempt_sequence=later.sequence, discussion_revalidation_thread_ids=[flow.thread_id])
    _reconstruct_flow_stores(flow)
    flow.model_responses = []
    again = flow.run(closure=ClosureScript(status="FIXED"), public_origin=True)
    assert flow.model_calls == calls + 1, again
    assert len([key for key in flow.retained().reconciliation_attempts if key.startswith("discussion-binding-recovery-v1:")]) == 1
    flow.merge.assert_not_called()
    assert ReviewEffectRepository(REPO).get(original_effect["operation_id"]).status == "UNCERTAIN"
