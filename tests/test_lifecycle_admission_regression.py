"""Production narrative must not turn an idle draft into durable capacity."""

from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult, ExplicitTargetOutcome, ProcessedPRResult, PRProcessingOutcome
from auto_coder.automation_engine import AutomationEngine
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.issue_context import extract_lifecycle_directive_issue_references
from auto_coder.pr_processor import CloudTaskOriginResolution

NARRATIVE = "Grid: existing closed #5459/REQ-007–008 ... No Issue Requirements or ownership were rewritten"


def test_production_narrative_is_not_a_directive():
    assert extract_lifecycle_directive_issue_references(NARRATIVE) == []


@pytest.mark.parametrize("verb", ["close", "closes", "closed", "closing", "fix", "fixes", "fixed", "resolve", "resolves", "resolved", "resolving"])
@pytest.mark.parametrize("prefix", ["", "- ", "* ", "1. ", "Summary. "])
def test_directive_clauses_preserve_all_verb_forms(verb, prefix):
    assert extract_lifecycle_directive_issue_references(f"{prefix}{verb}: owner/repo#5459") == [("owner/repo", 5459)]


def test_startup_narrative_does_not_extend_closed_issue_membership(tmp_path):
    from tests.test_implementation_slots import GitHubState

    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    old = ImplementationOwner("issue", 5459)
    assert slots.reserve(old, implementation_pr=5488)
    github = GitHubState(
        issues={5459: {"number": 5459, "state": "closed"}},
        prs={5488: {"state": "closed", "merged": True}, 5940: {"state": "open", "draft": True}},
        open_prs=[{"number": 5940, "body": NARRATIVE, "head": {"ref": "grid-cleanup"}}],
    )
    slots.reconcile(github, discover_open_prs=True)
    restarted = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    assert restarted.active_owners() == ()
    next_owner = ImplementationOwner("issue", 5949)
    execution = restarted.start_execution(next_owner)
    assert execution is not None
    assert restarted.start_execution(next_owner) is None


def test_unowned_draft_without_backend_defers_before_reservation(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(tmp_path))
    config = AutomationConfig()
    github = MagicMock()
    pr = {"number": 5940, "title": "Grid cleanup", "body": NARRATIVE, "draft": True, "state": "open", "user": {"login": "human"}, "head": {"ref": "grid-cleanup", "sha": "a" * 40}, "labels": []}
    github.get_pull_request_metadata_strict.return_value = pr
    github.get_issue.return_value = {"number": 5459, "state": "closed"}
    engine = AutomationEngine(github, config=config)
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    engine.implementation_slots = slots
    with (
        patch("auto_coder.pr_processor._selected_local_backend_name", return_value=""),
        patch.object(engine, "refresh_review_adjudications"),
        patch("auto_coder.automation_engine.process_pull_request") as process,
        patch.object(slots, "start_execution", wraps=slots.start_execution) as start,
    ):
        process.return_value = ProcessedPRResult(pr, outcome=PRProcessingOutcome.DEFERRED, target_reason="No local PR repair backend is configured")
        for _ in range(2):
            result = engine._process_single_candidate_unified("owner/repo", Candidate(type="pr", data=pr, priority=0), config)
            assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
            assert "backend" in result.target_reason.lower()
        start.assert_not_called()
        process.assert_not_called()
    from auto_coder.execution_trace import get_trace_collector

    events = get_trace_collector().get_snapshot(repository="owner/repo", item_type="pr", item_number=5940).events
    admissions = [event for event in events if event.stage_id == "pr.implementation-admission"]
    assert admissions
    assert all(event.outcome == "deferred" and event.facts["owner"] == "pr:5940" for event in admissions)
    assert slots.active_owners() == ()
    next_owner = ImplementationOwner("issue", 5949)
    from tests.test_issue_discovery_admission import _issue

    issue = {**_issue(5949), "labels": [{"name": "implementation-ready"}]}
    github.get_issue_dispatch_snapshot_strict.return_value = issue
    with patch.object(engine, "_process_single_candidate_reserved", return_value=CandidateProcessingResult(type="issue", number=5949, title="Issue 5949", success=True)) as dispatch:
        admitted = engine._process_single_candidate_unified("owner/repo", Candidate(type="issue", data=issue, priority=0), config)
        replay = engine._process_single_candidate_unified("owner/repo", Candidate(type="issue", data=issue, priority=0), config)
    assert admitted.success is True
    assert replay.target_outcome in {ExplicitTargetOutcome.SKIPPED, ExplicitTargetOutcome.DEFERRED}
    dispatch.assert_called_once()
    assert slots.active_owners() == (next_owner,)


