"""Publisher continuity across ordinary review persistence and transport boundaries."""

import json
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from auto_coder import pr_processor
from auto_coder.automation_config import PRProcessingOutcome
from auto_coder.effective_decision_application import PUBLICATION_PENDING, DecisionRetentionError, EffectiveDecisionStore, build_retained_record, derive_application, retain_ordinary_correction
from auto_coder.github_app_reviewer import ReviewerAppIdentity, ReviewPublicationResult
from tests.test_accepted_finding_bridge import REPO, Env, env  # noqa: F401
from tests.test_effective_decision_pr_flow import SimulatedCrash, _historical_root_with_current_violation, _reconstruct_flow_stores, flow_env  # noqa: F401
from tests.test_ordinary_correction_retention import ordinary_violation


def retained_ordinary(env, *, version=1, app_id=4765828):
    raw = ordinary_violation(env, 8101)
    projection = env.bridge().project(env.target(8101))
    evidence = retain_ordinary_correction(raw, projection)
    application = derive_application(raw, projection)
    store = EffectiveDecisionStore(REPO)
    store.retain(build_retained_record(application.decision, projection, env.head, env.base, evidence))
    data = json.loads(store.storage_path.read_text())
    data["8101"]["publication_identity_version"] = version
    data["8101"]["publication_publisher_app_id"] = app_id
    store.storage_path.write_text(json.dumps(data))
    return store, raw


@pytest.mark.parametrize("version, saved_app, current_app", [(0, 0, 5792531), (1, 4765828, 5792531), (1, 4765828, 0), (True, 0, 4765828), (1, True, 4765828), (2, 0, 4765828), (1, 4765828, True)])
def test_retained_ordinary_never_sends_under_changed_or_unknown_publisher(env: Env, monkeypatch, version, saved_app, current_app):
    store, raw = retained_ordinary(env, version=version, app_id=saved_app)
    before = store.storage_path.read_bytes()
    send = MagicMock(return_value=ReviewPublicationResult(True, "REQUEST_CHANGES"))
    monkeypatch.setattr(pr_processor, "publish_adversarial_review", send)
    monkeypatch.setattr(pr_processor, "resolve_reviewer_app_identity", lambda _repo: ReviewerAppIdentity(login="reviewer[bot]" if current_app else "", app_id=current_app))
    with pytest.raises(DecisionRetentionError, match="publisher"):
        pr_processor._publish_effective_review(REPO, 8101, env.head, raw, None)
    send.assert_not_called()
    assert store.storage_path.read_bytes() == before
    assert store.load(8101).publication == PUBLICATION_PENDING


@pytest.mark.parametrize("version, saved_app", [(0, 0), (1, 5792531)])
def test_production_ordinary_resumption_holds_without_canonical_intent(flow_env: Env, monkeypatch, version, saved_app):
    flow, script, _ = _historical_root_with_current_violation(flow_env, monkeypatch, "cloud", "missing")
    with patch("auto_coder.pr_processor._publish_effective_review", side_effect=SimulatedCrash), pytest.raises(SimulatedCrash):
        flow.run(closure=script)
    store = EffectiveDecisionStore(REPO)
    data = json.loads(store.storage_path.read_text())
    data[str(flow.pr)]["publication_identity_version"] = version
    data[str(flow.pr)]["publication_publisher_app_id"] = saved_app
    store.storage_path.write_text(json.dumps(data))
    before = store.load(flow.pr)
    assert before is not None and before.publication == PUBLICATION_PENDING
    assert flow_env.ledger.get_pending_publication_intents("https://api.github.com", REPO, flow.pr) == ()
    _reconstruct_flow_stores(flow)
    flow.model_responses = []
    for force in (False, True):
        actions = flow.run(closure=script, force=force)
        assert any("publisher identity" in action for action in actions), list(actions)
        assert flow.reviews == [], list(actions)
        assert flow.status.outcome is PRProcessingOutcome.FAILED
        assert flow.handoffs == 0 and flow.merge.call_count == 0
        assert flow.model_calls == 1
    after = store.load(flow.pr)
    assert after.ordinary_correction == before.ordinary_correction
    assert after.publication == PUBLICATION_PENDING


@pytest.mark.parametrize("app_id", [4765828, 5792531])
@pytest.mark.parametrize("already_bound", [False, True])
@pytest.mark.parametrize("with_authority", [False, True])
def test_fresh_or_same_publisher_is_persisted_before_send(env: Env, monkeypatch, app_id, already_bound, with_authority):
    store, raw = retained_ordinary(env, app_id=app_id if already_bound else 0)
    identity = ReviewerAppIdentity(login="reviewer[bot]", app_id=app_id)
    authority = MagicMock() if with_authority else None
    monkeypatch.setattr(pr_processor, "resolve_reviewer_app_identity", lambda _repo: identity)

    def send(*args, **kwargs):
        current = store.load(8101)
        assert current.publication_publisher_app_id == identity.app_id
        assert current.publication == PUBLICATION_PENDING
        assert kwargs["expected_publisher"] == identity
        if authority is not None:
            assert kwargs["approval_authority"] is authority
        return ReviewPublicationResult(True, "REQUEST_CHANGES")

    publish = MagicMock(side_effect=send)
    monkeypatch.setattr(pr_processor, "publish_adversarial_review", publish)
    assert pr_processor._publish_effective_review(REPO, 8101, env.head, raw, authority).success
    publish.assert_called_once()
    assert store.load(8101).publication == PUBLICATION_PENDING


