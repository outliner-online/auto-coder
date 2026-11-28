"""Late PR stops fence supported publication and protection effects."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from auto_coder import pr_processor
from auto_coder.automation_config import AutomationConfig
from auto_coder.pr_automation_guard import PRCommit, PRCommitHistory, PRGuardStore, assess_pr_automation
from auto_coder.pr_repair_dispatch import RepairDisposition
from auto_coder.utils import CommandResult
from tests.test_base_update_execution_git import clean_update_repository, git

REPOSITORY = "owner/repo"


def _latch_stop(store: PRGuardStore, pr_number: int, reason: str) -> None:
    count = 2 if reason == "revert" else 30
    commits = tuple(PRCommit(f"{index:040x}", 'Revert "failed correction"' if reason == "revert" and index == count else f"Repair {index}") for index in range(1, count + 1))
    assert store.observe(REPOSITORY, pr_number, PRCommitHistory(commits[-1].sha, count, commits)).stopped


@pytest.mark.parametrize("stop_reason", ["revert", "commit_limit"])
@pytest.mark.parametrize("boundary", ["before_merge", "before_push", "before_retry"])
def test_base_update_refuses_new_effect_after_latched_stop(tmp_path, monkeypatch, _use_real_commands, boundary, stop_reason):
    work, remote, pr_data = clean_update_repository(tmp_path)
    monkeypatch.chdir(work)
    pr_number = pr_data["number"]
    input_head = pr_data["head"]["sha"]
    client = SimpleNamespace(get_pr_automation_history_strict=Mock(return_value=PRCommitHistory(input_head, 1, (PRCommit(input_head, "feature"),))))
    store = PRGuardStore()
    assert assess_pr_automation(client, REPOSITORY, pr_number).allowed
    real_command = pr_processor.cmd.run_command
    merged = False
    push_calls = 0

    def run(command, *args, **kwargs):
        nonlocal merged
        result = real_command(command, *args, **kwargs)
        if command[:3] == ["git", "rev-list", "--count"] and boundary == "before_merge":
            _latch_stop(store, pr_number, stop_reason)
        if command[:2] == ["git", "merge"]:
            assert result.success
            merged = True
            if boundary == "before_push":
                _latch_stop(store, pr_number, stop_reason)
        return result

    def push(**_kwargs):
        nonlocal push_calls
        push_calls += 1
        if boundary == "before_retry" and push_calls == 1:
            _latch_stop(store, pr_number, stop_reason)
            return CommandResult(False, "", "controlled rejection before send", 1)
        # A mistaken new send is observable at the actual bare remote.
        published = git(work, "push", "origin", "HEAD:feature", check=False)
        return CommandResult(published.returncode == 0, published.stdout, published.stderr, published.returncode)

    monkeypatch.setattr(pr_processor.cmd, "run_command", run)
    monkeypatch.setattr(pr_processor, "git_push", push)
    monkeypatch.setattr("auto_coder.base_update_execution.time.sleep", lambda _seconds: None)
    result = pr_processor._update_with_base_branch(REPOSITORY, pr_data, AutomationConfig(), client)

    # A previously observed stop survives a remote head restored to the old H.
    # The history reader and destination both still expose H during this call.
    assert store.status(REPOSITORY, pr_number).stopped
    assert result.repair_result is not None
    assert result.repair_result.disposition is RepairDisposition.REFUSED
    assert "external intervention" in result.repair_result.reason
    assert merged is (boundary != "before_merge")
    assert push_calls == (1 if boundary == "before_retry" else 0)
    assert git(remote, "rev-parse", "refs/heads/feature").stdout.strip() == input_head
    candidate = git(work, "rev-parse", "HEAD").stdout.strip()
    assert (candidate != input_head) is merged


def test_healthy_base_update_retains_real_publication(tmp_path, monkeypatch, _use_real_commands):
    work, remote, pr_data = clean_update_repository(tmp_path)
    monkeypatch.chdir(work)
    input_head = pr_data["head"]["sha"]
    client = SimpleNamespace(get_pr_automation_history_strict=Mock(return_value=PRCommitHistory(input_head, 1, (PRCommit(input_head, "feature"),))))
    sends = []

    def push(**kwargs):
        sends.append(kwargs)
        published = git(work, "push", "origin", kwargs["branch"], check=False)
        return CommandResult(published.returncode == 0, published.stdout, published.stderr, published.returncode)

    monkeypatch.setattr(pr_processor, "git_push", push)
    result = pr_processor._update_with_base_branch(REPOSITORY, pr_data, AutomationConfig(), client)

    assert result.repair_result is not None
    assert result.repair_result.disposition is RepairDisposition.APPLIED
    assert result.repair_result.synchronized
    assert sends == [{"branch": "HEAD:feature", "expected_remote_sha": input_head}]
    candidate = git(work, "rev-parse", "HEAD").stdout.strip()
    assert candidate != input_head
    assert git(remote, "rev-parse", "refs/heads/feature").stdout.strip() == candidate
    assert not PRGuardStore().status(REPOSITORY, pr_data["number"]).stopped


from auto_coder.adversarial_validator import AdversarialValidationResult, ChangeProvenanceItem, ReviewThreadDisposition
from auto_coder.canonical_pr_blocker_ledger import BlockerDisposition
from auto_coder.review_thread_validation import resolve_addressed_review_threads
from tests.test_github_app_reviewer import RecordingClient, auth_responses, configured_reviewer, response
from tests.test_review_thread_closure_receipt import API_ORIGIN, BASE, HEAD
from tests.test_review_thread_closure_receipt import PR_NUMBER as THREAD_PR_NUMBER
from tests.test_review_thread_closure_receipt import THREAD_ID
from tests.test_review_thread_closure_receipt import receipt_scenario as receipt_scenario


def _history_client(head):
    return SimpleNamespace(get_pr_automation_history_strict=Mock(return_value=PRCommitHistory(head, 1, (PRCommit(head, "ordinary implementation"),))))


def _effect_authorizer(client, pr_number):
    def authorize():
        decision = assess_pr_automation(client, REPOSITORY, pr_number)
        return "" if decision.allowed else decision.reason

    return authorize


@pytest.mark.parametrize("stop_reason", [None, "revert", "commit_limit"])
def test_effective_review_rechecks_stop_after_head_lookup_before_approval(tmp_path, monkeypatch, stop_reason):
    store = PRGuardStore()
    github = _history_client("sha-a")
    assert assess_pr_automation(github, REPOSITORY, 42).allowed

    class Wire(RecordingClient):
        def request(self, method, url, **kwargs):
            result = super().request(method, url, **kwargs)
            if method == "GET" and url.endswith("/pulls/42") and stop_reason:
                _latch_stop(store, 42, stop_reason)
            return result

    wire = Wire(auth_responses())
    reviewer = configured_reviewer(tmp_path, wire, monkeypatch)
    monkeypatch.setattr(pr_processor, "publish_adversarial_review", reviewer.publish)
    result = AdversarialValidationResult(result="PASS", summary="Independent review completed")

    publication = pr_processor._publish_effective_review(REPOSITORY, 42, "sha-a", result, None, github_client=github)

    reviews = [call for call in wire.calls if call[0] == "POST" and call[1].endswith("/reviews")]
    if stop_reason:
        assert not publication.success and publication.policy_refusal
        assert "external intervention" in publication.reason
        assert reviews == []
        assert store.status(REPOSITORY, 42).stopped
    else:
        assert publication.success and publication.event == "APPROVE"
        assert len(reviews) == 1
        assert reviews[0][2]["json"]["event"] == "APPROVE"


def test_late_stop_after_sent_clarification_refuses_following_review(tmp_path, monkeypatch):
    store = PRGuardStore()
    github = _history_client("sha-a")
    assert assess_pr_automation(github, REPOSITORY, 42).allowed

    class Wire(RecordingClient):
        def request(self, method, url, **kwargs):
            result = super().request(method, url, **kwargs)
            if method == "POST" and url.endswith("/pulls/42/comments"):
                _latch_stop(store, 42, "revert")
            return result

    wire = Wire(
        auth_responses()[:-1]
        + [
            response(200, [{"filename": "assets/generated.bin", "patch": None}]),
            response(201, {"id": 81}),
            response(200, {"id": 9}),
        ]
    )
    reviewer = configured_reviewer(tmp_path, wire, monkeypatch)
    monkeypatch.setattr(pr_processor, "publish_adversarial_review", reviewer.publish)
    result = AdversarialValidationResult(
        result="INCONCLUSIVE",
        summary="Binary provenance needs clarification",
        unexplained_changes=[ChangeProvenanceItem(paths=["assets/generated.bin"], change_group="Generated artifact", why_unexplained="No generator input is evident")],
    )

    publication = pr_processor._publish_effective_review(REPOSITORY, 42, "sha-a", result, None, github_client=github)

    assert not publication.success and publication.policy_refusal
    business_posts = [url for method, url, _kwargs in wire.calls if method == "POST" and "/pulls/" in url]
    assert business_posts == ["https://api.github.test/repos/owner/repo/pulls/42/comments"]


def _close_guarded_thread(scenario, authorizer, claimed):
    return resolve_addressed_review_threads(
        scenario.client(),
        REPOSITORY,
        THREAD_PR_NUMBER,
        HEAD,
        claimed,
        [
            ReviewThreadDisposition(
                thread_id=THREAD_ID,
                status="ADDRESSED",
                rationale="The retry counter now resets before each retry.",
                evidence="src/retry.py resets the counter; tests/test_retry.py exercises this boundary.",
                discussion_evidence_digest=claimed[0].discussion_evidence_digest,
            )
        ],
        stale_registry=scenario.registry(),
        ledger=scenario.ledger(),
        base_sha=BASE,
        requirement_manifest_revision="contract-v1",
        review_attempt_id="guard-effect-review",
        new_effect_authorizer=authorizer,
    )


@pytest.mark.parametrize("stop_reason", ["revert", "commit_limit"])
@pytest.mark.parametrize("boundary", ["before_acceptance", "after_receipt"])
def test_latched_stop_preserves_unresolved_review_protection(receipt_scenario, boundary, stop_reason):
    scenario = receipt_scenario
    store = PRGuardStore()
    client = _history_client(HEAD)
    assert assess_pr_automation(client, REPOSITORY, THREAD_PR_NUMBER).allowed
    claimed = scenario.claimed()
    if boundary == "before_acceptance":
        _latch_stop(store, THREAD_PR_NUMBER, stop_reason)
    else:
        # The receipt send has already entered; a newly observed stop must
        # prevent the next effect, the GraphQL thread resolution.
        scenario.transport.before_create = lambda: _latch_stop(store, THREAD_PR_NUMBER, stop_reason)

    report = _close_guarded_thread(scenario, _effect_authorizer(client, THREAD_PR_NUMBER), claimed)

    assert report.resolved_thread_ids == ()
    assert not scenario.transport.resolved
    assert "resolve" not in scenario.transport.effects
    assert scenario.transport.effects == ([] if boundary == "before_acceptance" else ["reply"])
    blocker = scenario.ledger().get_snapshot(API_ORIGIN, REPOSITORY, THREAD_PR_NUMBER, require_retained_state=True).get_blocker(scenario.blocker_id)
    assert blocker is not None
    assert blocker.disposition is (BlockerDisposition.OPEN if boundary == "before_acceptance" else BlockerDisposition.VERIFIED_CORRECTION)
    assert "external intervention" in repr(report)


def test_stopped_closure_can_read_back_earlier_confirmed_effects_without_new_mutations(receipt_scenario):
    scenario = receipt_scenario
    client = _history_client(HEAD)
    claimed = scenario.claimed()
    authorizer = _effect_authorizer(client, THREAD_PR_NUMBER)
    first = _close_guarded_thread(scenario, authorizer, claimed)
    assert first.resolved_thread_ids == (THREAD_ID,)
    effects = list(scenario.transport.effects)
    _latch_stop(PRGuardStore(), THREAD_PR_NUMBER, "revert")

    repeated = _close_guarded_thread(scenario, authorizer, claimed)

    assert repeated.resolved_thread_ids == (THREAD_ID,)
    assert scenario.transport.effects == effects
    assert scenario.transport.resolved


from auto_coder.github_app_reviewer import ReviewPublicationResult
from auto_coder.pr_review_cycle import PUBLICATION_ACKNOWLEDGED, ContractSnapshot, PrReviewCycleRepository, RoundProvenance, StrongPolicyIdentity
from auto_coder.two_tier_pr_gate import TwoTierPrGate


def _pending_strong_publication(tmp_path):
    contract = ContractSnapshot(("#2209",), "REQ-001: Preserve the accepted payload.")
    policy = StrongPolicyIdentity("strong-route", "options", "v1")
    cycle = PrReviewCycleRepository(REPOSITORY, tmp_path / "cycle.json")
    provenance = RoundProvenance("head-a", "base-a")
    cycle.record_ordinary_pass(42, provenance, contract)
    claim = cycle.claim_strong_audit(42, provenance, contract, policy)
    cycle.record_strong_result(42, claim.claim_id, "PASS", "reviewer/model", [])
    return cycle, pr_processor.TwoTierGateInputs(TwoTierPrGate(REPOSITORY, cycle), contract, policy, "head-a", "base-a")


def test_two_tier_native_publication_rechecks_latch_before_post(tmp_path, monkeypatch):
    cycle, inputs = _pending_strong_publication(tmp_path)
    github = _history_client("head-a")
    assert assess_pr_automation(github, REPOSITORY, 42).allowed

    class Wire(RecordingClient):
        def request(self, method, url, **kwargs):
            result = super().request(method, url, **kwargs)
            if method == "GET" and url.endswith("/pulls/42"):
                _latch_stop(PRGuardStore(), 42, "revert")
            return result

    wire = Wire(auth_responses("head-a"))
    reviewer = configured_reviewer(tmp_path, wire, monkeypatch)
    monkeypatch.setattr(pr_processor, "load_reviewer_app_config", lambda **_kwargs: object())
    monkeypatch.setattr(pr_processor, "GitHubAppReviewer", lambda _config: reviewer)

    published, _reason = pr_processor._consume_pending_two_tier_publication(REPOSITORY, 42, inputs, github_client=github)

    assert not published
    assert cycle.snapshot(42).pending_effect
    assert PRGuardStore().status(REPOSITORY, 42).stopped
    assert not any(method == "POST" and url.endswith("/reviews") for method, url, _kwargs in wire.calls)


def test_stopped_two_tier_publication_reconciles_unknown_delivery_without_resend(tmp_path, monkeypatch):
    cycle, inputs = _pending_strong_publication(tmp_path)
    github = _history_client("head-a")
    calls = []

    class Reviewer:
        def publish_exact_pr_review(self, repo, number, head, body, authorize, comments=()):
            assert authorize()
            calls.append("send")
            return ReviewPublicationResult(False, "", "response lost after review acceptance")

        def find_exact_pr_review(self, repo, number, head, body, comments=()):
            calls.append("readback")
            return ReviewPublicationResult(True, "accepted-review-receipt", "")

    monkeypatch.setattr(pr_processor, "load_reviewer_app_config", lambda **_kwargs: object())
    monkeypatch.setattr(pr_processor, "GitHubAppReviewer", lambda _config: Reviewer())
    assert not pr_processor._consume_pending_two_tier_publication(REPOSITORY, 42, inputs, github_client=github)[0]
    _latch_stop(PRGuardStore(), 42, "revert")

    published, reason = pr_processor._consume_pending_two_tier_publication(REPOSITORY, 42, inputs, github_client=github)

    assert published
    assert calls == ["send", "readback"]
    assert "accepted-review-receipt" in reason
    assert cycle.snapshot(42).accepted_strong_round.publication_status == PUBLICATION_ACKNOWLEDGED
    assert PRGuardStore().status(REPOSITORY, 42).stopped
