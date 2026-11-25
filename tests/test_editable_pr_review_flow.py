"""Editable review through the actual PR processor, Git worktrees and publisher.

Only external reviewer, GitHub and CI/merge endpoints are controlled. Candidate
comparison, attempt state, session invalidation and commit/push use production
code against a real local bare repository.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from unittest.mock import Mock

import pytest

from auto_coder import pr_processor
from auto_coder.automation_config import ProcessedPRResult, PRProcessingOutcome
from auto_coder.cli_helpers import AdversarialValidationAvailability
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
    _git(["remote", "add", "origin", "https://github.com/owner/repo.git"], repository)
    _git(["config", f"url.{remote}.insteadOf", "https://github.com/owner/repo.git"], repository)
    monkeypatch.setenv("AUTO_CODER_WORKSPACE_ROOT", str(repository))
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(tmp_path / "runtime"))
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)

    _apply_standard_merge_gates(monkeypatch, merge_result=True)
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
