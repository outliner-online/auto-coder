"""Production narrative must not turn an idle draft into durable capacity."""

from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult, ExplicitTargetOutcome, ProcessedPRResult, PRProcessingOutcome
from auto_coder.automation_engine import AutomationEngine
from auto_coder.github_controller_auth import ControllerIdentity
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.issue_context import extract_lifecycle_directive_issue_references
from auto_coder.pr_processor import CloudTaskOriginResolution
from tests.pr_automation_fakes import install_mock_pr_history

NARRATIVE = "Grid: existing closed #5459/REQ-007–008 ... No Issue Requirements or ownership were rewritten"


@pytest.mark.parametrize("body,issue_read,ready", [(NARRATIVE, False, True), (f"`{NARRATIVE}`", False, True), ("Closes #5459", True, True), ("Closes #5459", True, False)])
def test_real_startup_owner_discovery_never_fetches_documentary_issue(tmp_path, monkeypatch, body, issue_read, ready):
    import asyncio

    from auto_coder.implementation_slots import ImplementationOwnerResolutionError
    from auto_coder.util.gh_cache import OpenGitHubEntities

    strict_read = MagicMock(return_value={"number": 5459, "state": "open"})
    if not ready or not issue_read:
        strict_read.side_effect = RuntimeError("local Issue lookup is unavailable")

    class StrictStartupGitHub(MagicMock):
        def get_issue_strict(self, repository, number):
            return strict_read(repository, number)

    github = StrictStartupGitHub()
    github.get_open_pull_requests.return_value = [{"number": 2864, "body": body, "head": {"ref": "fix/lifecycle-slot-intake"}}]
    github.get_open_entities_strict.return_value = OpenGitHubEntities(pull_requests=[2864])
    slots = ImplementationSlotRepository("outliner-online/auto-coder", 1, tmp_path / "slots.json")
    engine = AutomationEngine(github, AutomationConfig())
    monkeypatch.setattr(engine, "_get_implementation_slots", lambda repository: slots)
    monkeypatch.setattr("auto_coder.automation_engine.install_asyncio_diagnostics", lambda loop: None)
    monkeypatch.setattr("auto_coder.automation_engine.get_health_monitor", MagicMock())
    lanes = []

    async def idle(*args):
        await engine._shutdown_event.wait()

    async def lane(repository, worker_id, item_type):
        assert engine.startup_reconciled
        lanes.append(item_type)
        await idle()

    for name in ("_producer_loop", "_invalidation_loop", "_capacity_refill_loop", "_claude_followup_recovery_loop"):
        monkeypatch.setattr(engine, name, idle)
    monkeypatch.setattr(engine, "_worker_loop", lane)

    async def startup():
        task = asyncio.create_task(engine.start_automation("outliner-online/auto-coder", concurrency=1))
        try:
            if not ready:
                with pytest.raises(ImplementationOwnerResolutionError, match="local Issue lookup is unavailable"):
                    await asyncio.wait_for(task, 5)
                assert not engine.startup_reconciled and lanes == []
            else:
                for _ in range(200):
                    if len(lanes) == 2:
                        break
                    if task.done():
                        await task
                    await asyncio.sleep(0.01)
                assert sorted(lanes) == ["issue", "pr"]
                assert engine.startup_reconciled and engine.daemon_serving
                github.get_open_entities_strict.assert_called_once_with("outliner-online/auto-coder")
        finally:
            engine.request_graceful_shutdown("test complete")
            if not task.done():
                await asyncio.wait_for(task, 5)

    asyncio.run(startup())
    if issue_read:
        strict_read.assert_called_once_with("outliner-online/auto-coder", 5459)
    else:
        strict_read.assert_not_called()
    assert slots.active_owners() == ()


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
    install_mock_pr_history(github, "a" * 40)
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
    ):
        process.return_value = ProcessedPRResult(pr, outcome=PRProcessingOutcome.DEFERRED, target_reason="No local PR repair backend is configured")
        for _ in range(2):
            result = engine._process_single_candidate_unified("owner/repo", Candidate(type="pr", data=pr, priority=0), config)
            assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
            assert "backend" in result.target_reason.lower()
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
    install_mock_pr_history(github, "a" * 40)
    pr = {"number": 5940, "state": "open", "draft": route != "ready"}
    github.get_pull_request_metadata_strict.return_value = pr
    origin = CloudTaskOriginResolution(origin=CloudTaskOrigin("jules", "existing-task", MagicMock()))
    with patch("auto_coder.pr_processor._selected_local_backend_name", return_value="local-edit" if route == "local" else ""), patch("auto_coder.pr_processor._resolve_cloud_task_origin", return_value=origin):
        assert draft_pr_implementation_blocker("owner/repo", 5940, github) is None


