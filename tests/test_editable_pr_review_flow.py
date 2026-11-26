"""Editable review through the actual PR processor, Git worktrees and publisher.

Only external reviewer, GitHub and CI/merge endpoints are controlled. Candidate
comparison, attempt state, session invalidation and commit/push use production
code against a real local bare repository.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable
from unittest.mock import Mock

import pytest

from auto_coder import pr_processor
from auto_coder.automation_config import ProcessedPRResult, PRProcessingOutcome
from auto_coder.cli_helpers import AdversarialValidationAvailability
from auto_coder.controller_git_identity import ControllerGitIdentity, ControllerGitIdentityError
from auto_coder.execution_trace import get_trace_collector
from auto_coder.github_app_reviewer import ReviewPublicationResult
from auto_coder.reviewer_session_registry import ReviewerSession, ReviewerSessionRegistry
from auto_coder.utils import CommandExecutor
from tests.ci_observation_fakes import authoritative_ci_status
from tests.test_pr_adversarial_review_audit import (  # noqa: F401 (audit_store is a shared fixture)
    PASS_PAYLOAD,
    PR_NUMBER,
    REPO_NAME,
    MockReviewerClient,
    _apply_standard_merge_gates,
    _build_backend_manager,
    _build_config,
    _build_github_client,
    _build_pr_data,
    _build_pr_repo,
    _git,
    audit_store,
)


@dataclass
class ReviewFlow:
    repository: Path
    remote: Path
    initial_head: str
    base: str
    client: object
    config: object
    reviewer: MockReviewerClient
    publication: Mock
    merge: Mock
    seen: list[tuple[str, str, Path]] = field(default_factory=list)
    last_status: ProcessedPRResult | None = None

    def head(self) -> str:
        return _git(["rev-parse", "refs/heads/feature-branch"], self.remote)

    def metadata(self) -> dict:
        metadata = _build_pr_data(self.head(), base_sha=self.base)
        metadata["head"]["repo"] = {"full_name": REPO_NAME}
        return metadata

    def run(self):
        # GitHub exposes the updated branch through this ref after a push.
        _git(["update-ref", f"refs/pull/{PR_NUMBER}/head", self.head()], self.remote)
        metadata = self.metadata()
        self.last_status = ProcessedPRResult(metadata)
        with get_trace_collector().start_execution(REPO_NAME, "pr", PR_NUMBER, origin="worker"):
            return pr_processor._handle_pr_merge(self.client, REPO_NAME, metadata, self.config, {}, self.last_status)


def _caller_snapshot(repository: Path) -> tuple:
    return (
        _git(["rev-parse", "HEAD"], repository),
        _git(["symbolic-ref", "HEAD"], repository),
        _git(["status", "--porcelain"], repository),
        _git(["diff", "--binary"], repository),
        _git(["diff", "--cached", "--binary"], repository),
        (repository / "untracked.txt").read_bytes(),
    )


def _correction_events(after_sequence: int):
    return [
        event
        for event in get_trace_collector().get_snapshot(repository=REPO_NAME, item_type="pr", item_number=PR_NUMBER).events
        if event.sequence > after_sequence and event.stage_id == "pr.adversarial-validation" and (event.facts or {}).get("effect") in {"reviewer-source-changes", "reviewer-publication-resumed"}
    ]


def _edit_source(root: Path) -> None:
    source = root / "sample.py"
    source.write_text(source.read_text() + "\n# Reviewed greeting implementation.\n", encoding="utf-8")


def _write_runtime_outputs(root: Path) -> None:
    for directory in (".agent-tmp", ".pytest_cache", "test-results"):
        output = root / directory / "review-output.txt"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("runtime evidence\n", encoding="utf-8")


@pytest.fixture
def flow(tmp_path, monkeypatch, _use_real_commands, audit_store, request):
    repository, head = _build_pr_repo(tmp_path)
    if getattr(request, "param", "") in {"dynamic", "interrupted-dynamic"}:
        (repository / "tests").mkdir()
        (repository / "tests" / "test_sample.py").write_text("def test_greeting():\n    assert True\n", encoding="utf-8")
        (repository / "scripts").mkdir()
        script = "#!/bin/bash\nset -e\nprintf '\\n# Source changed during dynamic verification.\\n' >> sample.py\n"
        if request.param == "interrupted-dynamic":
            script += "kill -TERM $$\n"
        (repository / "scripts" / "test.sh").write_text(script, encoding="utf-8")
        _git(["add", "tests", "scripts"], repository)
        _git(["commit", "-m", "add focused dynamic verification"], repository)
        head = _git(["rev-parse", "HEAD"], repository)
    base = _git(["rev-parse", f"{head}^"], repository)
    remote = tmp_path / "remote.git"
    _git(["clone", "--bare", str(repository), str(remote)], tmp_path)
    _git(["update-ref", "refs/heads/feature-branch", head], remote)
    _git(["update-ref", f"refs/pull/{PR_NUMBER}/head", head], remote)
    _git(["remote", "set-url", "origin", "https://github.com/owner/repo.git"], repository)
    _git(["config", f"url.{remote}.insteadOf", "https://github.com/owner/repo.git"], repository)
    monkeypatch.setenv("AUTO_CODER_WORKSPACE_ROOT", str(repository))
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(tmp_path / "runtime"))
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)

    _apply_standard_merge_gates(monkeypatch, merge_result=True)
    monkeypatch.setattr(pr_processor, "resolve_controller_git_identity", lambda repository: ControllerGitIdentity(repository, "controller[bot]", 98765, 123))
    merge = Mock(return_value=True)
    publication = Mock(return_value=ReviewPublicationResult(True, "APPROVE", ""))
    monkeypatch.setattr(pr_processor, "_merge_pr", merge)
    monkeypatch.setattr(pr_processor, "publish_adversarial_review", publication)
    client = _build_github_client(head, repo=repository, base_sha=base)
    config = _build_config()
    seen: list[tuple[str, str, Path]] = []

    class Reviewer(MockReviewerClient):
        mutation: Callable[[Path], None] | None = None
        fail_after_edit = False

        def _response(self, mode: str, is_noedit: bool) -> str:
            assert is_noedit is False
            root = Path(CommandExecutor.run_command(["git", "rev-parse", "--show-toplevel"]).stdout.strip())
            examined = _git(["rev-parse", "HEAD"], root)
            seen.append((mode, examined, root))
            assert root != repository
            if self.mutation is not None:
                mutation, self.mutation = self.mutation, None
                mutation(root)
            if self.fail_after_edit:
                raise RuntimeError("reviewer interrupted before returning a result")
            self.calls.append(mode)
            return self._next()

        def _run_llm_cli(self, prompt, is_noedit=False):
            return self._response("fresh", is_noedit)

        def continue_session(self, session_id, prompt, is_noedit=False):
            return self._response("continue", is_noedit)

    reviewer = Reviewer("reviewer", responses=[PASS_PAYLOAD, PASS_PAYLOAD], session_id="review-provider-session")
    manager = _build_backend_manager(monkeypatch, {"reviewer": reviewer}, "reviewer")
    monkeypatch.setattr("auto_coder.cli_helpers.resolve_adversarial_validation_availability", lambda validation_kind=None, execution_cwd=None: AdversarialValidationAvailability(backend_manager=manager))
    scenario = ReviewFlow(repository, remote, head, base, client, config, reviewer, publication, merge, seen)
    client.get_pull_request.side_effect = lambda *_args, **_kwargs: scenario.metadata()
    client.get_pull_request_metadata_strict.side_effect = lambda *_args, **_kwargs: scenario.metadata()
    client.get_pull_request_head_sha_strict.side_effect = lambda *_args, **_kwargs: scenario.head()
    client.get_pr_diff.side_effect = lambda *_args, **_kwargs: _git(["diff", "--binary", base, scenario.head()], repository)

    (repository / "sample.py").write_text("staged caller changes\n", encoding="utf-8")
    _git(["add", "sample.py"], repository)
    (repository / "sample.py").write_text("unstaged caller changes\n", encoding="utf-8")
    (repository / "untracked.txt").write_text("preserved caller content\n", encoding="utf-8")
    return scenario


@pytest.mark.parametrize("editing_response", [PASS_PAYLOAD, "completed source correction without valid review JSON"])
def test_review_fix_is_published_without_self_approval_then_independently_reviewed(flow, monkeypatch, editing_response):
    registry = ReviewerSessionRegistry()
    retained = ReviewerSession(REPO_NAME, PR_NUMBER, "reviewer", "codex-cloud", "reviewer-model", "prior-review-session", flow.initial_head)
    registry.save(retained)
    before = _caller_snapshot(flow.repository)
    flow.reviewer.mutation = _edit_source
    flow.reviewer._responses = [editing_response, PASS_PAYLOAD]
    trace_start = get_trace_collector().get_snapshot().sequence_high_watermark

    actions = flow.run()

    changed_head = flow.head()
    assert changed_head != flow.initial_head, actions
    assert _git(["rev-parse", f"{changed_head}^"], flow.remote) == flow.initial_head
    assert "# Reviewed greeting implementation." in _git(["show", f"{changed_head}:sample.py"], flow.remote)
    assert flow.reviewer.calls == ["continue"]
    flow.publication.assert_not_called()
    flow.merge.assert_not_called()
    assert _caller_snapshot(flow.repository) == before
    assert all(not root.exists() for _, _, root in flow.seen)
    retired = registry.get(REPO_NAME, PR_NUMBER, "reviewer", "codex-cloud", "reviewer-model")
    assert retired is not None and retired.session_id == "" and retired.last_head_sha == flow.initial_head
    events = _correction_events(trace_start)
    assert len(events) == 1
    assert events[0].outcome == "deferred"
    assert events[0].facts["effect"] == "reviewer-source-changes"
    assert events[0].facts["examined_head"] == flow.initial_head
    assert events[0].facts["local_phase"] == "awaiting_validation"
    assert events[0].facts["published"] is True
    assert events[0].facts["review_disposition"] == "CORRECTIVE_HANDOFF"
    assert actions.retry_not_before == events[0].facts["retry_not_before"]
    assert flow.last_status.outcome is PRProcessingOutcome.DEFERRED
    assert flow.last_status.retry_not_before == actions.retry_not_before
    assert time.time() < actions.retry_not_before <= time.time() + 60.0

    monkeypatch.setattr(pr_processor, "_check_github_actions_status", authoritative_ci_status(success=False, ids=[2], in_progress=True))
    flow.run()
    assert flow.reviewer.calls == ["continue"]
    flow.merge.assert_not_called()
    monkeypatch.setattr(pr_processor, "_check_github_actions_status", authoritative_ci_status(success=True, ids=[3]))

    new_actions = flow.run()

    assert flow.reviewer.calls == ["continue", "fresh"], new_actions
    assert [head for _, head, _ in flow.seen] == [flow.initial_head, changed_head]
    flow.publication.assert_called_once()
    flow.merge.assert_called_once()
    assert _caller_snapshot(flow.repository) == before


def test_only_disposable_runtime_output_keeps_original_review_candidate(flow):
    flow.reviewer.mutation = _write_runtime_outputs
    before = _caller_snapshot(flow.repository)

    actions = flow.run()

    assert flow.head() == flow.initial_head, actions
    flow.publication.assert_called_once()
    flow.merge.assert_called_once()
    assert _caller_snapshot(flow.repository) == before
    assert all(not root.exists() for _, _, root in flow.seen)


@pytest.mark.parametrize("flow", ["dynamic"], indirect=True)
def test_dynamic_check_source_mutation_cannot_certify_unchanged_head(flow):
    requested = json.loads(PASS_PAYLOAD)
    requested["dynamic_check_requested"] = "tests/test_sample.py"
    flow.reviewer._responses = [json.dumps(requested)]

    actions = flow.run()

    assert flow.head() != flow.initial_head, actions
    assert "Source changed during dynamic verification." in _git(["show", f"{flow.head()}:sample.py"], flow.remote)
    assert flow.reviewer.calls == ["fresh"]
    flow.publication.assert_not_called()
    flow.merge.assert_not_called()


def test_interrupted_editing_reviewer_neither_publishes_nor_approves(flow):
    before = _caller_snapshot(flow.repository)
    flow.reviewer.mutation = _edit_source
    flow.reviewer.fail_after_edit = True

    actions = flow.run()

    assert flow.head() == flow.initial_head, actions
    flow.publication.assert_called_once()
    assert flow.publication.call_args.args[3].result == "ERROR"
    flow.merge.assert_not_called()
    assert _caller_snapshot(flow.repository) == before
    assert all(not root.exists() for _, _, root in flow.seen)


def test_strong_audit_fixes_are_published_without_accepting_its_own_pass(flow):
    from auto_coder.pr_review_cycle import ContractSnapshot, StrongPolicyIdentity
    from auto_coder.two_tier_pr_gate import TwoTierPrGate

    inputs = pr_processor.TwoTierGateInputs(
        gate=TwoTierPrGate(REPO_NAME),
        contract=ContractSnapshot(("#99",), "Issue #99 REQ-001: greet() returns hello."),
        policy=StrongPolicyIdentity("strong-review", "model=strong", "v1"),
        head_sha=flow.initial_head,
        base_sha=flow.base,
        head_repository=REPO_NAME,
        head_ref="feature-branch",
    )
    inputs.gate.ordinary_pass(PR_NUMBER, flow.initial_head, flow.base, inputs.contract)
    before = _caller_snapshot(flow.repository)
    flow.reviewer.mutation = _edit_source

    accepted, reason = pr_processor._execute_pending_strong_audit(
        REPO_NAME,
        PR_NUMBER,
        inputs,
        publication_authority=lambda sha, ref: flow.head() == sha and ref == "feature-branch",
    )

    assert not accepted, reason
    assert flow.head() != flow.initial_head, reason
    assert "independent" in reason
    assert flow.reviewer.calls == ["fresh"]
    snapshot = inputs.gate.state.snapshot(PR_NUMBER)
    assert snapshot.accepted_strong_round is None
    assert snapshot.active_claim is None
    assert snapshot.completion is None
    flow.publication.assert_not_called()
    flow.merge.assert_not_called()
    assert _caller_snapshot(flow.repository) == before


def _seed_stale_strong_finding(flow, monkeypatch):
    """A published finding at the old head, followed by a repair and policy change."""
    from auto_coder.accepted_finding_bridge import ObservedRoot, RootObservation
    from auto_coder.llm_backend_config import LLMBackendConfiguration
    from auto_coder.pr_review_cycle import Finding, RoundProvenance
    from auto_coder.pr_review_effects import AcceptedReviewPayload

    config = LLMBackendConfiguration(backend_strong_pr_adversarial_validation_order=["reviewer"])
    monkeypatch.setattr("auto_coder.llm_backend_config.get_llm_config", lambda *args, **kwargs: config)
    inputs = pr_processor._two_tier_gate_inputs(flow.client, REPO_NAME, flow.metadata())
    assert inputs is not None
    old_policy = replace(inputs.policy, model_options="previous strong policy")
    inputs.gate.ordinary_pass(PR_NUMBER, flow.base, flow.base, inputs.contract)
    claim = inputs.gate.state.claim_strong_audit(PR_NUMBER, RoundProvenance(flow.base, flow.base), inputs.contract, old_policy)
    finding = Finding(
        finding_id="retained-greeting",
        origin_round_id=claim.claim_id,
        requirement_ids=("#99/REQ-001",),
        requirement_texts=("greet() returns the string hello.",),
        counterexample="greet() returns placeholder at the audited head.",
        expected_behavior="greet() returns hello.",
        actual_behavior="greet() returns placeholder.",
        evidence="sample.py:2",
        affected_boundary="sample.py:greet",
        focused_regression_scenario="Call greet() and assert its returned greeting.",
    )
    strong = inputs.gate.state.record_strong_result(PR_NUMBER, claim.claim_id, "FINDINGS", "reviewer/previous-model", [finding])
    inputs.gate.state.acknowledge_publication(PR_NUMBER, strong.round_id)
    strong = inputs.gate.state.snapshot(PR_NUMBER).accepted_strong_round
    assert strong is not None
    payload = AcceptedReviewPayload.strong(REPO_NAME, PR_NUMBER, strong, (finding,))
    transport = pr_processor._GitHubReviewEffectTransport(Mock(), payload, lambda: True)
    bridge, target = pr_processor._accepted_state_inputs(REPO_NAME, flow.metadata(), flow.initial_head)
    projection = bridge.project(target, RootObservation((ObservedRoot(9001, transport.comments[0].body, True, "PRRT_retained"),), complete=True))
    assert projection.complete
    assert projection.records[0].root_comment_ids == (9001,)
    return inputs, strong, config


def _edit_on_renewed_strong(flow, mutation):
    def after_ordinary(_root):
        assert len(flow.seen) == 1
        flow.reviewer.mutation = mutation

    flow.reviewer.mutation = after_ordinary


def test_renewed_strong_fix_is_published_without_closing_retained_findings(flow, monkeypatch):
    inputs, strong, _config = _seed_stale_strong_finding(flow, monkeypatch)
    before = _caller_snapshot(flow.repository)
    _edit_on_renewed_strong(flow, _edit_source)
    trace_start = get_trace_collector().get_snapshot().sequence_high_watermark

    actions = flow.run()

    changed_head = flow.head()
    assert changed_head != flow.initial_head, actions
    assert _git(["rev-parse", f"{changed_head}^"], flow.remote) == flow.initial_head
    assert "# Reviewed greeting implementation." in _git(["show", f"{changed_head}:sample.py"], flow.remote)
    assert flow.reviewer.calls == ["fresh", "fresh"]
    snapshot = inputs.gate.state.snapshot(PR_NUMBER)
    assert snapshot.accepted_strong_round == strong
    assert [finding.finding_id for finding in snapshot.open_findings] == ["retained-greeting"]
    assert snapshot.active_claim is None
    assert snapshot.accepted_closure is None
    assert snapshot.completion is None
    flow.publication.assert_not_called()
    flow.merge.assert_not_called()
    assert _caller_snapshot(flow.repository) == before
    assert all(not root.exists() for _, _, root in flow.seen)
    events = [event for event in get_trace_collector().get_snapshot().events if event.sequence > trace_start and event.stage_id == "pr.strong-audit"]
    assert len(events) == 1
    assert events[0].outcome == "deferred"
    assert events[0].facts["renewed_strong_required"] is True
    assert "source changes" in events[0].facts["reason"]
    assert "independent" in events[0].facts["reason"]
    corrections = _correction_events(trace_start)
    assert len(corrections) == 1
    assert corrections[0].facts["local_phase"] == "awaiting_validation"
    assert corrections[0].facts["published"] is True
    assert corrections[0].facts["review_disposition"] == "CORRECTIVE_HANDOFF"
    assert actions.retry_not_before == corrections[0].facts["retry_not_before"]
    assert flow.last_status.outcome is PRProcessingOutcome.DEFERRED
    assert flow.last_status.retry_not_before == actions.retry_not_before
    assert time.time() < actions.retry_not_before <= time.time() + 60.0


@pytest.mark.parametrize("changed_target", ["head", "base", "contract", "policy"])
def test_renewed_strong_fix_refuses_publication_when_target_changes(flow, monkeypatch, changed_target):
    inputs, strong, config = _seed_stale_strong_finding(flow, monkeypatch)
    before = _caller_snapshot(flow.repository)

    def edit_and_change_target(root):
        _edit_source(root)
        if changed_target == "head":
            _git(["update-ref", "refs/heads/feature-branch", flow.base], flow.remote)
        elif changed_target == "base":
            flow.base = flow.initial_head
        elif changed_target == "contract":
            issue = flow.client.get_issue.return_value
            flow.client.get_issue.return_value = {**issue, "body": issue["body"].replace("string hello", "string hello world")}
        else:
            config.backend_strong_pr_adversarial_validation_order = ["reviewer", "new-policy-reviewer"]

    _edit_on_renewed_strong(flow, edit_and_change_target)
    trace_start = get_trace_collector().get_snapshot().sequence_high_watermark

    actions = flow.run()

    assert flow.head() == (flow.base if changed_target == "head" else flow.initial_head), actions
    assert flow.reviewer.calls == ["fresh", "fresh"]
    snapshot = inputs.gate.state.snapshot(PR_NUMBER)
    assert snapshot.accepted_strong_round == strong
    assert [finding.finding_id for finding in snapshot.open_findings] == ["retained-greeting"]
    assert snapshot.active_claim is None
    assert snapshot.accepted_closure is None
    assert snapshot.completion is None
    assert "provider-entry authority changed during preparation" in snapshot.attempt_error_reason
    flow.publication.assert_not_called()
    flow.merge.assert_not_called()
    assert _caller_snapshot(flow.repository) == before
    assert all(not root.exists() for _, _, root in flow.seen)
    events = [event for event in get_trace_collector().get_snapshot().events if event.sequence > trace_start and event.stage_id == "pr.strong-audit"]
    assert len(events) == 1
    assert events[0].outcome == "deferred"
    assert events[0].facts["renewed_strong_required"] is True
    assert "provider-entry authority changed during preparation" in events[0].facts["reason"]


def test_renewed_strong_pending_publication_retries_without_another_reviewer(flow, monkeypatch):
    from auto_coder import local_review_repair
    from auto_coder.utils import CommandResult

    inputs, strong, _config = _seed_stale_strong_finding(flow, monkeypatch)
    before = _caller_snapshot(flow.repository)
    _edit_on_renewed_strong(flow, _edit_source)
    original_push = local_review_repair.git_push
    offline_push = Mock(return_value=CommandResult(False, "", "remote temporarily unavailable", 1))
    monkeypatch.setattr(local_review_repair, "git_push", offline_push)
    trace_start = get_trace_collector().get_snapshot().sequence_high_watermark

    first_actions = flow.run()

    assert flow.head() == flow.initial_head, first_actions
    assert flow.reviewer.calls == ["fresh", "fresh"]
    offline_push.assert_called_once()
    store = local_review_repair.LocalReviewRepairStore(local_review_repair.local_review_repair_db_path(REPO_NAME))
    request = local_review_repair.LocalReviewRepairRequest(REPO_NAME, PR_NUMBER, REPO_NAME, "feature-branch", flow.initial_head, (), "")
    retained = store.get_publication_pending(request)
    assert retained is not None
    assert retained.result_sha != flow.initial_head
    assert _git(["rev-parse", f"{retained.result_sha}^"], flow.repository) == flow.initial_head
    assert "# Reviewed greeting implementation." in _git(["show", f"{retained.result_sha}:sample.py"], flow.repository)
    assert first_actions.retry_not_before is not None
    assert time.time() < first_actions.retry_not_before <= time.time() + 60.0
    assert flow.last_status.outcome is PRProcessingOutcome.DEFERRED
    assert flow.last_status.retry_not_before == first_actions.retry_not_before
    events = _correction_events(trace_start)
    assert len(events) == 1
    assert events[0].outcome == "deferred"
    assert events[0].facts["effect"] == "reviewer-source-changes"
    assert events[0].facts["local_phase"] == "publication_pending"
    assert events[0].facts["published"] is False
    assert events[0].facts["review_disposition"] == "RECONCILIATION_WAIT"
    assert events[0].facts["retry_not_before"] == first_actions.retry_not_before

    monkeypatch.setattr(local_review_repair, "git_push", original_push)
    second_actions = flow.run()

    assert flow.head() == retained.result_sha, second_actions
    assert flow.reviewer.calls == ["fresh", "fresh"]
    resumed = store.get(request, retained.attempt_id)
    assert resumed is not None and resumed.phase == "awaiting_validation"
    assert resumed.result_sha == retained.result_sha
    events = _correction_events(trace_start)
    assert len(events) == 2
    assert events[1].outcome == "deferred"
    assert events[1].facts["effect"] == "reviewer-publication-resumed"
    assert events[1].facts["local_phase"] == "awaiting_validation"
    assert events[1].facts["published"] is True
    assert events[1].facts["review_disposition"] == "CORRECTIVE_HANDOFF"
    assert second_actions.retry_not_before == events[1].facts["retry_not_before"]
    assert flow.last_status.retry_not_before == second_actions.retry_not_before
    snapshot = inputs.gate.state.snapshot(PR_NUMBER)
    assert snapshot.accepted_strong_round == strong
    assert [finding.finding_id for finding in snapshot.open_findings] == ["retained-greeting"]
    assert snapshot.active_claim is None
    assert snapshot.accepted_closure is None
    assert snapshot.completion is None
    flow.publication.assert_not_called()
    flow.merge.assert_not_called()
    assert _caller_snapshot(flow.repository) == before


@pytest.mark.parametrize("flow", ["interrupted-dynamic"], indirect=True)
def test_interrupted_dynamic_check_cannot_publish_its_partial_source(flow):
    requested = json.loads(PASS_PAYLOAD)
    requested["dynamic_check_requested"] = "tests/test_sample.py"
    flow.reviewer._responses = [json.dumps(requested)]
    before = _caller_snapshot(flow.repository)

    actions = flow.run()

    assert flow.head() == flow.initial_head, actions
    assert flow.reviewer.calls == ["fresh"]
    flow.publication.assert_called_once()
    assert flow.publication.call_args.args[3].result == "ERROR"
    flow.merge.assert_not_called()
    assert _caller_snapshot(flow.repository) == before


def test_accepted_push_with_lost_acknowledgement_resumes_without_another_reviewer(flow, monkeypatch):
    from auto_coder import local_review_repair
    from auto_coder.utils import CommandResult

    original_push = local_review_repair.git_push
    pushes: list[str] = []

    def lose_push_acknowledgement(**kwargs):
        result = original_push(**kwargs)
        assert result.success, result.stderr
        pushes.append(flow.head())
        return CommandResult(False, result.stdout, "push acknowledgement was interrupted", 1)

    monkeypatch.setattr(local_review_repair, "git_push", lose_push_acknowledgement)
    flow.reviewer.mutation = _edit_source
    trace_start = get_trace_collector().get_snapshot().sequence_high_watermark

    first_actions = flow.run()
    published_head = flow.head()
    assert published_head != flow.initial_head, first_actions
    assert flow.reviewer.calls == ["fresh"]
    flow.publication.assert_not_called()
    flow.merge.assert_not_called()
    initial_events = _correction_events(trace_start)
    assert len(initial_events) == 1
    assert initial_events[0].outcome == "deferred"
    assert initial_events[0].facts["local_phase"] == "publication_pending"
    assert initial_events[0].facts["published"] is False
    assert initial_events[0].facts["review_disposition"] == "RECONCILIATION_WAIT"
    assert first_actions.retry_not_before == initial_events[0].facts["retry_not_before"]
    assert flow.last_status.outcome is PRProcessingOutcome.DEFERRED
    assert flow.last_status.retry_not_before == first_actions.retry_not_before
    assert time.time() < first_actions.retry_not_before <= time.time() + 60.0

    second_actions = flow.run()

    assert flow.head() == published_head, second_actions
    assert flow.reviewer.calls == ["fresh"]
    assert pushes == [published_head]
    flow.publication.assert_not_called()
    flow.merge.assert_not_called()
    events = _correction_events(trace_start)
    assert len(events) == 2
    resumed = events[1]
    assert resumed.outcome == "deferred"
    assert resumed.facts["effect"] == "reviewer-publication-resumed"
    assert resumed.facts["examined_head"] == published_head
    assert resumed.facts["local_phase"] == "awaiting_validation"
    assert resumed.facts["published"] is True
    assert resumed.facts["review_disposition"] == "CORRECTIVE_HANDOFF"
    assert second_actions.retry_not_before == resumed.facts["retry_not_before"]
    assert flow.last_status.retry_not_before == second_actions.retry_not_before


def test_legacy_retained_correction_supersession_still_requires_new_ci_and_independent_review(flow, tmp_path, monkeypatch):
    from auto_coder import editable_review, local_review_repair
    from auto_coder.local_review_repair import LocalReviewRepairRequest, LocalReviewRepairStore, local_review_repair_db_path
    from tests.test_review_supersession import missing_git_identity, retain_legacy_shape

    before = _caller_snapshot(flow.repository)
    flow.reviewer.mutation = _edit_source
    original_commit = local_review_repair.git_commit_with_retry

    def legacy_commit(*args, **kwargs):
        # Reproduce the historical publisher, before per-command identity existed.
        kwargs.pop("commit_env", None)
        return original_commit(*args, **kwargs)

    with monkeypatch.context() as legacy:
        legacy.setattr(local_review_repair, "git_commit_with_retry", legacy_commit)
        with missing_git_identity(flow.repository, monkeypatch):
            first_actions = flow.run()
    assert flow.head() == flow.initial_head, first_actions
    assert flow.reviewer.calls == ["fresh"]
    flow.publication.assert_not_called()
    flow.merge.assert_not_called()
    store = LocalReviewRepairStore(local_review_repair_db_path(REPO_NAME))
    lookup = LocalReviewRepairRequest(REPO_NAME, PR_NUMBER, REPO_NAME, "", "", (), "")
    old = store.get_unfinished(lookup, feedback_prefix=editable_review.REVIEW_OUTPUT_PREFIX)
    assert old is not None and old.phase == "indeterminate" and not old.result_sha
    assert "identity" in old.reason.lower() or "email" in old.reason.lower()
    request = LocalReviewRepairRequest(REPO_NAME, PR_NUMBER, REPO_NAME, old.head_ref, old.head_sha, old.feedback_identities, "", expected_push_url=old.expected_push_url)
    retain_legacy_shape(store, request)
    retained_workspace = Path(old.workspace_path)
    retained_contents = (retained_workspace / "sample.py").read_bytes()

    peer = tmp_path / "independent-owner"
    _git(["clone", "--branch", "feature-branch", str(flow.remote), str(peer)], tmp_path)
    _git(["config", "user.name", "Independent Owner"], peer)
    _git(["config", "user.email", "owner@example.com"], peer)
    (peer / "sample.py").write_bytes(retained_contents)
    (peer / "independent.txt").write_text("independent owner work\n", encoding="utf-8")
    _git(["add", "-A"], peer)
    _git(["commit", "-m", "Independently incorporate the full reviewer correction"], peer)
    _git(["push", "origin", "HEAD:feature-branch"], peer)
    replacement_head = flow.head()
    assert replacement_head != flow.initial_head

    def forbidden(*_args, **_kwargs):
        pytest.fail("supersession must not resend or reproduce the old reviewer output")

    monkeypatch.setattr(local_review_repair, "git_commit_with_retry", forbidden)
    monkeypatch.setattr(local_review_repair, "git_push", forbidden)
    monkeypatch.setattr(editable_review, "execute_local_review_repair", forbidden)
    trace_start = get_trace_collector().get_snapshot().sequence_high_watermark
    supersession_actions = flow.run()
    assert flow.reviewer.calls == ["fresh"], supersession_actions
    flow.publication.assert_not_called()
    flow.merge.assert_not_called()
    assert store.get(request).phase == "superseded"
    assert store.get_supersession(request).replacement_head == replacement_head
    events = [event for event in get_trace_collector().get_snapshot().events if event.sequence > trace_start and (event.facts or {}).get("effect") == "reviewer-correction-superseded"]
    assert len(events) == 1
    assert events[0].stage_id == "pr.adversarial-validation"
    assert events[0].outcome == "deferred"
    assert events[0].facts["local_phase"] == "superseded"
    assert events[0].facts["published"] is False
    assert events[0].facts["review_disposition"] == "RECONCILIATION_WAIT"
    assert flow.last_status.outcome is PRProcessingOutcome.DEFERRED
    from auto_coder.dashboard_detail import evidence_rows

    display = evidence_rows(events)
    assert len(display) == 1 and display[0]["outcome"] == "deferred"
    assert "effect: reviewer-correction-superseded" in display[0]["facts"]
    assert "local_phase: superseded" in display[0]["facts"]
    assert "published: false" in display[0]["facts"]
    assert "review_disposition: RECONCILIATION_WAIT" in display[0]["facts"]

    monkeypatch.setattr(pr_processor, "_check_github_actions_status", authoritative_ci_status(success=False, ids=[20], in_progress=True))
    flow.run()
    assert flow.reviewer.calls == ["fresh"]
    flow.publication.assert_not_called()
    flow.merge.assert_not_called()

    monkeypatch.setattr(pr_processor, "_check_github_actions_status", authoritative_ci_status(success=True, ids=[21]))
    final_actions = flow.run()
    assert flow.reviewer.calls == ["fresh", "fresh"], final_actions
    assert [head for _, head, _ in flow.seen] == [flow.initial_head, replacement_head]
    flow.publication.assert_called_once()
    flow.merge.assert_called_once()
    assert retained_workspace.is_dir()
    assert (retained_workspace / "sample.py").read_bytes() == retained_contents
    assert _git(["rev-parse", "HEAD"], retained_workspace) == flow.initial_head
    assert _caller_snapshot(flow.repository) == before


def test_controller_identity_failure_resumes_without_review_and_waits_for_fresh_ci(flow, monkeypatch):
    from auto_coder import editable_review
    from auto_coder.local_review_repair import LocalReviewRepairRequest, LocalReviewRepairStore, local_review_repair_db_path

    before = _caller_snapshot(flow.repository)
    flow.reviewer.mutation = _edit_source

    def unavailable(_repository):
        raise ControllerGitIdentityError("Controller App installation is unavailable")

    with monkeypatch.context() as offline:
        offline.setattr(pr_processor, "resolve_controller_git_identity", unavailable)
        first_actions = flow.run()
    assert flow.head() == flow.initial_head, first_actions
    assert flow.reviewer.calls == ["fresh"]
    flow.publication.assert_not_called()
    flow.merge.assert_not_called()
    store = LocalReviewRepairStore(local_review_repair_db_path(REPO_NAME))
    lookup = LocalReviewRepairRequest(REPO_NAME, PR_NUMBER, REPO_NAME, "", "", (), "")
    old = store.get_unfinished(lookup, feedback_prefix=editable_review.REVIEW_OUTPUT_PREFIX)
    assert old is not None and old.phase == "indeterminate" and not old.result_sha
    assert old.pre_push_evidence_json
    request = LocalReviewRepairRequest(REPO_NAME, PR_NUMBER, REPO_NAME, old.head_ref, old.head_sha, old.feedback_identities, "", expected_push_url=old.expected_push_url)
    trace_start = get_trace_collector().get_snapshot().sequence_high_watermark

    recovery_actions = flow.run()
    candidate = flow.head()
    assert candidate != flow.initial_head, recovery_actions
    assert _git(["rev-parse", f"{candidate}^"], flow.remote) == flow.initial_head
    assert flow.reviewer.calls == ["fresh"]
    recovered = store.get(request)
    assert recovered is not None and recovered.phase == "awaiting_validation"
    assert recovered.result_sha == candidate
    assert _git(["show", "-s", "--format=%an%n%ae%n%cn%n%ce", candidate], flow.remote).splitlines() == [
        "controller[bot]",
        "98765+controller[bot]@users.noreply.github.com",
        "controller[bot]",
        "98765+controller[bot]@users.noreply.github.com",
    ]
    events = _correction_events(trace_start)
    assert len(events) == 1
    assert events[0].outcome == "deferred"
    assert events[0].facts["effect"] == "reviewer-publication-resumed"
    assert events[0].facts["local_phase"] == "awaiting_validation"
    assert events[0].facts["published"] is True
    assert events[0].facts["review_disposition"] == "CORRECTIVE_HANDOFF"
    from auto_coder.dashboard_detail import evidence_rows

    display = evidence_rows(events)
    assert "effect: reviewer-publication-resumed" in display[0]["facts"]
    assert "published: true" in display[0]["facts"]
    assert flow.last_status.outcome is PRProcessingOutcome.DEFERRED
    flow.publication.assert_not_called()
    flow.merge.assert_not_called()

    monkeypatch.setattr(pr_processor, "_check_github_actions_status", authoritative_ci_status(success=False, ids=[20], in_progress=True))
    flow.run()
    assert flow.reviewer.calls == ["fresh"]
    flow.publication.assert_not_called()
    flow.merge.assert_not_called()
    monkeypatch.setattr(pr_processor, "_check_github_actions_status", authoritative_ci_status(success=True, ids=[21]))
    final_actions = flow.run()
    assert flow.reviewer.calls == ["fresh", "fresh"], final_actions
    assert [head for _, head, _ in flow.seen] == [flow.initial_head, candidate]
    flow.publication.assert_called_once()
    flow.merge.assert_called_once()
    assert _caller_snapshot(flow.repository) == before
