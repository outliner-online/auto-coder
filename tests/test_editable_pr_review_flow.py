"""Editable review through the actual PR processor, Git worktrees and publisher.

Only external reviewer, GitHub and CI/merge endpoints are controlled. Candidate
comparison, attempt state, session invalidation and commit/push use production
code against a real local bare repository.
"""

from __future__ import annotations

import json
import runpy
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


def _ordinary_correction_payload() -> str:
    payload = json.loads(PASS_PAYLOAD)
    payload.update(
        result="NEEDS_FIX",
        summary="The admitted HEAD returns incorrect. The local correction now returns hello; it still needs independent review.",
        requirement_coverage=[{"requirement_id": "#99/REQ-001", "status": "VIOLATED", "evidence": "At the admitted HEAD, sample.greet() returns incorrect instead of hello."}],
        findings=[
            {
                "finding_identity": "incorrect-greeting",
                "correction_identity": "restore-required-greeting",
                "requirement_ids": ["#99/REQ-001"],
                "violated_requirement": "greet() returns the string hello.",
                "evidence_classification": "DEMONSTRATED",
                "reachability": "Import sample and call greet() at the admitted input HEAD.",
                "required_behavior": "greet() returns the string hello.",
                "actual_behavior": "greet() returns the string incorrect.",
                "evidence": "sample.py:2 at the admitted HEAD returns incorrect; the pre-edit call confirms this value.",
                "counterexample": "Calling greet() at the admitted HEAD must return hello but returns incorrect.",
                "test_gap": "The admitted repository has no greeting regression test.",
                "suggested_regression_scenario": "Import sample and assert greet() == 'hello'.",
                "anchor_path": "sample.py",
                "anchor_line": 2,
                "anchor_side": "RIGHT",
            }
        ],
    )
    return json.dumps(payload)


def _strong_response_identity(prompt: str) -> dict:
    identity_block = prompt.split("Identity (copy every value exactly into the JSON response):\n", 1)[1].split("Issue identities:", 1)[0]
    payload = {}
    for line in identity_block.splitlines():
        key, separator, value = line.strip().partition("=")
        if separator:
            payload[key] = int(value) if key == "finding_set_revision" else value
    return payload


def _strong_correction_response(prompt: str) -> str:
    payload = _strong_response_identity(prompt)
    payload.update(
        result="FINDINGS",
        summary="The admitted HEAD returns incorrect. The local correction returns hello but requires independent review.",
        findings=[
            {
                "finding_id": "incorrect-greeting",
                "requirement_ids": ["#99/REQ-001"],
                "requirement_texts": ["REQ-001: greet() returns the string hello."],
                "counterexample": "Calling greet() at the admitted HEAD must return hello but returns incorrect.",
                "expected_behavior": "greet() returns hello.",
                "actual_behavior": "greet() returns incorrect.",
                "evidence": "sample.py:2 at the admitted HEAD returns incorrect; the pre-edit call confirms this value.",
                "affected_boundary": "sample.py:greet",
                "material_consequence": "The public greeting result violates the explicit required value.",
                "focused_regression_scenario": "Import sample and assert greet() == 'hello'.",
                "is_regression_gap": False,
            }
        ],
    )
    return json.dumps(payload)


def _edit_source(root: Path) -> None:
    source = root / "sample.py"
    assert runpy.run_path(str(source))["greet"]() == "incorrect"
    source.write_text('def greet():\n    return "hello"\n', encoding="utf-8")
    assert runpy.run_path(str(source))["greet"]() == "hello"


def _optional_source_edit(root: Path) -> None:
    source = root / "sample.py"
    assert runpy.run_path(str(source))["greet"]() == "hello"
    source.write_text(source.read_text() + "\n# Optional greeting explanation.\n", encoding="utf-8")


def _write_runtime_outputs(root: Path) -> None:
    for directory in (".agent-tmp", ".pytest_cache", "test-results"):
        output = root / directory / "review-output.txt"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("runtime evidence\n", encoding="utf-8")