@pytest.mark.parametrize("evidence", [None, {"number": 5940}, RuntimeError("GitHub offline")])
def test_draft_preflight_fails_closed_for_unavailable_evidence(evidence):
    from auto_coder.pr_processor import draft_pr_implementation_blocker

    github = MagicMock()
    install_mock_pr_history(github, "a" * 40)
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
    install_mock_pr_history(github, "a" * 40)
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


@pytest.mark.parametrize("body", ["Fixes #1, closes #2", "Fixes #1. Also resolves #2", "Summary: This PR fixes #1; closed #2", "### Fixed #1\n- Resolving #2", "Fixes:\n#1\nCloses:\n#2"])
def test_multiple_explicit_directives_remain_ordered(body):
    assert extract_lifecycle_directive_issue_references(body) == [(None, 1), (None, 2)]


@pytest.mark.parametrize("issue_owner", [None, 5459])
def test_draft_provider_preflight_does_not_promote_narrative_to_provider_ownership(issue_owner):
    from auto_coder.cloud_manager import CloudManager
    from auto_coder.pr_processor import draft_pr_implementation_blocker

    assert CloudManager("owner/repo").add_session(5459, "historical-task", provider="jules")
    github = MagicMock()
    install_mock_pr_history(github, "a" * 40)
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


@pytest.mark.parametrize(
    "provider,stored,marker",
    [
        ("jules", "known-task", "https://jules.google.com/session/known-task"),
        ("claude-routine", "cse_known", "https://claude.ai/code/session_known"),
    ],
)
@pytest.mark.parametrize("provenance", ["missing", "direct-pr", "lifecycle-issue"])
def test_recorded_provider_session_requires_independent_draft_association(provider, stored, marker, provenance):
    from auto_coder.cloud_manager import CloudTaskBinding
    from auto_coder.pr_processor import DraftPRAdmissionEvidence, DraftPRRepairAuthority, _resolve_cloud_task_origin, _revalidate_cloud_origin, draft_pr_implementation_blocker, draft_pr_repair_authority_scope

    binding = CloudTaskBinding(provider=provider, task_id=stored)
    manager = MagicMock()
    manager.get_binding.side_effect = lambda number: binding if number == (5940 if provenance == "direct-pr" else 5459) else None
    manager.read_bindings_strict.return_value = {"5459": binding}
    github = MagicMock()
    install_mock_pr_history(github, "a" * 40)
    github.get_pull_request_metadata_strict.return_value = {"number": 5940, "state": "open", "draft": True, "body": NARRATIVE + "\n" + marker, "head": {"ref": "grid-cleanup", "sha": "h1"}}
    owner = 5459 if provenance == "lifecycle-issue" else None
    evidence = DraftPRAdmissionEvidence()
    with (
        patch("auto_coder.pr_processor.CloudManager", return_value=manager),
        patch("auto_coder.pr_processor._selected_local_backend_name", return_value=""),
        patch("auto_coder.codex_pr_attribution.resolve_codex_pr_origin") as attribution,
        patch("auto_coder.cloud_task_engine.CloudTaskEngine") as factory,
    ):
        attribution.return_value.origin = None
        factory.return_value.get_client_for_provider.return_value = MagicMock()
        blocker = draft_pr_implementation_blocker("owner/repo", 5940, github, owner, expected_head=("grid-cleanup", "h1"), evidence=evidence)
        assert (blocker is None) is (provenance != "missing")
        authority = evidence.authority or DraftPRRepairAuthority("owner/repo", 5940, "grid-cleanup", "h1")
        with draft_pr_repair_authority_scope(authority):
            resolution = _resolve_cloud_task_origin("owner/repo", github.get_pull_request_metadata_strict.return_value, github)
            if provenance == "missing":
                assert resolution.origin is None
                assert "no provider-owned" in resolution.reason
                factory.assert_not_called()
            else:
                assert (resolution.origin.provider, resolution.origin.task_id) == (provider, stored)
                assert _revalidate_cloud_origin("owner/repo", 5940, resolution.origin, github_client=github) is None
                manager.get_binding.side_effect = lambda number: None
                assert "no provider-owned" in _revalidate_cloud_origin("owner/repo", 5940, resolution.origin, github_client=github)


