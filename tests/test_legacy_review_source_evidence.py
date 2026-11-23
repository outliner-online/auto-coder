"""Ordinary legacy evidence is not an unsent source-lane certificate."""

import hashlib
import json
from string import Template
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.canonical_pr_blocker_ledger import CanonicalPRBlockerLedger
from auto_coder.cloud_provider_instructions import CloudTaskOperation, prepare_cloud_task
from auto_coder.cloud_run import CloudRun, CloudRunRepository
from auto_coder.codex_cloud_client import _codex_followup_state_path
from auto_coder.codex_wham_client import FollowUpDeliveryOutcome, FollowUpDeliveryResult
from auto_coder.codex_work_accounting import CodexWorkAccounting
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.pr_processor import ClaimedReviewThreadGateState, _cloud_review_repair_state_path, _handle_pr_merge, _review_feedback_identity
from auto_coder.pr_repair import ExistingPrRepairTarget, build_existing_pr_repair_prompt
from auto_coder.pr_review_cycle import DELIVERY_NONE, PrReviewCycleRepository
from auto_coder.prompt_loader import get_prompt_template
from auto_coder.util.gh_cache import PullRequestRepairMetadata
from tests.ci_observation_fakes import authoritative_ci_status
from tests.test_scoped_review_followup import HEAD, PR, REPO, TASK, _registered_findings