@pytest.fixture
def flow(tmp_path, monkeypatch, _use_real_commands, audit_store, request):
    repository, head = _build_pr_repo(tmp_path)
    scenario_kind = getattr(request, "param", "")
    if scenario_kind == "defective":
        (repository / "sample.py").write_text('def greet():\n    return "incorrect"\n', encoding="utf-8")
        _git(["add", "sample.py"], repository)
        _git(["commit", "--amend", "--no-edit"], repository)
        head = _git(["rev-parse", "HEAD"], repository)
    if scenario_kind in {"dynamic", "interrupted-dynamic"}:
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
        response_factory: Callable[[str], str] | None = None
        fail_after_edit = False

        def _response(self, mode: str, is_noedit: bool, prompt: str) -> str:
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
            return self.response_factory(prompt) if self.response_factory is not None else self._next()

        def _run_llm_cli(self, prompt, is_noedit=False):
            return self._response("fresh", is_noedit, prompt)

        def continue_session(self, session_id, prompt, is_noedit=False):
            return self._response("continue", is_noedit, prompt)

    first_response = _ordinary_correction_payload() if scenario_kind == "defective" else PASS_PAYLOAD
    reviewer = Reviewer("reviewer", responses=[first_response, PASS_PAYLOAD], session_id="review-provider-session")
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


@pytest.mark.parametrize("flow", ["defective"], indirect=True)
def test_review_fix_is_published_without_self_approval_then_independently_reviewed(flow, monkeypatch):
    registry = ReviewerSessionRegistry()
    retained = ReviewerSession(REPO_NAME, PR_NUMBER, "reviewer", "codex-cloud", "reviewer-model", "prior-review-session", flow.initial_head)
    registry.save(retained)
    before = _caller_snapshot(flow.repository)
    flow.reviewer.mutation = _edit_source
    trace_start = get_trace_collector().get_snapshot().sequence_high_watermark

    actions = flow.run()

    changed_head = flow.head()
    assert changed_head != flow.initial_head, actions
    assert _git(["rev-parse", f"{changed_head}^"], flow.remote) == flow.initial_head
    assert _git(["show", f"{changed_head}:sample.py"], flow.remote) == 'def greet():\n    return "hello"'
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


@pytest.mark.parametrize("flow", ["defective"], indirect=True)
def test_ordinary_correction_refuses_requirement_changed_during_review(flow, monkeypatch):
    from auto_coder import local_review_repair

    before = _caller_snapshot(flow.repository)

    def correct_and_change_requirement(root):
        _edit_source(root)
        issue = flow.client.get_issue.return_value
        flow.client.get_issue.return_value = {**issue, "body": issue["body"].replace("string hello.", "string goodbye.")}

    flow.reviewer.mutation = correct_and_change_requirement
    commit = Mock(wraps=local_review_repair.git_commit_with_retry)
    push = Mock(wraps=local_review_repair.git_push)
    monkeypatch.setattr(local_review_repair, "git_commit_with_retry", commit)
    monkeypatch.setattr(local_review_repair, "git_push", push)
    trace_start = get_trace_collector().get_snapshot().sequence_high_watermark

    actions = flow.run()

    assert flow.head() == flow.initial_head, actions
    commit.assert_not_called()
    push.assert_not_called()
    assert flow.reviewer.calls == ["fresh"]
    flow.publication.assert_not_called()
    flow.merge.assert_not_called()
    assert _caller_snapshot(flow.repository) == before
    events = _correction_events(trace_start)
    assert len(events) == 1
    assert events[0].facts["published"] is False
    assert "authority" in events[0].facts["reason"].lower()