@pytest.mark.parametrize("shared_session", [False, True])
def test_unassociated_recorded_provider_sessions_do_not_admit_draft(shared_session):
    from auto_coder.cloud_manager import CloudManager
    from auto_coder.pr_processor import draft_pr_implementation_blocker

    manager = CloudManager("owner/repo")
    assert manager.add_session(5459, "first-task", provider="jules")
    assert manager.add_session(5460, "first-task" if shared_session else "second-task", provider="jules")
    github = MagicMock()
    install_mock_pr_history(github, "a" * 40)
    github.get_pull_request_metadata_strict.return_value = {"number": 5940, "state": "open", "draft": True, "body": "https://jules.google.com/session/first-task\nhttps://jules.google.com/task/second-task"}
    with patch("auto_coder.pr_processor._selected_local_backend_name", return_value=""), patch("auto_coder.cloud_task_engine.CloudTaskEngine") as factory:
        blocker = draft_pr_implementation_blocker("owner/repo", 5940, github)
    assert "no provider-owned" in blocker
    factory.assert_not_called()


NON_DIRECTIVES = [
    "- Closed #5459/REQ-007–008 (prior Grid requirement)",
    "- Closed owner/repo#5459/REQ-007–008 (prior Grid requirement)",
    "> Example: Closes #5459",
    "```markdown\nCloses #5459\n```",
    "~~~~\nCloses #5459\n~~~~",
    "    Closes #5459",
    "Example: `Closes #5459`",
    "Example: ``Closes #5459``",
]


@pytest.mark.parametrize("qualified", [False, True])
@pytest.mark.parametrize("path", ["intake", "startup"])
@pytest.mark.parametrize("suffix,valid", [("example", False), ("_example", False), ("例", False), ("", True), (" ", True), ("\n", True), (".", True), (",", True), (";", True), (":", True), ("!", True), ("?", True), (")", True), ("]", True), ("–", True), ("-", True)])
def test_lifecycle_issue_reference_requires_exact_token(tmp_path, qualified, path, suffix, valid):
    from tests.test_implementation_slots import GitHubState

    qualifier = "owner/repo" if qualified else ""
    body = f"Closes {qualifier}#5459{suffix}"
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    owner = ImplementationOwner("issue", 5459)
    assert slots.reserve(owner, implementation_pr=5488)
    pr = {"number": 5940, "body": body, "head": {"ref": "grid-cleanup"}}
    github = GitHubState(issues={5459: {"number": 5459, "state": "closed"}}, prs={5488: {"state": "closed", "merged": True}, 5940: {"state": "open"}}, open_prs=[pr])
    if path == "intake":
        assert slots.resolve_owner("pr", pr, github) == (owner if valid else ImplementationOwner("pr", 5940))
    else:
        slots.reconcile(github, discover_open_prs=True)
        if valid:
            assert slots.snapshot().owners[0].implementation_prs == (5488, 5940)
        else:
            assert slots.active_owners() == ()
    assert extract_lifecycle_directive_issue_references(body) == ([(qualifier or None, 5459)] if valid else [])


@pytest.mark.parametrize("body", NON_DIRECTIVES)
def test_documentary_directives_never_enter_intake_or_startup(tmp_path, body):
    from tests.test_implementation_slots import GitHubState

    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    owner = ImplementationOwner("issue", 5459)
    assert slots.reserve(owner, implementation_pr=5488)
    pr = {"number": 5940, "body": body, "head": {"ref": "grid-cleanup"}}
    github = GitHubState(issues={5459: {"number": 5459, "state": "closed"}}, prs={5488: {"state": "closed", "merged": True}, 5940: {"state": "open"}}, open_prs=[pr])
    assert extract_lifecycle_directive_issue_references(body) == []
    assert slots.resolve_owner("pr", pr, github) == ImplementationOwner("pr", 5940)
    slots.reconcile(github, discover_open_prs=True)
    assert slots.active_owners() == ()