@pytest.mark.parametrize("version,app_id", [(0, 0), (1, 4765828)])
def test_same_producer_rederivation_preserves_original_publication_identity(env: Env, version, app_id):
    store, _ = retained_ordinary(env, version=version, app_id=app_id)
    record = store.load(8101)
    fresh = replace(record, publication_identity_version=1, publication_publisher_app_id=5792531)
    kept = store.retain(fresh)
    assert (kept.publication_identity_version, kept.publication_publisher_app_id) == (version, app_id)
    assert kept.ordinary_correction == record.ordinary_correction


def test_identity_persistence_failure_suppresses_send(env: Env, monkeypatch):
    store, raw = retained_ordinary(env, app_id=0)
    before = store.storage_path.read_bytes()
    monkeypatch.setattr(pr_processor, "resolve_reviewer_app_identity", lambda _repo: ReviewerAppIdentity("reviewer[bot]", 4765828))
    monkeypatch.setattr(EffectiveDecisionStore, "_write", lambda *a: (_ for _ in ()).throw(OSError("synthetic storage failure")))
    send = MagicMock()
    monkeypatch.setattr(pr_processor, "publish_adversarial_review", send)
    with pytest.raises(DecisionRetentionError, match="publisher"):
        pr_processor._publish_effective_review(REPO, 8101, env.head, raw, None)
    send.assert_not_called()
    assert store.storage_path.read_bytes() == before


def test_publisher_binding_refuses_a_replaced_producer(env: Env):
    store, _ = retained_ordinary(env, app_id=0)
    expected = store.load(8101)
    store.save(replace(expected, attempt_sequence=expected.attempt_sequence + 1))
    before = store.storage_path.read_bytes()
    with pytest.raises(DecisionRetentionError, match="publisher target changed"):
        store.pin_publication_publisher(expected, 4765828)
    assert store.storage_path.read_bytes() == before


def test_selected_transport_must_match_the_pinned_publisher(env: Env, monkeypatch):
    from auto_coder import github_app_reviewer

    store, raw = retained_ordinary(env)
    reviewer = MagicMock()
    reviewer.get_identity.return_value = ReviewerAppIdentity("new-reviewer[bot]", 5792531)
    monkeypatch.setattr(github_app_reviewer, "load_reviewer_app_config", lambda **kwargs: object())
    monkeypatch.setattr(github_app_reviewer, "GitHubAppReviewer", lambda *a, **k: reviewer)
    result = github_app_reviewer.publish_adversarial_review(REPO, 8101, env.head, raw, ledger=env.ledger, expected_publisher=ReviewerAppIdentity("reviewer[bot]", 4765828))
    assert not result.success and result.policy_refusal
    reviewer.publish.assert_not_called()
    assert store.load(8101).publication == PUBLICATION_PENDING


def test_stale_writer_cannot_erase_a_publisher_binding(env: Env, monkeypatch):
    store, _ = retained_ordinary(env, app_id=0)
    stale = store.load(8101)
    EffectiveDecisionStore(REPO).pin_publication_publisher(stale, 4765828)
    monkeypatch.setattr(store, "load", lambda _pr: stale)
    store.retain(replace(stale, wait_reason="later bookkeeping"))
    kept = EffectiveDecisionStore(REPO).load(8101)
    assert kept.publication_publisher_app_id == 4765828
    assert kept.ordinary_correction == stale.ordinary_correction


def test_concurrent_initial_bindings_choose_only_one_publisher(env: Env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    store, _ = retained_ordinary(env, app_id=0)
    record = store.load(8101)
    barrier = Barrier(2)

    def bind(app_id):
        barrier.wait(timeout=5)
        try:
            return EffectiveDecisionStore(REPO).pin_publication_publisher(record, app_id).publication_publisher_app_id
        except DecisionRetentionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(bind, [4765828, 5792531]))
    winners = [x for x in results if x is not None]
    assert len(winners) == 1
    assert store.load(8101).publication_publisher_app_id == winners[0]


def test_different_attempt_cannot_use_retained_publisher_authority(env: Env, monkeypatch):
    store, raw = retained_ordinary(env)
    before = store.storage_path.read_bytes()
    send = MagicMock()
    monkeypatch.setattr(pr_processor, "publish_adversarial_review", send)
    with pytest.raises(DecisionRetentionError, match="publisher target changed"):
        pr_processor._publish_effective_review(REPO, 8101, env.head, replace(raw, attempt_sequence=raw.attempt_sequence + 1), None)
    send.assert_not_called()
    assert store.storage_path.read_bytes() == before


@pytest.mark.parametrize("version,app_id", [(0, 0), (1, 4765828)])
def test_changed_head_does_not_erase_unknown_or_attempted_publication(env: Env, monkeypatch, version, app_id):
    store, raw = retained_ordinary(env, version=version, app_id=app_id)
    before = store.storage_path.read_bytes()
    send = MagicMock()
    monkeypatch.setattr(pr_processor, "publish_adversarial_review", send)
    with pytest.raises(DecisionRetentionError, match="publisher.*another target"):
        pr_processor._publish_effective_review(REPO, 8101, "changed-head", raw, None)
    send.assert_not_called()
    assert store.storage_path.read_bytes() == before