@pytest.mark.parametrize("editing_verdict", ["PASS", "INCONCLUSIVE", "malformed"])
def test_nonblocking_or_invalid_review_cannot_publish_optional_edits(flow, monkeypatch, editing_verdict):
    from auto_coder import local_review_repair

    before = _caller_snapshot(flow.repository)
    flow.reviewer.mutation = _optional_source_edit
    if editing_verdict == "malformed":
        response = "completed an optional source cleanup without valid review JSON"
    else:
        payload = json.loads(PASS_PAYLOAD)
        payload["result"] = editing_verdict
        if editing_verdict == "INCONCLUSIVE":
            payload["summary"] = "Decision-critical review evidence is unavailable."
            payload["requirement_coverage"][0].update(status="UNVERIFIED", evidence="The reviewer did not establish the required greeting result.")
        response = json.dumps(payload)
    flow.reviewer._responses = [response]
    commit = Mock(wraps=local_review_repair.git_commit_with_retry)
    push = Mock(wraps=local_review_repair.git_push)
    monkeypatch.setattr(local_review_repair, "git_commit_with_retry", commit)
    monkeypatch.setattr(local_review_repair, "git_push", push)
    trace_start = get_trace_collector().get_snapshot().sequence_high_watermark

    actions = flow.run()

    assert flow.head() == flow.initial_head, actions
    assert "Optional greeting explanation." not in _git(["show", f"{flow.head()}:sample.py"], flow.remote)
    commit.assert_not_called()
    push.assert_not_called()
    assert flow.reviewer.calls == ["fresh"]
    flow.publication.assert_not_called()
    flow.merge.assert_not_called()
    assert _caller_snapshot(flow.repository) == before
    assert all(not root.exists() for _, _, root in flow.seen)
    events = _correction_events(trace_start)
    assert len(events) == 1
    assert events[0].facts["local_phase"] == "not_started"
    assert events[0].facts["published"] is False
    assert events[0].facts["reason"].startswith("reviewer correction refused:")
    assert events[0].facts["review_disposition"] == "RECONCILIATION_WAIT"


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
def test_dynamic_check_source_mutation_cannot_certify_unchanged_head(flow, monkeypatch):
    from auto_coder import local_review_repair

    requested = json.loads(PASS_PAYLOAD)
    requested["dynamic_check_requested"] = "tests/test_sample.py"
    flow.reviewer._responses = [json.dumps(requested)]
    push = Mock(wraps=local_review_repair.git_push)
    monkeypatch.setattr(local_review_repair, "git_push", push)
    trace_start = get_trace_collector().get_snapshot().sequence_high_watermark

    actions = flow.run()

    assert flow.head() == flow.initial_head, actions
    assert "Source changed during dynamic verification." not in _git(["show", f"{flow.head()}:sample.py"], flow.remote)
    push.assert_not_called()
    assert flow.reviewer.calls == ["fresh"]
    flow.publication.assert_not_called()
    flow.merge.assert_not_called()
    events = _correction_events(trace_start)
    assert len(events) == 1
    assert events[0].facts["local_phase"] == "not_started"
    assert events[0].facts["published"] is False
    assert events[0].facts["reason"].startswith("reviewer correction refused:")


@pytest.mark.parametrize("flow", ["defective"], indirect=True)
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


def _pending_strong_inputs(flow):
    from auto_coder.pr_review_cycle import ContractSnapshot, StrongPolicyIdentity
    from auto_coder.two_tier_pr_gate import TwoTierPrGate

    inputs = pr_processor.TwoTierGateInputs(
        gate=TwoTierPrGate(REPO_NAME),
        contract=ContractSnapshot(("#99",), "Issue #99 REQ-001: greet() returns the string hello."),
        policy=StrongPolicyIdentity("strong-review", "model=strong", "v1"),
        head_sha=flow.initial_head,
        base_sha=flow.base,
        head_repository=REPO_NAME,
        head_ref="feature-branch",
    )
    inputs.gate.ordinary_pass(PR_NUMBER, flow.initial_head, flow.base, inputs.contract)
    return inputs


