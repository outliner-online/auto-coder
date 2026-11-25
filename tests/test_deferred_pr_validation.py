"""Completed ordinary reviews survive unavailable acceptance observations.

The production processor, validator parser, attempt/session stores, publication
adapter, and dashboard projection run unchanged. Only the model and external
GitHub/provider boundaries are controlled by the shared joined-flow fixture.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable
from unittest.mock import patch

import pytest

from auto_coder.adversarial_validation_attempts import AdversarialValidationAttemptRepository
from auto_coder.automation_config import PRProcessingOutcome
from auto_coder.execution_trace import Outcome, TraceCollector, get_trace_collector
from auto_coder.github_request_governor import GitHubRequestDeferred
from auto_coder.util.github_request_outcome import GitHubRequestContext
from tests.test_accepted_finding_bridge import REPO, Env, env, save_empty_session  # noqa: F401
from tests.test_dashboard_observability import _mounted_detail
from tests.test_effective_decision_pr_flow import Flow, flow_env, make_flow, ordinary_response  # noqa: F401


@dataclass
class HeadEndpoint:
    """A GitHub endpoint that can become unavailable while the model runs."""

    error: Exception | None = None
    side_effect: Callable[..., str] = lambda *_args, **_kwargs: ""

    def __call__(self, *args: object, **kwargs: object) -> str:
        if self.error is not None:
            raise self.error
        return self.side_effect(*args, **kwargs)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    monkeypatch.setattr(TraceCollector, "_instance", None)
    return now


def _flow(flow_env: Env, monkeypatch: pytest.MonkeyPatch, pr: int, verdict: str = "PASS") -> tuple[Flow, HeadEndpoint]:
    flow = make_flow(flow_env, monkeypatch, pr, "cloud", saved_status=None)
    save_empty_session(flow_env, pr, flow.head)
    payload = json.loads(ordinary_response("none", status="ADDRESSED", result=verdict))
    payload["thread_dispositions"] = []
    if verdict == "NEEDS_FIX":
        payload["requirement_coverage"][0]["status"] = "VIOLATED"
        payload["findings"] = [
            {
                "requirement_id": "#2401/REQ-001",
                "finding_identity": "lost-production-state",
                "correction_identity": "preserve-both-state-values",
                "evidence_classification": "DEMONSTRATED",
                "violated_requirement": "Preserve state",
                "reachability": "src/state.py:delete_two",
                "required_behavior": "Preserve both values",
                "actual_behavior": "The second value is lost",
                "evidence": "The production entry point drops the second value",
                "counterexample": "Delete loses the second value while tests pass",
                "anchor_path": "src/state.py",
            }
        ]
    flow.model_responses = [json.dumps(payload)]
    endpoint = HeadEndpoint()
    flow.client.get_pull_request_head_sha_strict = endpoint
    return flow, endpoint


def _outage(flow: Flow, retry_at: float) -> GitHubRequestDeferred:
    context = GitHubRequestContext("head-check", "request-1", "pr-validation", "https://api.github.com", "GET", "read", "/repos/{owner}/{repo}/pulls/{number}", repository=REPO, item=str(flow.pr), strict_read=True)
    return GitHubRequestDeferred(context, "rate_limit_cooldown", retry_at)


def _attempts(flow: Flow) -> list[dict[str, object]]:
    return AdversarialValidationAttemptRepository(REPO)._read()["attempts"]


def _assert_no_effects(flow: Flow) -> None:
    assert flow.reviews == []
    assert flow.handoffs == 0
    flow.merge.assert_not_called()
    flow.client.resolve_review_thread.assert_not_called()


def _first_deferred(flow: Flow, endpoint: HeadEndpoint, retry_at: float, *, force: bool = False) -> tuple[str, int]:
    error = _outage(flow, retry_at)
    collector = get_trace_collector()
    with collector.start_execution(REPO, "pr", flow.pr, "deferred-validation-regression"):
        actions = flow.run(force=force, on_review=lambda: setattr(endpoint, "error", error))
    assert flow.model_calls == 1, actions
    _assert_no_effects(flow)
    assert flow.status.outcome is PRProcessingOutcome.DEFERRED, actions
    assert actions.retry_not_before == retry_at
    attempts = _attempts(flow)
    assert len(attempts) == 1
    return str(attempts[0]["id"]), int(attempts[0]["sequence"])


@pytest.mark.parametrize("verdict", ["PASS", "NEEDS_FIX"])
def test_rate_limited_head_check_resumes_after_restart_without_another_model(flow_env: Env, monkeypatch: pytest.MonkeyPatch, clock: list[float], verdict: str) -> None:
    flow, endpoint = _flow(flow_env, monkeypatch, 9401, verdict)
    deadline = clock[0] + 120
    attempt_id, sequence = _first_deferred(flow, endpoint, deadline)

    # A controller restart reconstructs all owners from their retained files.
    flow.env = flow.env.reconstructed()
    flow.model_responses = []  # Any accidental second model call fails immediately.
    endpoint.error = None
    waiting = flow.run()
    assert flow.model_calls == 1, waiting
    assert waiting.retry_not_before == deadline
    _assert_no_effects(flow)

    clock[0] = deadline + 1
    with get_trace_collector().start_execution(REPO, "pr", flow.pr, "restarted-validation-regression"):
        resumed = flow.run()

    assert flow.model_calls == 1, resumed
    assert [(attempt["id"], attempt["sequence"]) for attempt in _attempts(flow)] == [(attempt_id, sequence)]
    assert [review["event"] for review in flow.reviews] == ["APPROVE" if verdict == "PASS" else "REQUEST_CHANGES"], resumed
    assert flow.handoffs == (0 if verdict == "PASS" else 1), resumed
    assert flow.merge.call_count == (1 if verdict == "PASS" else 0), resumed
    events = get_trace_collector().get_snapshot(repository=REPO, item_type="pr", item_number=flow.pr).events
    restored = [event for event in events if (event.facts or {}).get("effect") == "validation-result-resumed"]
    assert len(restored) == 1
    assert restored[0].facts["attempt_id"] == attempt_id
    assert restored[0].facts["attempt_sequence"] == sequence


def test_deferred_result_reason_and_deadline_reach_the_mounted_decision_log(flow_env: Env, monkeypatch: pytest.MonkeyPatch, clock: list[float]) -> None:
    flow, endpoint = _flow(flow_env, monkeypatch, 9402)
    deadline = clock[0] + 120
    attempt_id, sequence = _first_deferred(flow, endpoint, deadline)
    events = get_trace_collector().get_snapshot(repository=REPO, item_type="pr", item_number=flow.pr).events
    deferred = [event for event in events if (event.facts or {}).get("effect") == "validation-result-retained"]
    assert len(deferred) == 1
    event = deferred[0]
    assert event.stage_id == "pr.adversarial-validation"
    assert event.outcome == Outcome.DEFERRED.value
    assert event.facts["phase"] == "gap-state-acceptance"
    assert event.facts["reason"] == "rate_limit_cooldown"
    assert event.facts["retry_not_before"] == deadline
    assert event.facts["attempt_id"] == attempt_id
    assert event.facts["attempt_sequence"] == sequence
    with patch("auto_coder.dashboard.ui") as ui:
        _mounted_detail(ui, "pr", flow.pr)
    rows = [row for call in ui.table.call_args_list for row in call.kwargs.get("rows", []) if "facts" in row]
    rendered = [row for row in rows if "validation-result-retained" in row["facts"]]
    assert rendered
    for row in rendered:
        assert row["outcome"] == "deferred"
        assert "rate_limit_cooldown" in row["facts"]
        assert "retry_not_before" in row["facts"]
        assert attempt_id in row["facts"]


@pytest.mark.parametrize("change", ["head", "base", "requirements", "policy", "newer_attempt"])
def test_changed_review_inputs_never_reuse_a_retained_result(flow_env: Env, monkeypatch: pytest.MonkeyPatch, clock: list[float], change: str) -> None:
    from auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration

    policy = LLMBackendConfiguration(backends={"reviewer": BackendConfig(name="reviewer", backend_type="codex", model="review-v1")}, backend_pr_adversarial_validation_order=["reviewer"])
    if change == "policy":
        monkeypatch.setattr("auto_coder.llm_backend_config.get_llm_config", lambda **_kwargs: policy)
    flow, endpoint = _flow(flow_env, monkeypatch, 9403)
    deadline = clock[0] + 120
    first_id, _ = _first_deferred(flow, endpoint, deadline)
    flow.env = flow.env.reconstructed()
    endpoint.error = None
    clock[0] = deadline + 1
    if change == "head":
        flow.head = "b" * 40
        flow.router.head_sha = flow.head
    elif change == "base":
        flow.env.base = flow.current_base = "c" * 40
    elif change == "requirements":
        flow.requirement_text = "Preserve state and reject a missing key."
    elif change == "policy":
        policy.backends["reviewer"].model = "review-v2"
    else:
        AdversarialValidationAttemptRepository(REPO).start(flow.pr, flow.head)

    observed_at_model: list[bool] = []
    actions = flow.run(on_review=lambda: observed_at_model.append(not flow.reviews and flow.handoffs == 0 and flow.merge.call_count == 0))

    assert flow.model_calls == 2, actions
    assert observed_at_model == [True], "A stale result emitted an effect before replacement validation."
    assert _attempts(flow)[-1]["id"] != first_id
    assert len(flow.reviews) == 1, actions


def test_unavailable_head_during_resume_preserves_result_until_a_fresh_confirmation(flow_env: Env, monkeypatch: pytest.MonkeyPatch, clock: list[float]) -> None:
    flow, endpoint = _flow(flow_env, monkeypatch, 9404)
    deadline = clock[0] + 120
    attempt_id, sequence = _first_deferred(flow, endpoint, deadline)
    flow.env = flow.env.reconstructed()
    flow.model_responses = []
    clock[0] = deadline + 1
    endpoint.error = _outage(flow, clock[0] + 90)
    still_waiting = flow.run()
    assert flow.model_calls == 1, still_waiting
    _assert_no_effects(flow)
    assert still_waiting.retry_not_before == endpoint.error.retry_at
    assert [(attempt["id"], attempt["sequence"]) for attempt in _attempts(flow)] == [(attempt_id, sequence)]

    clock[0] = endpoint.error.retry_at + 1
    endpoint.error = None
    resumed = flow.run()
    assert flow.model_calls == 1, resumed
    assert [review["event"] for review in flow.reviews] == ["APPROVE"]
    flow.merge.assert_called_once()


def test_changed_substantive_discussion_is_supplied_to_a_new_review(flow_env: Env, monkeypatch: pytest.MonkeyPatch, clock: list[float]) -> None:
    from auto_coder.review_feedback_marker import REVIEW_ADDRESSED_MARKER
    from auto_coder.util.gh_cache import ReviewThreadComment

    flow, endpoint = _flow(flow_env, monkeypatch, 9405)
    flow.production_thread_collection = True
    flow.root_body = "The production deletion path loses a value."
    flow.thread_replies = [ReviewThreadComment(database_id=flow.root_id + 1, author_id=702, author_login="implementation-agent[bot]", body=f"The correction preserves both values.\n{REVIEW_ADDRESSED_MARKER}", in_reply_to_id=flow.root_id)]
    flow.model_responses = [ordinary_response(flow.thread_id, status="ADDRESSED", evidence="tests/test_state.py covers both values")]
    deadline = clock[0] + 120
    first_id, _ = _first_deferred(flow, endpoint, deadline)
    flow.env = flow.env.reconstructed()
    clock[0] = deadline + 1
    endpoint.error = None
    new_reply = "The same production operation still drops state when both keys are absent."
    flow.thread_replies.append(ReviewThreadComment(database_id=flow.root_id + 2, author_id=703, author_login="reviewer[bot]", body=new_reply, in_reply_to_id=flow.root_id))

    actions = flow.run(on_review=lambda: _assert_no_effects(flow))

    assert flow.model_calls == 2, actions
    assert new_reply in flow.validation_sections[-1]
    assert _attempts(flow)[-1]["id"] != first_id


def test_transient_head_failure_retains_result_without_reclassifying_model_verdict(flow_env: Env, monkeypatch: pytest.MonkeyPatch, clock: list[float]) -> None:
    flow, endpoint = _flow(flow_env, monkeypatch, 9406)
    with get_trace_collector().start_execution(REPO, "pr", flow.pr, "transient-head-outage"):
        deferred = flow.run(on_review=lambda: setattr(endpoint, "error", ConnectionError("GitHub connection interrupted")))
    assert flow.model_calls == 1
    _assert_no_effects(flow)
    assert deferred.retry_not_before > clock[0]
    attempt = _attempts(flow)[0]
    events = get_trace_collector().get_snapshot(repository=REPO, item_type="pr", item_number=flow.pr).events
    event = next(event for event in events if (event.facts or {}).get("effect") == "validation-result-retained")
    assert event.outcome == Outcome.DEFERRED.value
    assert event.facts["reason"] == "head_observation_unavailable"

    flow.env = flow.env.reconstructed()
    flow.model_responses = []
    clock[0] = deferred.retry_not_before + 1
    endpoint.error = None
    resumed = flow.run()
    assert flow.model_calls == 1, resumed
    assert len(_attempts(flow)) == 1 and _attempts(flow)[0]["id"] == attempt["id"]
    assert [review["event"] for review in flow.reviews] == ["APPROVE"]


def test_corrupt_retained_store_blocks_new_review_and_effects_until_repaired(flow_env: Env, monkeypatch: pytest.MonkeyPatch, clock: list[float]) -> None:
    from auto_coder.deferred_pr_validation import DeferredPRValidationStore

    flow, endpoint = _flow(flow_env, monkeypatch, 9407)
    deadline = clock[0] + 120
    attempt_id, sequence = _first_deferred(flow, endpoint, deadline)
    store_path = DeferredPRValidationStore(REPO).storage_path
    retained_bytes = store_path.read_bytes()
    store_path.write_bytes(b"{")
    flow.env = flow.env.reconstructed()
    flow.model_responses = []
    endpoint.error = None
    clock[0] = deadline + 1

    unavailable = flow.run()

    assert flow.model_calls == 1, unavailable
    _assert_no_effects(flow)
    assert [(attempt["id"], attempt["sequence"]) for attempt in _attempts(flow)] == [(attempt_id, sequence)]
    assert store_path.read_bytes() == b"{", "Unreadable retained evidence must not be overwritten as absent."
    store_path.write_bytes(retained_bytes)
    clock[0] = max(clock[0], unavailable.retry_not_before or clock[0]) + 1
    resumed = flow.run()
    assert flow.model_calls == 1, resumed
    assert [review["event"] for review in flow.reviews] == ["APPROVE"]


def test_unavailable_current_review_context_cannot_replace_retained_evidence(flow_env: Env, monkeypatch: pytest.MonkeyPatch, clock: list[float]) -> None:
    from unittest.mock import PropertyMock

    flow, endpoint = _flow(flow_env, monkeypatch, 9408)
    deadline = clock[0] + 120
    attempt_id, sequence = _first_deferred(flow, endpoint, deadline)
    flow.env = flow.env.reconstructed()
    flow.model_responses = []
    endpoint.error = None
    clock[0] = deadline + 1
    # The shared context producer cannot observe its current requirement text.
    # This is unavailable evidence, not a positively observed changed contract.
    with patch.object(Flow, "requirement_text", new_callable=PropertyMock, side_effect=ConnectionError("current requirement evidence unavailable")):
        unavailable = flow.run()
    assert flow.model_calls == 1, unavailable
    _assert_no_effects(flow)
    assert [(attempt["id"], attempt["sequence"]) for attempt in _attempts(flow)] == [(attempt_id, sequence)]
    assert unavailable.retry_not_before > clock[0]

    clock[0] = unavailable.retry_not_before + 1
    resumed = flow.run()
    assert flow.model_calls == 1, resumed
    assert [review["event"] for review in flow.reviews] == ["APPROVE"]


@pytest.mark.parametrize("read_failure", ["exception", "incomplete"])
def test_unavailable_accepted_finding_projection_cannot_replace_retained_verdict(flow_env: Env, monkeypatch: pytest.MonkeyPatch, clock: list[float], read_failure: str) -> None:
    from auto_coder.accepted_finding_bridge import AcceptedFindingProjection, BridgeDiagnostic
    from auto_coder.deferred_pr_validation import DeferredPRValidationStore

    flow, endpoint = _flow(flow_env, monkeypatch, 9409, "NEEDS_FIX")
    deadline = clock[0] + 120
    attempt_id, sequence = _first_deferred(flow, endpoint, deadline)
    retained = DeferredPRValidationStore(REPO).load(flow.pr)
    assert retained is not None
    assert json.loads(retained.result_payload)["result"] == "NEEDS_FIX"
    flow.env = flow.env.reconstructed()
    flow.model_responses = []
    endpoint.error = None
    clock[0] = deadline + 1

    # Input identity already matches; only rebuilding current authority fails.
    unreadable = AcceptedFindingProjection(diagnostics=(BridgeDiagnostic("source_unavailable", "", "accepted finding projection unavailable"),))
    error = ConnectionError("accepted finding projection unavailable") if read_failure == "exception" else None
    with patch("auto_coder.pr_processor.project_accepted_findings", side_effect=error, return_value=unreadable) as projection:
        unavailable = flow.run()
        projection.assert_called_once()
    assert flow.model_calls == 1, unavailable
    _assert_no_effects(flow)
    assert [(attempt["id"], attempt["sequence"]) for attempt in _attempts(flow)] == [(attempt_id, sequence)]
    after_failure = DeferredPRValidationStore(REPO).load(flow.pr)
    assert after_failure is not None
    assert after_failure.result_payload == retained.result_payload
    assert unavailable.retry_not_before > clock[0]

    clock[0] = unavailable.retry_not_before + 1
    resumed = flow.run()
    assert flow.model_calls == 1, resumed
    assert [review["event"] for review in flow.reviews] == ["REQUEST_CHANGES"]
    assert flow.handoffs == 1
    flow.merge.assert_not_called()


def test_replay_of_retained_needs_fix_bypasses_the_new_review_cap(flow_env: Env, monkeypatch: pytest.MonkeyPatch, clock: list[float]) -> None:
    monkeypatch.setenv("AUTO_CODER_MAX_ADVERSARIAL_VALIDATIONS", "0")
    flow, endpoint = _flow(flow_env, monkeypatch, 9410, "NEEDS_FIX")
    deadline = clock[0] + 120
    attempt_id, sequence = _first_deferred(flow, endpoint, deadline, force=True)
    flow.env = flow.env.reconstructed()
    flow.model_responses = []
    endpoint.error = None

    waiting = flow.run()
    assert waiting.retry_not_before == deadline
    assert flow.model_calls == 1
    _assert_no_effects(flow)

    clock[0] = deadline + 1
    resumed = flow.run()
    assert flow.model_calls == 1, resumed
    assert [(attempt["id"], attempt["sequence"]) for attempt in _attempts(flow)] == [(attempt_id, sequence)]
    assert [review["event"] for review in flow.reviews] == ["REQUEST_CHANGES"], resumed
    assert flow.handoffs == 1
    flow.merge.assert_not_called()


def test_still_valid_evidence_written_by_the_review_does_not_invalidate_its_retained_result(flow_env: Env, monkeypatch: pytest.MonkeyPatch, clock: list[float]) -> None:
    flow = make_flow(flow_env, monkeypatch, 9411, "cloud")
    flow.accept_finding(gap=False)
    flow.model_responses = [ordinary_response(flow.thread_id, status="STILL_VALID")]
    endpoint = HeadEndpoint()
    flow.client.get_pull_request_head_sha_strict = endpoint
    before = flow.env.ledger.get_snapshot("https://api.github.com", REPO, flow.pr)
    deadline = clock[0] + 120
    attempt_id, sequence = _first_deferred(flow, endpoint, deadline)
    after = flow.env.ledger.get_snapshot("https://api.github.com", REPO, flow.pr)
    assert after.ledger_revision > before.ledger_revision

    flow.env = flow.env.reconstructed()
    flow.model_responses = []
    endpoint.error = None
    clock[0] = deadline + 1
    resumed = flow.run()

    assert flow.model_calls == 1, resumed
    assert [(attempt["id"], attempt["sequence"]) for attempt in _attempts(flow)] == [(attempt_id, sequence)]
    assert [review["event"] for review in flow.reviews] == ["REQUEST_CHANGES"], resumed
    assert flow.handoffs == 1
    flow.merge.assert_not_called()
    retained = flow.retained()
    assert retained is not None and retained.status == "NEEDS_FIX"


@pytest.mark.parametrize("verdict", ["PASS", "NEEDS_FIX"])
def test_native_publication_before_process_crash_is_not_posted_again(flow_env: Env, monkeypatch: pytest.MonkeyPatch, clock: list[float], verdict: str) -> None:
    from auto_coder.deferred_pr_validation import DeferredPRValidationStore
    from tests.test_effective_decision_pr_flow import SimulatedCrash

    flow, endpoint = _flow(flow_env, monkeypatch, 9412, verdict)
    flow.auto_status = True
    assert flow.router is not None
    # The read boundary exposes the review actually accepted by the transport.
    flow.client.get_pr_reviews_strict.side_effect = lambda *_args, **_kwargs: list(flow.router.review_records)
    deadline = clock[0] + 120
    attempt_id, sequence = _first_deferred(flow, endpoint, deadline)
    flow.env = flow.env.reconstructed()
    flow.model_responses = []
    endpoint.error = None
    clock[0] = deadline + 1

    with patch.object(AdversarialValidationAttemptRepository, "mark_published", side_effect=SimulatedCrash) as mark:
        with pytest.raises(SimulatedCrash):
            flow.run()
        mark.assert_called_once_with(attempt_id)
    expected_event = "APPROVE" if verdict == "PASS" else "REQUEST_CHANGES"
    assert [review["event"] for review in flow.reviews] == [expected_event]
    assert flow.handoffs == 0
    flow.merge.assert_not_called()
    original = _attempts(flow)[0]
    assert original["id"] == attempt_id and not original.get("published")
    assert DeferredPRValidationStore(REPO).load(flow.pr) is not None

    flow.env = flow.env.reconstructed()
    native_outage = _outage(flow, clock[0] + 90)
    flow.client.get_pr_reviews_strict.side_effect = native_outage
    unavailable = flow.run()
    assert unavailable.retry_not_before == native_outage.retry_at
    assert flow.model_calls == 1
    assert [review["event"] for review in flow.reviews] == [expected_event]
    assert flow.handoffs == 0
    flow.merge.assert_not_called()
    assert not _attempts(flow)[0].get("published")
    assert DeferredPRValidationStore(REPO).load(flow.pr) is not None

    flow.env = flow.env.reconstructed()
    flow.client.get_pr_reviews_strict.side_effect = lambda *_args, **_kwargs: list(flow.router.review_records)
    clock[0] = native_outage.retry_at + 1
    resumed = flow.run()

    assert flow.model_calls == 1, resumed
    assert [review["event"] for review in flow.reviews] == [expected_event], resumed
    assert [(attempt["id"], attempt["sequence"]) for attempt in _attempts(flow)] == [(attempt_id, sequence)]
    assert _attempts(flow)[0]["published"] is True
    assert DeferredPRValidationStore(REPO).load(flow.pr) is None
    if verdict == "NEEDS_FIX":
        flow.merge.assert_not_called()