@pytest.mark.parametrize("verb", ["close", "closes", "closed", "closing", "fix", "fixes", "fixed", "resolve", "resolves", "resolved", "resolving"])
def test_plain_directives_survive_documentary_regions_in_intake_and_startup(tmp_path, verb):
    from tests.test_implementation_slots import GitHubState

    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    owner = ImplementationOwner("issue", 5459)
    assert slots.reserve(owner, implementation_pr=5488)
    body = f"> Closes #9999\n\n```\nCloses #9999\n```\n\n- {verb}: owner/repo#5459\n"
    pr = {"number": 5940, "body": body, "head": {"ref": "grid-cleanup"}}
    github = GitHubState(issues={5459: {"number": 5459, "state": "closed"}}, prs={5488: {"state": "closed", "merged": True}, 5940: {"state": "open"}}, open_prs=[pr])
    assert extract_lifecycle_directive_issue_references(body) == [("owner/repo", 5459)]
    assert slots.resolve_owner("pr", pr, github) == owner
    slots.reconcile(github, discover_open_prs=True)
    assert slots.snapshot().owners[0].implementation_prs == (5488, 5940)


def test_reconciliation_after_active_sample_cannot_recreate_unsupported_draft(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from tests.test_implementation_slots import GitHubState
    from tests.test_issue_discovery_admission import _issue

    config = AutomationConfig()
    old = ImplementationOwner("issue", 5459)
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    assert slots.reserve(old, implementation_pr=5488)
    state = GitHubState(issues={5459: {"number": 5459, "state": "closed"}}, prs={5488: {"state": "closed", "merged": True}, 5940: {"state": "open", "draft": True}})
    github = MagicMock()
    install_mock_pr_history(github, "a" * 40)
    for name in ("get_issue", "get_issue_details", "get_pull_request", "get_pr_details", "get_connected_prs"):
        getattr(github, name).side_effect = getattr(state, name)
    pr = {"number": 5940, "title": "New draft", "body": "", "draft": True, "state": "open", "head": {"ref": "issue-5459-grid", "sha": "a" * 40}, "labels": []}
    github.get_pull_request_metadata_strict.return_value = pr
    engine = AutomationEngine(github, config=config)
    engine.implementation_slots = slots
    sampled = threading.Event()
    reconciled = threading.Event()
    real_active_owners = slots.active_owners
    armed = True

    def active_owners_barrier():
        nonlocal armed
        owners = real_active_owners()
        if armed and old in owners:
            armed = False
            sampled.set()
            assert reconciled.wait(5)
        return owners

    monkeypatch.setattr(slots, "active_owners", active_owners_barrier)
    with (
        patch.object(engine, "refresh_review_adjudications"),
        patch("auto_coder.pr_processor._selected_local_backend_name", return_value=""),
        patch.object(engine, "_process_single_candidate_reserved", return_value=CandidateProcessingResult(type="pr", number=5940, success=False, target_outcome=ExplicitTargetOutcome.DEFERRED)) as process,
    ):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(engine._process_single_candidate_unified, "owner/repo", Candidate(type="pr", data=pr, priority=0), config)
            try:
                assert sampled.wait(5)
                slots.reconcile(github)
                assert real_active_owners() == ()
            finally:
                reconciled.set()
            result = future.result(timeout=5)
        assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
        assert real_active_owners() == ()
        process.assert_not_called()
    assert real_active_owners() == ()
    issue = {**_issue(5949), "labels": [{"name": "implementation-ready"}]}
    github.get_issue_dispatch_snapshot_strict.return_value = issue
    with patch.object(engine, "_process_single_candidate_reserved", return_value=CandidateProcessingResult(type="issue", number=5949, success=True)) as dispatch:
        admitted = engine._process_single_candidate_unified("owner/repo", Candidate(type="issue", data=issue, priority=0), config)
        engine._process_single_candidate_unified("owner/repo", Candidate(type="issue", data=issue, priority=0), config)
    assert admitted.success is True
    dispatch.assert_called_once()


@pytest.mark.parametrize("head", [{"ref": "grid-cleanup", "sha": "b" * 40}, {"ref": "different-branch", "sha": "a" * 40}])
def test_fresh_draft_preflight_requires_exact_observed_head(head):
    from auto_coder.pr_processor import draft_pr_implementation_blocker

    github = MagicMock()
    install_mock_pr_history(github, "a" * 40)
    github.get_pull_request_metadata_strict.return_value = {"number": 5940, "state": "open", "draft": False, "head": head}
    with patch("auto_coder.pr_processor._selected_local_backend_name", return_value=""):
        blocker = draft_pr_implementation_blocker("owner/repo", 5940, github, expected_head=("grid-cleanup", "a" * 40))
    assert "head changed" in blocker


def test_capacity_retry_rechecks_fresh_draft_admission(tmp_path):
    from tests.test_implementation_slots import GitHubState

    config = AutomationConfig()
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    assert slots.reserve(ImplementationOwner("issue", 5459), implementation_pr=5488)
    state = GitHubState(issues={5459: {"number": 5459, "state": "closed"}, 6000: {"number": 6000, "state": "open"}}, prs={5488: {"state": "closed", "merged": True}})
    github = MagicMock()
    install_mock_pr_history(github, "a" * 40)
    for name in ("get_issue", "get_pull_request", "get_pr_details", "get_connected_prs"):
        getattr(github, name).side_effect = getattr(state, name)
    reconciled = False

    def issue_details(issue):
        nonlocal reconciled
        reconciled = True
        return issue

    github.get_issue_details.side_effect = issue_details
    pr = {"number": 5940, "title": "Draft", "body": "", "state": "open", "draft": True, "head": {"ref": "issue-6000-grid", "sha": "a" * 40}, "labels": []}
    github.get_pull_request_metadata_strict.side_effect = lambda *_: {**pr, "draft": reconciled}
    engine = AutomationEngine(github, config=config)
    engine.implementation_slots = slots
    with (
        patch.object(engine, "refresh_review_adjudications"),
        patch("auto_coder.pr_processor._selected_local_backend_name", return_value=""),
        patch.object(engine, "_process_single_candidate_reserved", return_value=CandidateProcessingResult(type="pr", number=5940, success=False, target_outcome=ExplicitTargetOutcome.DEFERRED)) as dispatch,
    ):
        result = engine._process_single_candidate_unified("owner/repo", Candidate(type="pr", data=pr, priority=0), config)
    assert reconciled is True
    assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
    assert slots.active_owners() == ()
    dispatch.assert_not_called()


@pytest.mark.parametrize("body", ["Fixes:\n```\nexample\n```\n#5459", "Fixes:\n> example\n#5459", "Fixes `#5459`"])
def test_directive_cannot_bridge_documentary_region(body):
    assert extract_lifecycle_directive_issue_references(body) == []


def test_trailing_comment_does_not_hide_genuine_directive():
    assert extract_lifecycle_directive_issue_references("Closes #5459 <!-- controller note -->") == [(None, 5459)]


def test_retained_active_execution_survives_draft_admission_guard(tmp_path):
    from tests.test_implementation_slots import GitHubState

    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    owner = ImplementationOwner("issue", 5459)
    execution = slots.start_execution(owner, implementation_pr=5488)
    state = GitHubState(issues={5459: {"number": 5459, "state": "closed"}}, prs={5488: {"state": "closed", "merged": True}})
    github = MagicMock()
    install_mock_pr_history(github, "a" * 40)
    for name in ("get_issue", "get_issue_details", "get_pull_request", "get_pr_details", "get_connected_prs"):
        getattr(github, name).side_effect = getattr(state, name)
    config = AutomationConfig()
    engine = AutomationEngine(github, config=config)
    engine.implementation_slots = slots
    pr = {"number": 5940, "title": "Draft", "body": "", "draft": True, "head": {"ref": "issue-5459-grid", "sha": "a" * 40}, "labels": []}
    with patch.object(engine, "refresh_review_adjudications"), patch("auto_coder.pr_processor.draft_pr_implementation_blocker") as policy, patch.object(engine, "_process_single_candidate_reserved") as dispatch:
        result = engine._process_single_candidate_unified("owner/repo", Candidate(type="pr", data=pr, priority=0), config)
    assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
    assert slots.active_execution_ids(owner) == (execution,)
    assert slots.snapshot().owners[0].implementation_prs == (5488,)
    policy.assert_not_called()
    dispatch.assert_not_called()


CONTAINER_CODE_EXAMPLES = [
    "\tCloses #5459",
    "- ~~~\n  Closes #5459\n  ~~~",
    "~~~\n~~~ not a closing fence\nCloses #5459\n~~~",
]


@pytest.mark.parametrize("body", CONTAINER_CODE_EXAMPLES)
@pytest.mark.parametrize("path", ["intake", "startup"])
def test_commonmark_code_examples_cannot_create_ownership(tmp_path, body, path):
    from tests.test_implementation_slots import GitHubState

    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    owner = ImplementationOwner("issue", 5459)
    assert slots.reserve(owner, implementation_pr=5488)
    pr = {"number": 5940, "body": body, "head": {"ref": "grid-cleanup"}}
    github = GitHubState(issues={5459: {"number": 5459, "state": "closed"}}, prs={5488: {"state": "closed", "merged": True}, 5940: {"state": "open"}}, open_prs=[pr])
    if path == "intake":
        assert slots.resolve_owner("pr", pr, github) == ImplementationOwner("pr", 5940)
    else:
        slots.reconcile(github, discover_open_prs=True)
        assert slots.active_owners() == ()
    assert extract_lifecycle_directive_issue_references(body) == []


@pytest.mark.parametrize("body", CONTAINER_CODE_EXAMPLES)
@pytest.mark.parametrize("verb", ["close", "closes", "closed", "closing", "fix", "fixes", "fixed", "resolve", "resolves", "resolved", "resolving"])
def test_genuine_directive_after_commonmark_code_example_survives(tmp_path, body, verb):
    from tests.test_implementation_slots import GitHubState

    body = body + f"\n\n{verb}: owner/repo#5459"
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    owner = ImplementationOwner("issue", 5459)
    assert slots.reserve(owner, implementation_pr=5488)
    pr = {"number": 5940, "body": body, "head": {"ref": "grid-cleanup"}}
    github = GitHubState(issues={5459: {"number": 5459, "state": "closed"}}, prs={5488: {"state": "closed", "merged": True}, 5940: {"state": "open"}}, open_prs=[pr])
    assert extract_lifecycle_directive_issue_references(body) == [("owner/repo", 5459)]
    assert slots.resolve_owner("pr", pr, github) == owner
    slots.reconcile(github, discover_open_prs=True)
    assert slots.snapshot().owners[0].implementation_prs == (5488, 5940)


@pytest.mark.parametrize("other_binding", [False, True])
@pytest.mark.parametrize("retained", [False, True])
def test_draft_admission_and_actual_review_repair_use_same_issue_provider(tmp_path, other_binding, retained):
    _joined_draft_review_repair(tmp_path, other_binding=other_binding, retained=retained)


@pytest.mark.parametrize("change", ["binding", "head", "unavailable"])
def test_draft_repair_holds_when_preflight_authority_changes(tmp_path, change):
    _joined_draft_review_repair(tmp_path, other_binding=True, change=change)


def _joined_draft_review_repair(tmp_path, *, other_binding, retained=False, change=""):
    from auto_coder.cloud_manager import CloudManager
    from auto_coder.execution_trace import Outcome
    from auto_coder.pr_processor import _adversarial_repair_outcome, _delegate_cloud_review_thread_repair, _resolve_pr_issue_numbers
    from auto_coder.util.gh_cache import PullRequestRepairMetadata, ReviewThread, ReviewThreadComment

    manager = CloudManager("owner/repo")
    assert manager.add_session(5459, "task-A", provider="jules")
    if other_binding:
        assert manager.add_session(5460, "task-B", provider="jules")
    calls = []

    class Transport:
        def send_followup(self, task_id, prompt):
            calls.append((task_id, prompt))
            return True

    pr = {"number": 5940, "title": "Grid", "body": "Related to #5460", "draft": True, "state": "open", "head": {"ref": "issue-5459-grid", "sha": "a" * 40}, "base": {"ref": "main", "sha": "base"}, "labels": [], "user": {"login": "maintainer"}}
    github = MagicMock()
    install_mock_pr_history(github, "a" * 40)
    github.get_authenticated_controller_identity.return_value = ControllerIdentity("auto-coder-bot", account_id=20)
    github.get_pr_comments_strict.return_value = []
    github.get_issue.return_value = {"number": 5459, "state": "open"}
    github.get_issue_details.side_effect = lambda issue: issue
    github.get_pull_request_metadata_strict.return_value = pr
    github.get_pull_request_repair_metadata_strict.return_value = PullRequestRepairMetadata(head_ref=pr["head"]["ref"], head_sha=pr["head"]["sha"], base_ref="main")
    github.get_pr_comments.return_value = []
    config = AutomationConfig()
    engine = AutomationEngine(github, config=config)
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    engine.implementation_slots = slots
    if retained:
        assert slots.reserve(ImplementationOwner("issue", 5459), implementation_pr=5940)
    repairs = []
    thread = ReviewThread(id="thread-1", is_resolved=False, comments=[ReviewThreadComment(database_id=1, body="Please cover the empty grid case", author_login="reviewer")])

    def dispatch(*_args, **_kwargs):
        if change == "binding":
            assert manager.add_session(5459, "task-C", provider="jules")
        elif change == "head":
            github.get_pull_request_metadata_strict.return_value = {**pr, "head": {**pr["head"], "sha": "b" * 40}}
        elif change == "unavailable":
            github.get_pull_request_metadata_strict.side_effect = RuntimeError("GitHub unavailable after admission")
        repairs.append(_delegate_cloud_review_thread_repair("owner/repo", pr, github_client=github, unresolved_threads=(thread,)))
        return CandidateProcessingResult(type="pr", number=5940, success=repairs[-1].delivered, target_outcome=ExplicitTargetOutcome.SUCCESS if repairs[-1].delivered else ExplicitTargetOutcome.DEFERRED)

    with patch.object(engine, "refresh_review_adjudications"), patch("auto_coder.pr_processor._selected_local_backend_name", return_value=""), patch("auto_coder.cloud_task_engine.CloudTaskEngine") as factory, patch.object(engine, "_process_single_candidate_reserved", side_effect=dispatch):
        factory.return_value.get_client_for_provider.return_value = Transport()
        result = engine._process_single_candidate_unified("owner/repo", Candidate(type="pr", data=pr, priority=0), config)
    assert _resolve_pr_issue_numbers("owner/repo", pr, github) == [5460]
    assert len(repairs) == 1
    if change:
        assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
        assert repairs[0].deferred is True
        assert repairs[0].delivered is False
        assert _adversarial_repair_outcome(repairs[0]) is Outcome.DEFERRED
        assert calls == []
    else:
        assert result.success is True
        assert repairs[0].delivered is True
        assert _adversarial_repair_outcome(repairs[0]) is Outcome.ACCEPTED_HANDOFF
        assert len(calls) == 1
        assert calls[0][0] == "task-A"
        assert "existing pull request #5940" in calls[0][1]


def test_strict_draft_preflight_does_not_hold_slot_store_lock(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    github = MagicMock()
    install_mock_pr_history(github, "a" * 40)
    pr = {"number": 5940, "title": "Grid", "body": "", "draft": True, "state": "open", "head": {"ref": "grid-cleanup", "sha": "a" * 40}, "labels": []}
    entered = threading.Event()
    continue_read = threading.Event()

    def metadata(*_args):
        entered.set()
        assert continue_read.wait(10)
        return pr

    github.get_pull_request_metadata_strict.side_effect = metadata
    config = AutomationConfig()
    engine = AutomationEngine(github, config=config)
    engine.implementation_slots = slots
    with patch.object(engine, "refresh_review_adjudications"), patch("auto_coder.pr_processor._selected_local_backend_name", return_value=""), ThreadPoolExecutor(max_workers=2) as pool:
        processing = pool.submit(engine._process_single_candidate_unified, "owner/repo", Candidate(type="pr", data=pr, priority=0), config)
        try:
            assert entered.wait(5)
            another = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
            writer = pool.submit(another.reserve, ImplementationOwner("issue", 9999))
            assert writer.result(timeout=3) is True
            another.release(ImplementationOwner("issue", 9999))
        finally:
            continue_read.set()
        result = processing.result(timeout=5)
    assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
    assert slots.active_owners() == ()


@pytest.mark.parametrize("body", [r"\`literal` . Closes #5459`", "Cl\\oses #5459", "Cl&#111;ses #5459", "> Example\nCloses #5459", "- > Example: Closes #5459"])
def test_commonmark_inline_and_quote_regions_do_not_manufacture_directives(body):
    assert extract_lifecycle_directive_issue_references(body) == []


def test_code_comment_example_does_not_hide_following_real_directive():
    body = "```html\n<!-- unclosed example comment\n```\n\nCloses owner/repo#5459"
    assert extract_lifecycle_directive_issue_references(body) == [("owner/repo", 5459)]


def test_directive_whitespace_can_span_authored_paragraphs():
    assert extract_lifecycle_directive_issue_references("Closes:\n\n#5459") == [(None, 5459)]


@pytest.mark.parametrize("change", ["", "binding", "head"])
def test_bound_draft_conflict_repair_rechecks_authority_before_send(tmp_path, change):
    from auto_coder import pr_processor
    from auto_coder.cloud_manager import CloudManager

    manager = CloudManager("owner/repo")
    assert manager.add_session(5459, "task-A", provider="claude-routine")
    assert manager.add_session(5460, "task-B", provider="claude-routine")
    sent = []

    class Transport:
        backend_name = "claude-routine"

        def send_followup(self, task_id, message):
            sent.append(task_id)
            return True

    pr = {"number": 5940, "body": "Related to #5460", "draft": True, "state": "open", "head": {"ref": "issue-5459-grid", "sha": "a" * 40}, "base": {"ref": "main", "sha": "base"}}
    github = MagicMock()
    install_mock_pr_history(github, "a" * 40)
    github.get_pull_request_metadata_strict.return_value = pr
    evidence = pr_processor.DraftPRAdmissionEvidence()
    recorder = pr_processor._record_cloud_conflict_deliveries
    changed = False

    def record(path, deliveries):
        nonlocal changed
        recorder(path, deliveries)
        if not changed and any(item.status == "pending" for item in deliveries.values()):
            changed = True
            if change == "binding":
                assert manager.add_session(5459, "task-C", provider="claude-routine")
            elif change == "head":
                github.get_pull_request_metadata_strict.return_value = {**pr, "head": {**pr["head"], "sha": "b" * 40}}

    with patch("auto_coder.pr_processor._selected_local_backend_name", return_value=""), patch("auto_coder.cloud_task_engine.CloudTaskEngine") as factory, patch("auto_coder.pr_processor._record_cloud_conflict_deliveries", side_effect=record):
        factory.return_value.get_client_for_provider.return_value = Transport()
        assert pr_processor.draft_pr_implementation_blocker("owner/repo", 5940, github, 5459, expected_head=(pr["head"]["ref"], pr["head"]["sha"]), evidence=evidence) is None
        with pr_processor.draft_pr_repair_authority_scope(evidence.authority):
            result = pr_processor._delegate_cloud_merge_conflict_repair_result("owner/repo", pr, github)
    assert changed is True
    if change:
        assert result.deferred is True
        assert result.delegated is False
        assert sent == []
    else:
        assert result.delegated is True
        assert sent == ["task-A"]


def test_draft_preparation_does_not_repeat_locked_hierarchy_reads(tmp_path):
    from auto_coder.implementation_slots import ImplementationAdmissionPreparationRequired
    from tests.test_implementation_slots import AuthoritativeHierarchy

    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    owner = ImplementationOwner("issue", 5459)
    prepared = False

    def policy():
        if not prepared:
            raise ImplementationAdmissionPreparationRequired("prepare outside the store lock")
        return None

    with patch.object(slots, "_read_issue_hierarchy", wraps=slots._read_issue_hierarchy) as hierarchy:
        with pytest.raises(ImplementationAdmissionPreparationRequired):
            slots.start_execution(owner, implementation_pr=5940, github_client=AuthoritativeHierarchy(), fresh_admission_check=policy)
        hierarchy.assert_not_called()
        prepared = True
        assert slots.start_execution(owner, implementation_pr=5940, github_client=AuthoritativeHierarchy(), fresh_admission_check=policy) is not None
        assert hierarchy.call_count == 3