@pytest.mark.parametrize("flow", ["defective"], indirect=True)
def test_ordinary_pass_still_allows_strong_defect_correction_without_self_approval(flow):
    inputs = _pending_strong_inputs(flow)
    before = _caller_snapshot(flow.repository)
    flow.reviewer.mutation = _edit_source
    flow.reviewer.response_factory = _strong_correction_response

    accepted, reason = pr_processor._execute_pending_strong_audit(
        REPO_NAME,
        PR_NUMBER,
        inputs,
        publication_authority=lambda sha, ref: flow.head() == sha and ref == "feature-branch",
        correction_basis_check=lambda basis: pr_processor._review_correction_requirements_current(flow.client, REPO_NAME, PR_NUMBER, basis),
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


@pytest.mark.parametrize(
    "requirement_id,requirement_text",
    [
        ("#99/REQ-001", "REQ-002: greet() returns the string hello."),
        ("#99/REQ-001", "REQ-001: greet() returns the string goodbye."),
        ("#100/REQ-001", "REQ-001: greet() returns the string hello."),
    ],
)
@pytest.mark.parametrize("flow", ["defective"], indirect=True)
def test_strong_declaration_mismatch_cannot_publish_correction(flow, monkeypatch, requirement_id, requirement_text):
    from auto_coder import local_review_repair

    inputs = _pending_strong_inputs(flow)
    before = _caller_snapshot(flow.repository)
    flow.reviewer.mutation = _edit_source

    def response(prompt):
        payload = json.loads(_strong_correction_response(prompt))
        payload["findings"][0].update(requirement_ids=[requirement_id], requirement_texts=[requirement_text])
        return json.dumps(payload)

    flow.reviewer.response_factory = response
    commit = Mock(wraps=local_review_repair.git_commit_with_retry)
    push = Mock(wraps=local_review_repair.git_push)
    monkeypatch.setattr(local_review_repair, "git_commit_with_retry", commit)
    monkeypatch.setattr(local_review_repair, "git_push", push)

    accepted, reason = pr_processor._execute_pending_strong_audit(
        REPO_NAME,
        PR_NUMBER,
        inputs,
        publication_authority=lambda sha, ref: flow.head() == sha and ref == "feature-branch",
        correction_basis_check=lambda basis: pr_processor._review_correction_requirements_current(flow.client, REPO_NAME, PR_NUMBER, basis),
    )

    assert not accepted
    assert "Strong correction does not match a current explicit Requirement" in reason
    assert flow.head() == flow.initial_head
    commit.assert_not_called()
    push.assert_not_called()
    flow.publication.assert_not_called()
    flow.merge.assert_not_called()
    assert inputs.gate.state.snapshot(PR_NUMBER).completion is None
    assert _caller_snapshot(flow.repository) == before


@pytest.mark.parametrize("editing_verdict", ["PASS", "INCONCLUSIVE", "malformed"])
def test_strong_nonblocking_or_invalid_review_cannot_publish_optional_edits(flow, monkeypatch, editing_verdict):
    from auto_coder import local_review_repair

    inputs = _pending_strong_inputs(flow)
    before = _caller_snapshot(flow.repository)
    flow.reviewer.mutation = _optional_source_edit

    def response(prompt):
        if editing_verdict == "malformed":
            return "completed an optional source cleanup without valid review JSON"
        return json.dumps({**_strong_response_identity(prompt), "result": editing_verdict, "findings": [], "summary": "No demonstrated Requirement defect supports the optional edit."})

    flow.reviewer.response_factory = response
    commit = Mock(wraps=local_review_repair.git_commit_with_retry)
    push = Mock(wraps=local_review_repair.git_push)
    monkeypatch.setattr(local_review_repair, "git_commit_with_retry", commit)
    monkeypatch.setattr(local_review_repair, "git_push", push)

    accepted, reason = pr_processor._execute_pending_strong_audit(
        REPO_NAME,
        PR_NUMBER,
        inputs,
        publication_authority=lambda sha, ref: flow.head() == sha and ref == "feature-branch",
    )

    assert not accepted, reason
    assert flow.head() == flow.initial_head, reason
    assert "reviewer correction refused:" in reason
    commit.assert_not_called()
    push.assert_not_called()
    assert flow.reviewer.calls == ["fresh"]
    snapshot = inputs.gate.state.snapshot(PR_NUMBER)
    assert snapshot.accepted_strong_round is None
    assert snapshot.active_claim is None
    assert snapshot.completion is None
    flow.publication.assert_not_called()
    flow.merge.assert_not_called()
    assert _caller_snapshot(flow.repository) == before
    assert all(not root.exists() for _, _, root in flow.seen)


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
    # The ordinary reviewer misses a real defect that independent Strong finds.
    flow.reviewer.response_factory = lambda prompt: _strong_correction_response(prompt) if "explicitly tagged PR reviewer role STRONG_AUDIT" in prompt else PASS_PAYLOAD


@pytest.mark.parametrize("flow", ["defective"], indirect=True)
def test_renewed_strong_fix_is_published_without_closing_retained_findings(flow, monkeypatch):
    inputs, strong, _config = _seed_stale_strong_finding(flow, monkeypatch)
    before = _caller_snapshot(flow.repository)
    _edit_on_renewed_strong(flow, _edit_source)
    trace_start = get_trace_collector().get_snapshot().sequence_high_watermark

    actions = flow.run()

    changed_head = flow.head()
    assert changed_head != flow.initial_head, actions
    assert _git(["rev-parse", f"{changed_head}^"], flow.remote) == flow.initial_head
    assert _git(["show", f"{changed_head}:sample.py"], flow.remote) == 'def greet():\n    return "hello"'
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
@pytest.mark.parametrize("flow", ["defective"], indirect=True)
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


@pytest.mark.parametrize("flow", ["defective"], indirect=True)
def test_renewed_strong_pre_send_deferral_resumes_without_another_reviewer(flow, monkeypatch):
    from auto_coder import local_review_repair

    inputs, strong, _config = _seed_stale_strong_finding(flow, monkeypatch)
    before = _caller_snapshot(flow.repository)
    _edit_on_renewed_strong(flow, _edit_source)
    push = Mock(wraps=local_review_repair.git_push)
    monkeypatch.setattr(local_review_repair, "git_push", push)
    store = local_review_repair.LocalReviewRepairStore(local_review_repair.local_review_repair_db_path(REPO_NAME))
    request = local_review_repair.LocalReviewRepairRequest(REPO_NAME, PR_NUMBER, REPO_NAME, "feature-branch", flow.initial_head, (), "")
    original_metadata = flow.client.get_pull_request_metadata_strict.side_effect

    def unavailable_after_candidate(*args, **kwargs):
        if store.get_publication_pending(request) is not None:
            raise RuntimeError("PR target is temporarily unreadable before send entry")
        return original_metadata(*args, **kwargs)

    flow.client.get_pull_request_metadata_strict.side_effect = unavailable_after_candidate
    trace_start = get_trace_collector().get_snapshot().sequence_high_watermark

    first_actions = flow.run()

    assert flow.head() == flow.initial_head, first_actions
    assert flow.reviewer.calls == ["fresh", "fresh"]
    push.assert_not_called()
    retained = store.get_publication_pending(request)
    assert retained is not None and retained.publication_send_state == "not_entered"
    assert retained.result_sha != flow.initial_head
    assert _git(["rev-parse", f"{retained.result_sha}^"], flow.repository) == flow.initial_head
    assert _git(["show", f"{retained.result_sha}:sample.py"], flow.repository) == 'def greet():\n    return "hello"'
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

    flow.client.get_pull_request_metadata_strict.side_effect = original_metadata
    second_actions = flow.run()

    push.assert_called_once()
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


@pytest.mark.parametrize("flow", ["defective"], indirect=True)
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


@pytest.mark.parametrize("flow", ["defective"], indirect=True)
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


@pytest.mark.parametrize("flow", ["defective"], indirect=True)
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


@pytest.mark.parametrize("flow", ["defective"], indirect=True)
def test_retained_correction_refuses_changed_requirement_before_recovery(flow, monkeypatch):
    from auto_coder import editable_review, local_review_repair
    from auto_coder.local_review_repair import LocalReviewRepairRequest, LocalReviewRepairStore, local_review_repair_db_path

    before = _caller_snapshot(flow.repository)
    flow.reviewer.mutation = _edit_source
    commit = Mock(wraps=local_review_repair.git_commit_with_retry)
    push = Mock(wraps=local_review_repair.git_push)
    monkeypatch.setattr(local_review_repair, "git_commit_with_retry", commit)
    monkeypatch.setattr(local_review_repair, "git_push", push)

    def unavailable(_repository):
        raise ControllerGitIdentityError("Controller App installation is unavailable")

    with monkeypatch.context() as offline:
        offline.setattr(pr_processor, "resolve_controller_git_identity", unavailable)
        first_actions = flow.run()
    assert flow.head() == flow.initial_head, first_actions
    assert flow.reviewer.calls == ["fresh"]
    push.assert_not_called()
    store = LocalReviewRepairStore(local_review_repair_db_path(REPO_NAME))
    lookup = LocalReviewRepairRequest(REPO_NAME, PR_NUMBER, REPO_NAME, "", "", (), "")
    retained = store.get_unfinished(lookup, feedback_prefix=editable_review.REVIEW_OUTPUT_PREFIX)
    assert retained is not None and retained.phase == "indeterminate" and not retained.result_sha
    assert retained.pre_push_evidence_json
    commits_before_recovery = commit.call_count
    issue = flow.client.get_issue.return_value
    flow.client.get_issue.return_value = {**issue, "body": issue["body"].replace("string hello.", "string goodbye.")}
    trace_start = get_trace_collector().get_snapshot().sequence_high_watermark

    recovery_actions = flow.run()

    assert flow.head() == flow.initial_head, recovery_actions
    assert flow.reviewer.calls == ["fresh"]
    assert commit.call_count == commits_before_recovery
    push.assert_not_called()
    flow.publication.assert_not_called()
    flow.merge.assert_not_called()
    assert _caller_snapshot(flow.repository) == before
    preserved = LocalReviewRepairStore(local_review_repair_db_path(REPO_NAME)).get_unfinished(lookup, feedback_prefix=editable_review.REVIEW_OUTPUT_PREFIX)
    assert preserved is not None and preserved.attempt_id == retained.attempt_id
    assert preserved.workspace_path == retained.workspace_path
    events = _correction_events(trace_start)
    assert len(events) == 1
    assert events[0].facts["published"] is False
    assert "authority" in events[0].facts["reason"].lower()


@pytest.mark.parametrize("flow", ["defective"], indirect=True)
def test_unresolved_push_restart_waits_without_resend_until_candidate_readback(flow, monkeypatch):
    from auto_coder import local_review_repair
    from auto_coder.utils import CommandResult

    before = _caller_snapshot(flow.repository)
    unknown_push = Mock(return_value=CommandResult(False, "", "push response was lost", 1))
    monkeypatch.setattr(local_review_repair, "git_push", unknown_push)
    flow.reviewer.mutation = _edit_source
    trace_start = get_trace_collector().get_snapshot().sequence_high_watermark

    first_actions = flow.run()

    assert flow.head() == flow.initial_head, first_actions
    unknown_push.assert_called_once()
    assert flow.reviewer.calls == ["fresh"]
    path = local_review_repair.local_review_repair_db_path(REPO_NAME)
    lookup = local_review_repair.LocalReviewRepairRequest(REPO_NAME, PR_NUMBER, REPO_NAME, "", "", (), "")
    retained = local_review_repair.LocalReviewRepairStore(path).get_publication_pending(lookup)
    assert retained is not None and retained.publication_send_state == "entered"
    assert retained.result_sha != flow.initial_head
    flow.publication.assert_not_called()
    flow.merge.assert_not_called()

    for _ in range(2):
        restarted = replace(flow, last_status=None)
        resumed_actions = restarted.run()
        reopened = local_review_repair.LocalReviewRepairStore(path).get_publication_pending(lookup)
        assert reopened is not None and reopened.result_sha == retained.result_sha
        assert reopened.publication_send_state == "entered"
        assert flow.head() == flow.initial_head, resumed_actions
        assert restarted.last_status.outcome is PRProcessingOutcome.DEFERRED
        unknown_push.assert_called_once()
        assert flow.reviewer.calls == ["fresh"]
        event = _correction_events(trace_start)[-1]
        assert event.facts["local_phase"] == "publication_pending"
        assert event.facts["published"] is False
        assert event.facts["review_disposition"] == "RECONCILIATION_WAIT"
        flow.publication.assert_not_called()
        flow.merge.assert_not_called()

    # Model late acceptance at the external server without another controller send.
    _git(["fetch", "--no-tags", str(flow.repository), retained.result_sha], flow.remote)
    _git(["update-ref", "refs/heads/feature-branch", retained.result_sha, flow.initial_head], flow.remote)
    reconciled = replace(flow, last_status=None)
    final_actions = reconciled.run()

    assert flow.head() == retained.result_sha, final_actions
    unknown_push.assert_called_once()
    assert flow.reviewer.calls == ["fresh"]
    assert local_review_repair.LocalReviewRepairStore(path).get_publication_pending(lookup) is None
    event = _correction_events(trace_start)[-1]
    assert event.facts["local_phase"] == "awaiting_validation"
    assert event.facts["published"] is True
    assert event.facts["review_disposition"] == "CORRECTIVE_HANDOFF"
    flow.publication.assert_not_called()
    flow.merge.assert_not_called()
    assert _caller_snapshot(flow.repository) == before