def test_historical_narrative_membership_is_not_erased_from_text(tmp_path):
    from tests.test_implementation_slots import GitHubState

    path = tmp_path / "slots.json"
    slots = ImplementationSlotRepository("owner/repo", 1, path)
    old = ImplementationOwner("issue", 5459)
    assert slots.reserve(old, implementation_pr=5940)
    github = GitHubState(
        issues={5459: {"number": 5459, "state": "closed"}},
        prs={5940: {"number": 5940, "state": "open", "draft": True}},
        open_prs=[{"number": 5940, "body": NARRATIVE, "head": {"ref": "grid-cleanup"}}],
    )
    restarted = ImplementationSlotRepository("owner/repo", 1, path)
    restarted.reconcile(github, discover_open_prs=True)
    assert restarted.active_owners() == (old,)
    assert restarted.snapshot().owners[0].implementation_prs == (5940,)
    assert restarted.available_normal_slots() == 0


@pytest.mark.parametrize("route", ["local", "provider", "ready"])
def test_draft_preflight_preserves_executable_routes(route):
    from auto_coder.pr_processor import CloudTaskOrigin, draft_pr_implementation_blocker

    github = MagicMock()
    pr = {"number": 5940, "state": "open", "draft": route != "ready"}
    github.get_pull_request_metadata_strict.return_value = pr
    origin = CloudTaskOriginResolution(origin=CloudTaskOrigin("jules", "existing-task", MagicMock()))
    with patch("auto_coder.pr_processor._selected_local_backend_name", return_value="local-edit" if route == "local" else ""), patch("auto_coder.pr_processor._resolve_cloud_task_origin", return_value=origin):
        assert draft_pr_implementation_blocker("owner/repo", 5940, github) is None


@pytest.mark.parametrize("evidence", [None, {"number": 5940}, RuntimeError("GitHub offline")])
def test_draft_preflight_fails_closed_for_unavailable_evidence(evidence):
    from auto_coder.pr_processor import draft_pr_implementation_blocker

    github = MagicMock()
    if isinstance(evidence, Exception):
        github.get_pull_request_metadata_strict.side_effect = evidence
    else:
        github.get_pull_request_metadata_strict.return_value = evidence
    with patch("auto_coder.pr_processor._selected_local_backend_name", return_value=""), patch("auto_coder.pr_processor._resolve_cloud_task_origin") as provider:
        assert draft_pr_implementation_blocker("owner/repo", 5940, github)
        provider.assert_not_called()


@pytest.mark.parametrize("retained,manual", [(True, False), (False, True)])
def test_retained_and_manual_drafts_reach_ordinary_processing(tmp_path, retained, manual):
    config = AutomationConfig()
    github = MagicMock()
    pr = {"number": 5940, "title": "Grid", "body": NARRATIVE, "draft": True, "head": {"ref": "grid-cleanup"}, "labels": []}
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    owner = ImplementationOwner("pr", 5940)
    if retained:
        assert slots.reserve(owner)
    engine = AutomationEngine(github, config=config)
    engine.implementation_slots = slots
    with (
        patch.object(engine, "refresh_review_adjudications"),
        patch("auto_coder.pr_processor.draft_pr_implementation_blocker") as preflight,
        patch.object(engine, "_process_single_candidate_reserved", return_value=CandidateProcessingResult(type="pr", number=5940, title="Grid", success=False)) as process,
    ):
        engine._process_single_candidate_unified("owner/repo", Candidate(type="pr", data=pr, priority=0), config, explicit_only=manual)
    preflight.assert_not_called()
    process.assert_called_once()


@pytest.mark.parametrize("body", ["Fixes #1, closes #2", "Fixes #1. Also resolves #2", "Summary: This PR fixes #1; closed #2", "### Fixed #1\n- Resolving #2"])
def test_multiple_explicit_directives_remain_ordered(body):
    assert extract_lifecycle_directive_issue_references(body) == [(None, 1), (None, 2)]


@pytest.mark.parametrize("issue_owner", [None, 5459])
def test_draft_provider_preflight_does_not_promote_narrative_to_provider_ownership(issue_owner):
    from auto_coder.cloud_manager import CloudManager
    from auto_coder.pr_processor import draft_pr_implementation_blocker

    assert CloudManager("owner/repo").add_session(5459, "historical-task", provider="jules")
    github = MagicMock()
    github.get_pull_request_metadata_strict.return_value = {
        "number": 5940,
        "body": NARRATIVE,
        "state": "open",
        "draft": True,
        "head": {"ref": "issue-5459" if issue_owner else "grid-cleanup"},
    }
    with patch("auto_coder.pr_processor._selected_local_backend_name", return_value=""), patch("auto_coder.cloud_task_engine.CloudTaskEngine") as provider:
        provider.return_value.get_client_for_provider.return_value = MagicMock()
        blocker = draft_pr_implementation_blocker("owner/repo", 5940, github, issue_owner)
        assert (blocker is None) is (issue_owner is not None)
        if issue_owner is None:
            provider.assert_not_called()