@pytest.fixture
def ordinary(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    slots = ImplementationSlotRepository(REPO, 1)
    owner = ImplementationOwner("issue", 9)
    assert slots.reserve(owner)
    incarnation = slots.establish_incarnation(owner)
    CodexWorkAccounting(slots).initialize_fresh(owner, incarnation)
    assert slots.record_implementation_pr(owner, PR)
    CloudRunRepository(REPO).save(CloudRun(REPO, 9, 1, "codex-cloud", task_id=TASK, backend_name="codex-cloud", launch_identity="launch-9", publication_head_repository=REPO, publication_head_ref="work-branch", pull_request_numbers=[PR]))
    threads = _registered_findings(tmp_path, monkeypatch, "fresh")
    old = next(thread for thread in threads if thread.comments[0].database_id == 100)
    new = next(thread for thread in threads if thread.comments[0].database_id == 110)
    data = {"number": PR, "body": "Fixes #9", "head": {"ref": "work-branch", "sha": HEAD, "repo": {"full_name": REPO}}, "base": {"ref": "main", "sha": "b" * 40, "repo": {"id": 3}}, "labels": [], "user": {"login": "maintainer"}}
    github = MagicMock()
    github.get_pr_comments.return_value = []
    github.get_pr_review_threads_strict.return_value = threads
    github.get_pull_request_metadata_strict.return_value = {**data, "state": "open"}
    github.get_pull_request_repair_metadata_strict.return_value = PullRequestRepairMetadata("work-branch", HEAD, "main")
    wham = MagicMock()
    wham.resolve_latest_assistant_turn.return_value = TASK + "~assttrn_1"
    wham.reconcile_follow_up.return_value = False
    wham.send_follow_up.return_value = FollowUpDeliveryResult(FollowUpDeliveryOutcome.INDETERMINATE, 502)
    config = AutomationConfig()
    config.AUTO_MERGE = True
    with (
        patch("auto_coder.codex_cloud_client.CodexWhamClient", return_value=wham),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.pr_processor._check_github_actions_status", side_effect=authoritative_ci_status(success=True, ids=[1])),
        patch("auto_coder.pr_processor._get_claimed_review_thread_state") as gate,
    ):

        def run(selected):
            gate.return_value = ClaimedReviewThreadGateState(unresolved=selected, blocking_unresolved=selected, has_blocking_unresolved=True)
            return _handle_pr_merge(github, REPO, data, config, {})

        yield run, old, new, wham, github, incarnation


@pytest.mark.parametrize("control", ["distinct_registered", "duplicate", "paraphrase", "missing_owner_binding", "missing_payload"])
def test_ordinary_registered_sources_do_not_prove_legacy_lane_is_unsent(ordinary, control):
    run, old, new, wham, github, incarnation = ordinary
    first = run((old,))
    assert any("unconfirmed follow-up delivery" in action for action in first), first
    ledger = CanonicalPRBlockerLedger()
    bundles = ledger.get_repair_bundles_for_pr("https://api.github.com", REPO, PR)
    assert len(bundles) == 1
    bundle = bundles[0]
    with ledger._connect() as connection:
        payload = connection.execute("SELECT rendered_payload FROM repair_bundles WHERE bundle_id = ?", (bundle.bundle_id,)).fetchone()[0]
    target = ExistingPrRepairTarget(REPO, PR, "work-branch", "main", HEAD)
    details = Template(get_prompt_template("codex_cloud.review_thread_repair_details")).safe_substitute(actionable_feedback=payload)
    prompt = build_existing_pr_repair_prompt(target, details, bundle=bundle)
    prepared = prepare_cloud_task(prompt, recipient="codex-cloud", operation=CloudTaskOperation.CONTINUATION, no_edit=False).prepared_task
    native_path = _codex_followup_state_path(REPO)
    records = json.loads(native_path.read_text())
    assert len(records) == 1
    native = next(iter(records.values()))
    old_identity = _review_feedback_identity(f"{REPO}#{PR}:codex-cloud:{TASK}:", old, 0)
    assert native["logical_identity"] == old_identity
    assert native["message_fingerprint"] == hashlib.sha256(prepared.encode()).hexdigest()
    assert (native["repair_pr_number"], native["repair_head_sha"], native["repair_owner_incarnation"]) == (PR, HEAD, incarnation)
    snapshot = ledger.get_snapshot("https://api.github.com", REPO, PR)
    for root, requirement in ((100, "REQ-001"), (110, "REQ-002")):
        blockers = snapshot.get_blockers_for_alias("github_root_comment", str(root))
        assert len(blockers) == 1
        assert blockers[0].qualified_requirements[0].requirement_id == requirement
    # Even the finding whose real POST became unknown still says NONE. Neither
    # finding status nor canonical admission is a provider source-lane receipt.
    findings = PrReviewCycleRepository(REPO).snapshot(PR).findings
    assert len(findings) == 2
    assert {finding.delivery_status for finding in findings} == {DELIVERY_NONE}
    if control in {"duplicate", "paraphrase"}:
        from auto_coder.review_feedback_marker import REVIEW_GUIDANCE_MARKER
        from auto_coder.util.gh_cache import ReviewThreadComment

        body = old.comments[0].body if control == "duplicate" else "Ensure empty maps fail for executable code but pass for type-only files."
        old.comments.append(ReviewThreadComment(999, body + "\n\n" + REVIEW_GUIDANCE_MARKER, "bot", 7, "Bot", "2026-01-03T00:00:00Z", "2026-01-03T00:00:00Z", 100))
        selected = (old,)
    else:
        selected = (old, new)
    if control == "missing_owner_binding":
        native["repair_owner_incarnation"] = ""
        native_path.write_text(json.dumps(records))
    if control == "missing_payload":
        with ledger._connect() as connection:
            connection.execute("UPDATE repair_bundles SET rendered_payload = '' WHERE bundle_id = ?", (bundle.bundle_id,))
    original_native = native_path.read_bytes()
    accounting = CodexWorkAccounting(ImplementationSlotRepository(REPO, 1))
    original_work = accounting.snapshot(ImplementationOwner("issue", 9), incarnation)
    assert original_work.operations
    for _restart in range(2):
        actions = run(selected)
        assert any("independent scope resolution" in action for action in actions), actions
    wham.send_follow_up.assert_called_once()
    assert native_path.read_bytes() == original_native
    assert accounting.snapshot(ImplementationOwner("issue", 9), incarnation) == original_work
    state = json.loads(_cloud_review_repair_state_path(REPO).read_text())
    assert state["pending_feedback"] == [old_identity]
    assert state["delivered_feedback"] == []
    github.merge_pull_request.assert_not_called()


@pytest.mark.parametrize("outcome", [FollowUpDeliveryOutcome.NOT_DELIVERED, FollowUpDeliveryOutcome.INDETERMINATE])
def test_ordinary_bookkeeping_exception_consumes_only_exact_native_rejection(ordinary, outcome):
    run, old, _new, wham, github, _incarnation = ordinary
    from auto_coder.pr_processor import _record_review_feedback_state

    state_path = _cloud_review_repair_state_path(REPO)
    peer = "owner/repo#999:codex-cloud:other-task:peer"
    _record_review_feedback_state(state_path, set(), {peer})
    wham.send_follow_up.return_value = FollowUpDeliveryResult(outcome, 400 if outcome is FollowUpDeliveryOutcome.NOT_DELIVERED else 502)
    with patch("auto_coder.codex_work_fence.CodexWorkFence.record_delivery", side_effect=RuntimeError("controlled post-transport bookkeeping failure")) as bookkeeping:
        actions = run((old,))
    bookkeeping.assert_called_once()
    wham.send_follow_up.assert_called_once()
    state = json.loads(state_path.read_text())
    own = _review_feedback_identity(f"{REPO}#{PR}:codex-cloud:{TASK}:", old, 0)
    if outcome is FollowUpDeliveryOutcome.NOT_DELIVERED:
        assert state["pending_feedback"] == [peer]
        assert any("rejected follow-up delivery; bookkeeping failed" in action for action in actions), actions
    else:
        assert set(state["pending_feedback"]) == {peer, own}
        assert not any("rejected follow-up delivery" in action for action in actions)
        run((old,))
        wham.send_follow_up.assert_called_once()
    assert state["delivered_feedback"] == []
    github.merge_pull_request.assert_not_called()


@pytest.mark.parametrize("proof", ["stale_exact", "wrong_task", "partial_identity_set"])
def test_ordinary_exception_cannot_reuse_other_rejection_proof(ordinary, proof):
    from auto_coder.codex_cloud_client import CodexCloudClient

    run, old, _new, wham, _github, _incarnation = ordinary
    own = _review_feedback_identity(f"{REPO}#{PR}:codex-cloud:{TASK}:", old, 0)
    initialize = CodexCloudClient.__init__

    def initialized(client, *args, **kwargs):
        initialize(client, *args, **kwargs)
        if proof == "stale_exact":
            client._followup_rejection = (TASK, (own,))

    def fail_before_native(client, repository, task_id, message, identities, **kwargs):
        if proof == "wrong_task":
            client._followup_rejection = ("task_e_other", identities)
        elif proof == "partial_identity_set":
            client._followup_rejection = (task_id, tuple(sorted((*identities, "another-identity"))))
        raise RuntimeError("controlled failure before native sender")

    with patch.object(CodexCloudClient, "__init__", initialized), patch("auto_coder.pr_processor._send_followup_with_quota_admission", side_effect=fail_before_native):
        actions = run((old,))
    wham.send_follow_up.assert_not_called()
    assert json.loads(_cloud_review_repair_state_path(REPO).read_text())["pending_feedback"] == [own]
    assert not any("rejected follow-up delivery" in action for action in actions)
